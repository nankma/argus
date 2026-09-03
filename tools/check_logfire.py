"""
Post-deployment check: confirms Logfire is actually RECEIVING traces from
the deployed bot -- not just that the bot process started without error.

A real incident, 2026-08-16 (back when Phoenix was the telemetry backend):
its env vars silently fell off a `docker run` and nothing noticed for
weeks, because a bot with no telemetry looks exactly like a bot with
telemetry until you go looking. Logfire can fail the same way and one
more besides -- `LOGFIRE_ENABLED` unset, the Vault secret unresolved, or
the token minted in the wrong region -- and every one of those is
silent: the OTLP HTTP exporter logs failures rather than raising, so the
bot starts cleanly and exports nothing.

Runs entirely via SSH `curl` on the bot VM, not a local SSH tunnel --
deliberately avoids the tunnel-reliability issue documented in
docs/reference/local-testing-api-plan.md's "Resolved issue" section
(accumulated session state, dual-stack ambiguity). This check only needs
one one-shot request/response round trip, not a sustained tunnel.

Two steps: (1) SSH to the bot VM, POST a message to test_api.py's
/test_message endpoint (see docs/reference/local-testing-api-plan.md --
requires ENABLE_TEST_API=true on that deploy), recording the timestamp
right before sending; (2) query Logfire's own API for a span newer than
that timestamp. Both halves matter: a span from before the restart
proves nothing about the running container.

    python tools/check_logfire.py --bot-vm ubuntu@<ip> --bot-key <key>

Needs LOGFIRE_API_KEY in the environment -- the same v2 token used for
export also reads, so there is no second credential.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Imported, not repeated: a check that filters on a different name
# than the exporter sets is a check that always passes or always
# fails, and 2026-08-23 was the always-fails version.
from agent import SERVICE_NAME  # noqa: E402
from app_settings import resolved_optional  # noqa: E402
LOGFIRE_HOSTS = {"us": "https://logfire-us.pydantic.dev",
                 "eu": "https://logfire-eu.pydantic.dev"}
TEST_API_PORT = 8765


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
    (`-p 127.0.0.1:8765:8765`, see docs/reference/local-testing-api-plan.md's
    security model), so 127.0.0.1 is correct from the VM's own shell."""
    payload = json.dumps({"chat_id": chat_id, "text": text})
    # POSIX single-quote escaping: '\'' for a literal quote inside a
    # single-quoted string, not just "hope the text never contains one" --
    # a real bug this project hit once with an apostrophe in a test message.
    escaped_payload = payload.replace("'", "'\\''")
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


def query_url(token: str) -> str:
    """Region comes from the token's own prefix, so the endpoint cannot
    disagree with the credential -- same rule as agent.logfire_traces_endpoint."""
    for region, host in LOGFIRE_HOSTS.items():
        if f"_{region}_" in token[:12]:
            return f"{host}/v2/query"
    raise SystemExit(f"cannot tell the Logfire region from token prefix {token[:11]!r}")


def spans_since(token: str, since: datetime, timeout: int) -> int:
    sql = (f"SELECT count(*) AS n FROM records WHERE service_name = '{SERVICE_NAME}' "
           f"AND start_timestamp > '{since.isoformat()}'")
    r = requests.post(
        query_url(token),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"sql": sql, "min_timestamp": (since - timedelta(hours=1)).isoformat()},
        timeout=timeout,
    )
    if r.status_code == 429:
        # A rate limit is not a telemetry failure. Treating it as one would
        # make a deploy report "Logfire is broken" when the only problem is
        # that this script asked too often -- the false negative that makes
        # a check worth ignoring.
        return -1
    if r.status_code != 200:
        print(f"\nFAIL: could not query Logfire ({r.status_code}): {r.text[:160]}")
        raise SystemExit(1)
    rows = r.json().get("data", [])
    return rows[0]["n"] if rows else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bot-vm", required=True, help="e.g. ubuntu@<bot-vm-ip>")
    parser.add_argument("--bot-key", required=True, help="path to the bot VM's SSH key")
    parser.add_argument("--chat-id", type=int, default=999)
    parser.add_argument("--timeout", type=int, default=90)
    args = parser.parse_args()

    token = resolved_optional("telemetry.logfire-api-key")
    if not token:
        print("FAIL: LOGFIRE_API_KEY is not set -- cannot query Logfire.")
        raise SystemExit(1)

    # A moment BEFORE the trigger, so an older span cannot be mistaken for
    # proof that the running container is exporting.
    started = datetime.now(timezone.utc) - timedelta(seconds=5)

    print("1. Sending a test message through the bot...")
    if not send_test_message(args.bot_vm, args.bot_key, args.chat_id,
                             "what is new in AI hardware", args.timeout):
        print("\nFAIL: bot VM / test_api unreachable -- cannot tell whether telemetry works.")
        raise SystemExit(1)

    print("2. Polling Logfire for a span newer than the message...")
    deadline = time.time() + args.timeout
    throttled = 0
    while time.time() < deadline:
        count = spans_since(token, started, timeout=30)
        if count > 0:
            print(f"\nOK: {count} span(s) reached Logfire within {args.timeout}s.")
            return
        if count < 0:
            throttled += 1
            print(f"   rate limited, backing off ({throttled})")
            time.sleep(20)
            continue
        # 15s, not 5: ingestion lag is ~5s and the query API is rate
        # limited per minute, so polling harder makes this flakier rather
        # than faster.
        time.sleep(15)

    print(f"\nFAIL: the bot responded and Logfire is reachable, but zero spans "
          f"landed within {args.timeout}s.")
    print("Likely LOGFIRE_ENABLED missing from the docker run, the Vault secret")
    print("unresolved, or a token from the other region -- all of which are silent:")
    print("the OTLP HTTP exporter logs failures instead of raising.")
    raise SystemExit(1)


if __name__ == "__main__":
    main()
