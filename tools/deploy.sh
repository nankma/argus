#!/usr/bin/env bash
#
# Build, transfer, restart, verify. No LLM in the loop.
#
# Why this exists
# ---------------
# Deploying is deterministic: ten commands in a fixed order with fixed
# checks. Driving it through an agent cost 184k tokens the first time and
# 100k the second, and almost none of that was the commands. A model is
# stateless, so every tool call re-sends the whole conversation -- measured
# on this project, 3,212 turns carried 1.6 BILLION re-read tokens against
# 6,411 tokens of genuinely new input, averaging half a million tokens of
# context per call. Cost scales with (turns x conversation length), which
# grows quadratically. A script's context is zero, forever.
#
# So: the happy path runs here. An agent is for DIAGNOSIS when this script
# fails -- hand it the log path this prints, not the whole deploy.
#
# Every check below exists because something once went wrong without it.
# `docs/plans/deployment-plan.md` and the `build-locally-deploy-remotely`
# skill have the incident histories; the one-line reasons are inline here.
#
# Usage:
#   tools/deploy.sh              # deploy HEAD of main
#   tools/deploy.sh --dry-run    # preflight only, change nothing
#   tools/deploy.sh --skip-ci    # deploy without waiting on CI (say why)
#   tools/deploy.sh --force      # deploy even if the image would be identical
#
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1

INFRA="local-infra/infrastructure.yaml"     # gitignored: real IPs and keys
IMAGE="myfirstagent-bot"
CONTAINER="myfirstagent-bot"
LOGDIR="${TMPDIR:-/tmp}/argus-deploy-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$LOGDIR"

DRY_RUN=0
SKIP_CI=0
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --skip-ci) SKIP_CI=1 ;;
        --force)   FORCE=1 ;;
        *) echo "unknown argument: $arg"; exit 2 ;;
    esac
done

step()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()    { printf '    ok  %s\n' "$*"; }
warn()  { printf '    !!  %s\n' "$*"; }
die()   { printf '\n\033[31mFAILED: %s\033[0m\n' "$*"; printf 'logs: %s\n' "$LOGDIR"; exit 1; }

# Runs a command with its output in a log file rather than on screen, and
# only surfaces the log when it fails. This is the whole cost discipline in
# one function.
run() {
    local name="$1"; shift
    local log="$LOGDIR/$name.log"
    if "$@" > "$log" 2>&1; then
        return 0
    fi
    printf '\n--- last 30 lines of %s ---\n' "$log"
    tail -30 "$log"
    return 1
}

# ---------------------------------------------------------------- preflight

step "Preflight"

[ -f "$INFRA" ] || die "$INFRA not found -- it is gitignored, so a fresh clone has to obtain it separately"

VM_IP=$(grep -m1 -E '^\s+public_ip:' "$INFRA" | awk '{print $2}')
SSH_KEY=$(grep -m1 -E '^\s+private_key:' "$INFRA" | sed 's/^[^:]*: *//' | tr -d '\r')
SSH_USER=$(grep -m1 -E '^\s+user:' "$INFRA" | awk '{print $2}')
[ -n "$VM_IP" ] && [ -n "$SSH_KEY" ] && [ -n "$SSH_USER" ] || die "could not read VM details from $INFRA"
# Windows path from the yaml -> a path ssh can use under Git Bash.
case "$SSH_KEY" in
    [A-Za-z]:\\*) SSH_KEY="/$(echo "${SSH_KEY:0:1}" | tr 'A-Z' 'a-z')/$(echo "${SSH_KEY:3}" | tr '\\' '/')" ;;
esac
[ -f "$SSH_KEY" ] || die "ssh key not found at $SSH_KEY"
SSH=(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=20 "$SSH_USER@$VM_IP")
ok "target $SSH_USER@$VM_IP"

BRANCH=$(git rev-parse --abbrev-ref HEAD)
[ "$BRANCH" = "main" ] || warn "on branch '$BRANCH', not main -- deploying it anyway"

