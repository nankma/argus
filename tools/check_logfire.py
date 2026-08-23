"""
Post-deployment check: confirms Logfire is actually RECEIVING traces from
the deployed bot -- the Logfire counterpart of check_telemetry.py.

Same reasoning as that script, and the same incident behind it: on
2026-08-16 PHOENIX_ENABLED/PHOENIX_ENDPOINT silently fell off a
`docker run` and nothing noticed for weeks, because a bot with no
telemetry looks exactly like a bot with telemetry until you go looking.
Logfire can fail the same way and one more besides -- `LOGFIRE_ENABLED`
unset, the Vault secret unresolved, or the token minted in the wrong
region -- and every one of those is silent: the OTLP HTTP exporter logs
failures rather than raising, so the bot starts cleanly and exports
nothing.

Reuses check_telemetry.py's trigger (SSH + curl to test_api on the bot VM,
no local tunnel -- see that file for why) and then queries Logfire's own
API for a span newer than the moment the message was sent. Both halves
matter: a span from before the restart proves nothing about the running
container.

    python tools/check_logfire.py --bot-vm ubuntu@<ip> --bot-key <key>

Needs LOGFIRE_API_KEY in the environment -- the same v2 token used for
export also reads, so there is no second credential.
"""

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.check_telemetry import send_test_message  # noqa: E402

# Imported, not repeated: a check that filters on a different name
# than the exporter sets is a check that always passes or always
# fails, and 2026-08-23 was the always-fails version.
from agent import SERVICE_NAME  # noqa: E402
LOGFIRE_HOSTS = {"us": "https://logfire-us.pydantic.dev",
                 "eu": "https://logfire-eu.pydantic.dev"}


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

    token = os.environ.get("LOGFIRE_API_KEY")
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
