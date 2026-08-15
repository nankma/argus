"""
Post-deployment check: confirms Phoenix is actually RECEIVING traces from
the deployed bot -- not just that the bot process started without error.
See docs/telemetry-and-testing-plan.md's "Currently NOT connected on the
live deployment" finding: PHOENIX_ENABLED/PHOENIX_ENDPOINT silently
dropped off a `docker run` at some point between a previously-verified-
working deploy and now, and nothing caught it until someone went looking
for unrelated data weeks later. This script is what would have caught
that regression on the very next deploy instead.

Runs entirely via SSH `curl` on each VM, not a local SSH tunnel --
deliberately avoids the tunnel-reliability issue documented in
docs/local-testing-api-plan.md's "Resolved issue" section (accumulated
session state, dual-stack ambiguity). This check only needs two one-shot
request/response round trips, not a sustained tunnel, so there's no
reason to take on that failure mode here.

Two steps:
  1. SSH to the bot VM, POST a message to test_api.py's /test_message
     endpoint (see docs/local-testing-api-plan.md -- requires
     ENABLE_TEST_API=true on that deploy). Records the timestamp right
     before sending.
  2. SSH to the Phoenix VM, query its GraphQL API (System API Key auth,
     see docs/observability-and-debugging.md) for spans in the
     myfirstagent project with startTime after that timestamp.

Exits 0 and prints "OK" with the span count if at least one matching
span is found; exits 1 with a specific, distinguishable reason otherwise
(bot unreachable vs. Phoenix unreachable vs. zero spans landed within
the timeout are three different failures, not one generic "failed" --
each points at a different thing to go check).

Usage:
    python tools/check_telemetry.py \
        --bot-vm ubuntu@<bot-vm-ip> --bot-key <path-to-bot-vm-key> \
        --phoenix-vm ubuntu@<phoenix-vm-ip> --phoenix-key <path-to-phoenix-vm-key> \
        [--phoenix-project-id UHJvamVjdDoy] [--timeout 30] [--poll-interval 3]

Verified live end-to-end 2026-08-16, after PHOENIX_ENABLED/PHOENIX_ENDPOINT
were restored on the bot's docker run: 35 spans confirmed landed in
Phoenix within 90s of a real test message. Two real bugs were caught and
fixed by that first live run, not found by inspection: (1) the default
test message ("What's new with OpenAI?") has an apostrophe, which broke
out of the single-quoted `-d '...'` shell string on the remote host --
fixed by POSIX-escaping the payload (`'` -> `'\''`) rather than assuming
future messages won't contain one; (2) curl's own `-m` was hardcoded to
20s regardless of --timeout, which was too short for a real news_query
call once Phoenix's SimpleSpanProcessor started adding synchronous
per-span export latency to every LLM/tool call on this project's tiny
(1/8 OCPU) VM -- fixed to derive curl's timeout from --timeout instead.
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

DEFAULT_PHOENIX_PROJECT_ID = "UHJvamVjdDoy"  # base64 of "Project:2" -- see docs/observability-and-debugging.md
TEST_API_PORT = 8765
PHOENIX_PORT = 6006


def _ssh_run(host: str, key: str, remote_command: str, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-i", key, "-o", "ConnectTimeout=15", "-o", "BatchMode=yes", host, remote_command],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def send_test_message(bot_vm: str, bot_key: str, chat_id: int, text: str, timeout: int) -> bool:
    """Runs curl ON the bot VM (not through a local tunnel) against its
    own loopback -- test_api.py binds 0.0.0.0 inside the container, and
    the container publishes to the VM's own loopback only
    (`-p 127.0.0.1:8765:8765`, see docs/local-testing-api-plan.md's
    security model), so 127.0.0.1 is correct from the VM's own shell."""
    payload = json.dumps({"chat_id": chat_id, "text": text})
    # Real bug, caught live: the default test message ("What's new with
    # OpenAI?") has an apostrophe, which breaks out of the single-quoted
    # `-d '...'` shell string once it reaches the remote bash -- POSIX
    # single-quote escaping needs '\'' for a literal quote inside a
    # single-quoted string, not just "hope the text never contains one."
    escaped_payload = payload.replace("'", "'\\''")
    # curl's own -m must actually use `timeout`, not a hardcoded value --
    # a real bug caught live: this was hardcoded to 20s regardless of
    # --timeout, so a real news_query call (search_news across ~20
    # sources, then an LLM synthesis call, each span now synchronously
    # exported to Phoenix via SimpleSpanProcessor -- see the collector's
    # own startup banner -- adds real per-call latency on a 1/8-OCPU VM)
    # timed out well before the script's own --timeout ever kicked in.
    curl_timeout = max(timeout - 5, 10)
    remote_command = (
        f"curl -sS -m {curl_timeout} -X POST http://127.0.0.1:{TEST_API_PORT}/test_message "
        f"-H 'Content-Type: application/json' -d '{escaped_payload}'"
    )
    result = _ssh_run(bot_vm, bot_key, remote_command, timeout)
    if result.returncode != 0:
        print(f"FAIL: could not reach the bot VM's test_api endpoint.\n  stderr: {result.stderr.strip()}")
        return False
    print(f"  bot response: {result.stdout.strip()[:200]}")
    return True


