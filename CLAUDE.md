# CLAUDE.md

This file provides guidance to Claude Code when working in this repo.
Kept deliberately short — it's loaded into every session regardless of
task, so detail that's only sometimes relevant lives in `docs/` or a
subagent's own definition instead. See **Where to look** below before
assuming something's missing here.

## Overview

A technology-industry news-trend agent on **LangChain** + DeepSeek
(`langchain-deepseek`'s `ChatDeepSeek`). `search_news` pulls from a
pluggable source registry (`news_sources.py`) and writes a trend report
citing sources; `save_note` persists arbitrary notes. Ships as a
Telegram bot (`bot.py`/`admin_bot.py`/`combined_bot.py`) with per-user
interests, reply-language, and scheduled push digests — not one shared
scope, since different subscribers care about different topics.

## Landmines — small, real, worth knowing before you touch nearby code

- **The model is pinned to `deepseek-v4-flash`, not an alias.** DeepSeek's
  catalogue is now `deepseek-v4-pro` / `deepseek-v4-flash` /
  `deepseek-v4-flash-vision-exp`; the old `deepseek-chat` alias still
  resolves but is no longer listed, so it could be repointed at pro (≈3x
  the price, visible only on the invoice) or dropped (bot down). Whatever
  replaces it must support **tool calling** — `search_news`/`save_note`,
  the router and the output check all depend on it, which is why the old
  `deepseek-reasoner` was never an option.
- **Replies are Telegram HTML, not Markdown.** `SYSTEM_PROMPT` asks for
  `<b>`/`<a href="">`; `bot.py` sends with `parse_mode=ParseMode.HTML`.
  See the `telegram-message-formatting` skill before touching either.
- **Don't add `arize-phoenix` back as a dependency.** The full package
  imports `pandas` at `import phoenix` time, which Windows Smart App
  Control blocks outright on this machine. `arize-phoenix-otel` (what's
  actually used) has no pandas dependency and covers everything needed.
- **Tests never make real API calls.** `PHOENIX_ENABLED` unset is a
  no-op (every test/CI run). `build_agent(model)`/`run_agent(...,
  callbacks=None)` take the model and telemetry as parameters
  specifically so tests can substitute fakes — see `tests/fakes.py`'s
  `FakeToolCallingModel`, not LangChain's built-in fakes (they don't
  override `bind_tools()` and raise `NotImplementedError`).

## Where to look