# The image is built from the WORKING TREE, not from the commit. A dirty
# tree therefore ships something no commit describes, which is how you get
# a container nobody can reproduce.
if [ -n "$(git status --porcelain -- '*.py' Dockerfile environment.yml docker-entrypoint.sh)" ]; then
    warn "uncommitted changes to files that go into the image:"
    git status --porcelain -- '*.py' Dockerfile environment.yml docker-entrypoint.sh | sed 's/^/        /'
    warn "the image will contain these, but no commit will describe them"
fi

# CRLF in a shebang breaks ENTRYPOINT with `env: 'bash\r': No such file`.
# .gitattributes should prevent it; verify rather than trust, because the
# working tree is what gets built and autocrlf rewrites on checkout.
if grep -qU $'\r' docker-entrypoint.sh 2>/dev/null; then
    die "docker-entrypoint.sh has CRLF line endings. Fix: sed -i 's/\\r\$//' docker-entrypoint.sh"
fi
ok "docker-entrypoint.sh is LF"

# Does this change reach the container at all? Files outside the Dockerfile
# COPY list (docs, tools/, .claude/) change nothing in the image, and
# rebuilding for them is pure cost -- see CLAUDE.md's deploy policy. Worse
# than pure cost, in fact: a restart re-runs the push job at first=10s and
# re-opens the window where a deploy can kill a push cycle between the send
# and record_push, so a pointless deploy is not a harmless one.
DEPLOYED_ID=$("${SSH[@]}" "sudo docker inspect -f '{{.Image}}' $CONTAINER" 2>/dev/null | tr -d '\r')
ok "container currently runs image ${DEPLOYED_ID:0:19}"

COPIED=$(sed -n 's/^COPY .*MAMBA_USER \(.*\) \.\/$/\1/p' Dockerfile)
COPIED="$COPIED environment.yml Dockerfile"
DEPLOYED_SHA=$("${SSH[@]}" "sudo docker inspect -f '{{index .Config.Labels \"commit\"}}' $CONTAINER" 2>/dev/null | tr -d '\r')
if [ -n "$DEPLOYED_SHA" ] && [ "$DEPLOYED_SHA" != "<no value>" ]; then
    CHANGED=$(git diff --name-only "$DEPLOYED_SHA"..HEAD -- $COPIED 2>/dev/null)
    if [ -z "$CHANGED" ]; then
        warn "nothing in the image changed since ${DEPLOYED_SHA:0:8}"
        [ "$FORCE" -eq 1 ] || die "refusing a no-op deploy. Pass --force if you want the restart itself."
    fi
fi

# Is a push about to fire? A deploy is not a neutral act for the push job.
# `docker stop` kills the container between send() and record_push() if it
# lands mid-cycle, which leaves last_push_at unadvanced and re-sends the
# same digest after restart; and `first=10` re-runs the whole cycle ten
# seconds after start, so anyone already due is pushed immediately rather
# than at the next quarter hour. Neither is fatal, both are avoidable by
# not deploying into the window.
DUE=$("${SSH[@]}" "sudo docker exec -e SUBSCRIBERS_DB_FILE=/data/subscribers.db $CONTAINER \
    /opt/conda/bin/python -c \"
import users_db
from datetime import datetime, timezone
now = datetime.now(timezone.utc)
for s in users_db.list_push_enabled_subscribers():
    lp, iv = s['last_push_at'], s['push_interval_hours']
    left = 0 if lp is None else iv - (now - lp).total_seconds()/3600
    if left < 0.25:
        print(f\\\"{s['chat_id']} due in {max(left,0):.2f}h\\\")
\"" 2>/dev/null | tr -d '\r')
if [ -n "$DUE" ]; then
    warn "a push is due within 15 minutes:"
    echo "$DUE" | sed 's/^/        /'
    [ "$FORCE" -eq 1 ] || die "deploying now could re-send or bring forward that push. Wait, or pass --force."
else
    ok "no push due within 15 minutes"
fi

if [ "$SKIP_CI" -eq 0 ]; then
    HEAD_SHA=$(git rev-parse HEAD)
    CI_STATUS=$(gh run list --limit 20 --json headSha,conclusion,status \
        --jq "[.[] | select(.headSha==\"$HEAD_SHA\")] | .[0] | \"\(.status)/\(.conclusion)\"" 2>/dev/null)
    case "$CI_STATUS" in
        completed/success) ok "CI green for ${HEAD_SHA:0:8}" ;;
        ""|null*)          die "no CI run found for ${HEAD_SHA:0:8}. Push it, or pass --skip-ci and say why." ;;
        *)                 die "CI for ${HEAD_SHA:0:8} is '$CI_STATUS'. A green local pytest is not evidence about CI." ;;
    esac