def fetch_phoenix_api_key(bot_vm: str, bot_key: str, timeout: int) -> str | None:
    """The System API Key isn't set on the Phoenix VM itself -- it's a
    credential the CLIENT (the bot) needs to push traces, resolved from
    Vault into the bot container's own PHOENIX_API_KEY env var by
    docker-entrypoint.sh. Reads it back out via the exact technique
    docs/observability-and-debugging.md already documents: run the
    entrypoint script with `printenv` substituted for the real command,
    so secrets get fetched and resolved but nothing else starts."""
    remote_command = "sudo docker exec myfirstagent-bot bash -c 'cd /app && ./docker-entrypoint.sh printenv PHOENIX_API_KEY'"
    result = _ssh_run(bot_vm, bot_key, remote_command, timeout)
    key = result.stdout.strip()
    if result.returncode != 0 or not key:
        print(f"  could not fetch PHOENIX_API_KEY from the bot VM.\n  stderr: {result.stderr.strip()}")
        return None
    return key


def count_recent_spans(
    phoenix_vm: str, phoenix_key: str, api_key: str, project_id: str, since: datetime, timeout: int
) -> int | None:
    """Returns the number of spans in the project with startTime >=
    `since`, or None if the query itself failed (VM unreachable, auth
    failure, malformed response) -- distinct from a successful query that
    legitimately found zero spans. Runs the query FROM the Phoenix VM
    itself: the GraphQL endpoint (port 6006, same as the human web UI)
    is not opened to the private subnet the way the OTLP ingestion port
    (4317) is -- see docs/security-plan.md finding 17 -- so this can't
    be curled directly from the bot VM or a local machine, only via SSH
    onto the Phoenix VM."""
    query = {
        "query": (
            "query($id: ID!, $start: DateTime!, $end: DateTime!) { "
            "node(id: $id) { ... on Project { "
            "spans(first: 200, timeRange: {start: $start, end: $end}, sort: {col: startTime, dir: desc}) "
            "{ edges { node { name startTime } } } } } }"
        ),
        "variables": {
            "id": project_id,
            "start": since.isoformat(),
            "end": (since + timedelta(minutes=5)).isoformat(),
        },
    }
    remote_command = (
        f"curl -sS -m 20 -X POST http://127.0.0.1:{PHOENIX_PORT}/graphql "
        "-H 'Content-Type: application/json' "
        f"-H 'Authorization: Bearer {api_key}' "
        f"-d '{json.dumps(query)}'"
    )
    result = _ssh_run(phoenix_vm, phoenix_key, remote_command, timeout)
    if result.returncode != 0 or not result.stdout.strip():
        print(f"  phoenix query failed.\n  stderr: {result.stderr.strip()}")
        return None
    try:
        body = json.loads(result.stdout)
        edges = body["data"]["node"]["spans"]["edges"]
    except (json.JSONDecodeError, KeyError, TypeError):
        print(f"  phoenix returned an unexpected response shape: {result.stdout.strip()[:300]}")
        return None
    return len(edges)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bot-vm", required=True, help="e.g. ubuntu@<bot-vm-ip>")
    parser.add_argument("--bot-key", required=True, help="path to the bot VM's SSH key")
    parser.add_argument("--phoenix-vm", required=True, help="e.g. ubuntu@<phoenix-vm-ip>")
    parser.add_argument("--phoenix-key", required=True, help="path to the Phoenix VM's SSH key")
    parser.add_argument("--phoenix-project-id", default=DEFAULT_PHOENIX_PROJECT_ID)
    parser.add_argument("--chat-id", type=int, default=999, help="test_api.py chat_id to use")
    parser.add_argument(
        "--timeout",
        type=int,
        default=90,
        help="total seconds to wait for a span to land (also bounds the test message's own curl call -- "
        "a real news_query call needs real time: search_news across ~20 sources plus an LLM synthesis "
        "call, each now paying synchronous per-span Phoenix export latency on top, confirmed live to "
        "need close to 90s on this project's 1/8-OCPU VM, not the 20-30s that might look sufficient)",
    )
    parser.add_argument("--poll-interval", type=int, default=5, help="seconds between Phoenix polls")
    args = parser.parse_args()

    print("1. Fetching the Phoenix System API Key from the bot VM...")
    api_key = fetch_phoenix_api_key(args.bot_vm, args.bot_key, args.timeout)
    if api_key is None:
        print("\nFAIL: could not resolve the Phoenix API key -- can't query Phoenix at all.")
        return 1

    since = datetime.now(timezone.utc)
    print("2. Sending a test message through the bot's test_api endpoint...")
    if not send_test_message(args.bot_vm, args.bot_key, args.chat_id, "What's new with OpenAI?", args.timeout):
        print("\nFAIL: bot VM / test_api unreachable -- can't tell whether telemetry works at all.")
        return 1

    print("3. Polling Phoenix for a new span...")
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        count = count_recent_spans(
            args.phoenix_vm, args.phoenix_key, api_key, args.phoenix_project_id, since, args.timeout
        )
        if count is None:
            print("\nFAIL: could not query Phoenix at all -- collector VM unreachable, or auth/query error.")
            return 1
        if count > 0:
            print(f"\nOK: {count} span(s) landed in Phoenix within {args.timeout}s of the test message.")
            return 0
        time.sleep(args.poll_interval)

    print(f"\nFAIL: bot responded and Phoenix is reachable, but zero spans landed within {args.timeout}s.")
    print("This is the specific failure mode from the 2026-08-16 finding: PHOENIX_ENABLED/PHOENIX_ENDPOINT")
    print("missing from the bot's docker run, or the OTLP export path (port 4317) blocked/misconfigured.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