| Need | Where |
|---|---|
| Full doc index, categorized | `docs/README.md` |
| Full architecture, request pipeline, design principles | `docs/system-overview.md` |
| First-time environment setup, running any piece locally | `docs/reference/setup.md` |
| Current VM topology, security model, deploy process shape | `docs/current/infrastructure.md` (real IPs/keys: `local-infra/infrastructure.yaml`, gitignored) |
| A specific feature's design/history/status | the matching `docs/plans/*.md` — see `docs/README.md` for the full index |
| News source registry (what's live, how to add one) | `docs/current/ai-news-sources.md` |
| Diagnosing a live issue (Logfire traces, hot-patching) | `docs/reference/observability-and-debugging.md` |
| How/where alerts reach a human (Logfire alerts, Telegram delivery, any relay) | `docs/plans/observability-platform-plan.md` — read this FIRST before touching alert delivery; two hours were lost 2026-08-28 re-deriving an answer it already had |

## Command output is billed, and billed again

Every byte a command prints enters the context and is re-sent as input on
every later tool call, so a big log early costs (its size) x (steps
remaining). One deploy in this repo cost 184k tokens that way, nearly all
of it build logs and progress bars.

**Redirect long output to a file and read the exit code.** Look inside
only when something failed, and then `grep` for the part that matters:

```bash
docker build -t myfirstagent-bot . > "$LOG" 2>&1; echo "exit=$?"
# only if that was non-zero:
grep -iE "error|failed" "$LOG" | tail -20
```

Better than `-q` or `| tail`, which *discard* the output: a failed build
then has to be re-run to find out why. A file keeps everything at zero
context cost. Use `--tail`/`grep` for things already on disk elsewhere
(`docker logs --tail 50`), and `grep -n` to locate before reading a narrow
range — never `cat` a file to check one line.

Trim noise, never checks. A run that misses a real problem is the
expensive one.

The `redirect-long-output` skill has the measured numbers, the exact
pattern, and the rationalizations to refuse. Load it before a build,
a deploy, or anything else that prints at length.

**A guard enforces this**, globally rather than per-repo — runaway output
is an account-level problem. `~/.claude/hooks/output_budget.py`, registered
in `~/.claude/settings.json`, watches every tool result. Saying "continue"
after a stop raises the ceiling by another budget: a speed bump, not a wall.

Its thresholds are measured, not guessed. Across 4,610 real tool calls in
this repo, **4.4 MB of tool output produced 3.35 billion billed input
tokens** — the re-billing effect, in numbers. And the distribution is
extremely long-tailed:

| | |
|---|---|
| p50 / p95 / p99 | 0.2 KB / 4.1 KB / 13.7 KB |
| 25 calls (0.54%) over 20 KB | 35% of all output |
| 6 calls (0.13%) over 50 KB | 27% of all output |

So the guard leads with a **per-call** check — warn above 25 KB (just past
p99), stop above 100 KB (5 calls in 4,610, every one an unfiltered log).
That fires at the moment the mistake is made and names the command, instead
of reporting a big number hundreds of calls later. A 2 MB session total is
kept only as a backstop for the other shape: a long grind of medium calls.

## Testing

```powershell
conda activate myfirstagent
pytest
```

No `DEEPSEEK_API_KEY` needed — runs against fakes/mocks only. Requires
`pytest.ini`'s `pythonpath = .` to import project modules at all.

Write a change's own unit tests as part of the change, and run plain
`pytest` yourself while iterating. **After finishing any code change**
(not doc-only edits), dispatch in order — `code-reviewer` (coding
standards), then `qa-engineer` (matches its documented design, coverage,
smoke suite, guardrail harness), then `deploy-engineer`. Each reports
back pass or a diagnosis; don't advance past a failure.

## Deploying: batch it, don't ship every merge

A merged PR is **not** a reason to deploy. Deployments are expensive in a
way that isn't obvious from the diff: each one is a build, an image
transfer over SSH, a container swap, and a full verification pass, and it
resets the bot's uptime. Merge freely; let changes accumulate on `main`
and go out together.

**Deploy when the change is worth the interruption:**

- a live incident or a bug users are hitting now
- a fix whose whole value is that it stops something ongoing — the
  2026-08-21 test-account leak was billing real money every 6 hours, so it
  shipped immediately
- a change the next piece of work depends on being live

**Don't deploy for**: a doc update, a comment, a refactor with no
behaviour change, a new test, or an analysis tool under
`docs/analysis/tools/` — those aren't even in the image. Check the
Dockerfile's `COPY` line: a commit touching nothing in it changes nothing
in the container, and rebuilding for it is pure cost.

When in doubt, say what is waiting undeployed and let the user decide.

## Landing a change: PR only, `main` is protected

`git push origin main` is **rejected** — branch protection requires the
`test` check, and it applies to admins too. Every change goes:

```powershell
git switch -c <branch>          # never commit straight onto main
git push -u origin <branch>
gh pr create --fill
gh pr checks --watch            # wait for CI; do not merge on a local green
gh pr merge --squash --delete-branch
```

A green local `pytest` is **not** evidence about CI. On 2026-08-19 two
pushes landed on `main` with CI red because local was green: three
analysis scripts were named `test_*.py`, pytest collected them, and they
imported `numpy`/`sklearn` — present on the dev machine from unrelated
work, deliberately absent from `environment.yml`. Same command, opposite
result. CI's environment is the one that resembles the image.

(`pytest.ini` pins `testpaths = tests` now, so that specific trap is
closed, but the general point stands: the dev machine's environment
drifts and CI's doesn't.)

If CI is genuinely wrong and the change must land, turn protection off
explicitly rather than working around it — and say so:
`gh api -X DELETE repos/nankma/auguring/branches/main/protection`.