else
    warn "--skip-ci: CI not checked"
fi

if [ "$DRY_RUN" -eq 1 ]; then
    step "Dry run: stopping before the build. Nothing was changed."
    exit 0
fi

# -------------------------------------------------------------------- build

step "Build"
run build docker build --build-arg "GIT_COMMIT=$(git rev-parse HEAD)" -t "$IMAGE" . || die "docker build failed -- see $LOGDIR/build.log"
LOCAL_ID=$(docker image inspect -f '{{.Id}}' "$IMAGE")
ok "built ${LOCAL_ID:0:19}"

# Two separate checks. The import proves the app is installed; the
# entrypoint one proves the container can actually START, which the import
# does NOT -- it bypasses ENTRYPOINT entirely, and that is exactly how a
# CRLF entrypoint reached production undetected.
run import docker run --rm "$IMAGE" python -c "import combined_bot" || die "the image cannot import combined_bot"
ok "imports"
run entrypoint docker run --rm --entrypoint ./docker-entrypoint.sh "$IMAGE" python -c "print('ok')" \
    || die "ENTRYPOINT is broken -- see $LOGDIR/entrypoint.log"
ok "entrypoint runs"

# ----------------------------------------------------------------- baseline

step "Baseline from the running container"
BASE=$("${SSH[@]}" "sudo docker exec $CONTAINER python -c \"
import users_db, os
print(len(users_db.list_all_subscribers()) if hasattr(users_db,'list_all_subscribers') else -1,
      len(users_db.list_push_enabled_subscribers()),
      len(os.listdir(os.environ.get('NEWS_CACHE_DIR','/data/news_cache'))))
\"" 2>>"$LOGDIR/baseline.log" | tr -d '\r')
if [ -n "$BASE" ]; then
    ok "subscribers/push-enabled/cached-articles: $BASE"
else
    warn "could not read a baseline (see $LOGDIR/baseline.log) -- continuing, but post-deploy comparison will be weaker"
fi

# ----------------------------------------------------------------- transfer

step "Transfer"
docker save "$IMAGE:latest" | "${SSH[@]}" "sudo docker load" > "$LOGDIR/transfer.log" 2>&1 \
    || die "transfer failed -- see $LOGDIR/transfer.log"
REMOTE_ID=$("${SSH[@]}" "sudo docker image inspect -f '{{.Id}}' $IMAGE:latest" | tr -d '\r')
[ "$REMOTE_ID" = "$LOCAL_ID" ] || die "remote image $REMOTE_ID != local $LOCAL_ID -- the transfer did not land what was built"
ok "remote image matches local byte-for-byte"

# ------------------------------------------------------------------ restart

step "Restart"
# The exact flags live in the gitignored infra file so that secrets and IPs
# stay out of the repo, and so there is exactly one place to keep current.
DOCKER_RUN=$(awk '/^    command: \|/{f=1;next} f&&/^    [a-z_]+:/{exit} f' "$INFRA" | sed 's/^      //')
[ -n "$DOCKER_RUN" ] || die "could not read docker_run.command from $INFRA"
echo "$DOCKER_RUN" > "$LOGDIR/docker-run.txt"

"${SSH[@]}" "sudo docker stop $CONTAINER >/dev/null 2>&1; sudo docker rm $CONTAINER >/dev/null 2>&1; true"
"${SSH[@]}" "$DOCKER_RUN" > "$LOGDIR/run.log" 2>&1 || die "docker run failed -- see $LOGDIR/run.log and $LOGDIR/docker-run.txt"
sleep 20

STATE=$("${SSH[@]}" "sudo docker inspect -f '{{.State.Status}} {{.RestartCount}} {{.Image}}' $CONTAINER" | tr -d '\r')
set -- $STATE
[ "$1" = "running" ] || die "container is '$1', not running"
[ "$3" = "$LOCAL_ID" ] || die "container runs $3, not the image just built ($LOCAL_ID)"
ok "running the new image, restarts=$2"

# ------------------------------------------------------------- verification

step "Verify"
FAILURES=0
check() {
    local name="$1"; shift
    if "${SSH[@]}" "$@" > "$LOGDIR/$name.log" 2>&1; then
        ok "$name"
    else
        warn "$name FAILED -- see $LOGDIR/$name.log"
        FAILURES=$((FAILURES + 1))
    fi
}

# `docker logs` being empty was itself a real incident: the bot ran for two
# deploys with no output and nobody checked the log itself.
LOGLINES=$("${SSH[@]}" "sudo docker logs --tail 200 $CONTAINER 2>&1 | wc -l" | tr -d '\r')
if [ "${LOGLINES:-0}" -gt 3 ]; then ok "docker logs has output ($LOGLINES lines)"
else warn "docker logs is nearly empty ($LOGLINES lines)"; FAILURES=$((FAILURES + 1)); fi

check telemetry        "sudo docker exec $CONTAINER python tools/check_telemetry.py"
check data-persistence "sudo docker exec $CONTAINER python tools/check_data_persistence.py"

AFTER=$("${SSH[@]}" "sudo docker exec $CONTAINER python -c \"
import users_db, os
print(len(users_db.list_all_subscribers()) if hasattr(users_db,'list_all_subscribers') else -1,
      len(users_db.list_push_enabled_subscribers()),
      len(os.listdir(os.environ.get('NEWS_CACHE_DIR','/data/news_cache'))))
\"" 2>/dev/null | tr -d '\r')
if [ -n "$BASE" ] && [ -n "$AFTER" ]; then
    if [ "$BASE" = "$AFTER" ]; then ok "subscriber and cache counts unchanged ($AFTER)"
    else warn "counts changed: before [$BASE] after [$AFTER] -- expected for cache, NOT for subscribers"; fi
fi

step "Smoke tests"
# The end-to-end check: this is what caught the DeepSeek thinking-mode
# outage (3/10 passing, every settings command misrouted) when every other
# check above was green.
if python tools/run_smoke_tests.py > "$LOGDIR/smoke.log" 2>&1; then
    ok "$(grep -oE '[0-9]+/[0-9]+ .*passed' "$LOGDIR/smoke.log" | tail -1)"
else
    warn "smoke tests FAILED -- see $LOGDIR/smoke.log"
    grep -iE "^(FAIL|case [0-9]+)" "$LOGDIR/smoke.log" | head -15
    FAILURES=$((FAILURES + 1))
fi

# Silence here is the point: these lines only appear when a guardrail layer
# fell back, which used to happen with no trace at all.
if "${SSH[@]}" "sudo docker logs --tail 500 $CONTAINER 2>&1 | grep -c 'layer [24] FAILED'" | grep -qv '^0'; then
    warn "guardrail fail-open occurred since restart -- check docker logs for 'layer 2 FAILED'"
    FAILURES=$((FAILURES + 1))
fi

# ---------------------------------------------------------------- reporting

step "Result"
printf 'logs: %s\n' "$LOGDIR"
if [ "$FAILURES" -eq 0 ]; then
    printf '\033[32mDeployed %s and verified.\033[0m\n' "${LOCAL_ID:7:12}"
    printf 'Update %s: last_verified, image id, and the docker run command if it changed.\n' "$INFRA"
    exit 0
fi
printf '\033[31m%d verification(s) failed.\033[0m The new image IS running.\n' "$FAILURES"
printf 'Roll back with the previous image id if needed, or hand %s to an agent to diagnose.\n' "$LOGDIR"
exit 1
