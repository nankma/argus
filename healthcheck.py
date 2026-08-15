"""
Lightweight liveness alerting for the two periodic jobs (news_ingest.py,
news_push.py). See docs/telemetry-and-testing-plan.md's "Currently NOT
connected on the live deployment" finding: Phoenix telemetry silently
disconnected at some point and nothing noticed for an unknown period,
because nothing in this project was ever built to notice a silent
failure and say so. This module is the answer to "why wasn't there an
alert" for the narrower, always-answerable question it CAN check purely
from inside the running bot process, without depending on any external
service's own reachability: are the two periodic jobs still ticking at
all?

This does NOT check whether Phoenix itself is reachable -- that needs an
outside-in check against a service this process doesn't control, which
is what tools/check_telemetry.py is for (run manually/post-deploy, not
as a standing in-process job). What this module CAN answer cheaply and
continuously is whether news_ingest/news_push have stopped running
entirely (a crashed job, an unhandled exception outside the per-item try/
except, the scheduler itself dying) -- a different, complementary
failure class from "Phoenix is down but the bot is fine."

Deliberately NOT a full incident-management system: no severity levels,
no escalation policy, no on-call rotation, no dedup-by-fingerprint. This
is a solo/personal project (see CLAUDE.md) -- admin_bot.py's channel
already IS the paging mechanism this project has (used today for
access-request approvals); this reuses that channel rather than standing
up new infrastructure for a one-admin deployment.
"""

from datetime import datetime, timezone

from telegram import Bot

import users_db

INGEST_TICK_KEY = "__ingest_tick__"
PUSH_TICK_KEY = "__push_tick__"

# Both jobs tick every 15 minutes (bot.INGEST_TICK_SECONDS/
# PUSH_TICK_SECONDS) regardless of whether any individual source/
# subscriber is due -- a generous multiple of that (1h) means a single
# slow cycle or a missed tick or two doesn't false-positive; only a job
# that's genuinely stopped running crosses this.
STALE_THRESHOLD_HOURS = 1

_JOBS = ((INGEST_TICK_KEY, "news_ingest"), (PUSH_TICK_KEY, "news_push"))


def check_health(now: datetime | None = None) -> list[str]:
    """Returns human-readable problem descriptions, empty if healthy.
    Each entry is real text (not just a boolean) so an alert can say what
    is actually wrong, not just "something is wrong somewhere"."""
    now = now or datetime.now(timezone.utc)
    problems = []
    for key, label in _JOBS:
        last = users_db.get_source_last_pulled_at(key)
        if last is None:
            problems.append(f"{label} has never ticked since this container started")
            continue
        elapsed_hours = (now - last).total_seconds() / 3600
        if elapsed_hours > STALE_THRESHOLD_HOURS:
            problems.append(f"{label} last ticked {elapsed_hours:.1f}h ago (expected every ~15min)")
    return problems


async def run_health_check(admin_bot_token: str, admin_chat_id: int, now: datetime | None = None) -> None:
    """One scheduler tick: compares the current problem set against the
    last one actually alerted on (users_db.get_health_state), and only
    sends a Telegram message to the admin on a CHANGE -- a new problem
    appearing, or an existing one clearing. An unchanged, still-broken
    state doesn't re-alert every tick (that would just be noise for a
    known, ongoing issue), but recovery is reported too: "it's fixed now"
    is real information, not just silence once the alert stops."""
    current = check_health(now)
    previous = users_db.get_health_state()
    if set(current) == set(previous):
        return
    users_db.set_health_state(current)
    if current:
        text = "⚠️ Health check found problem(s):\n" + "\n".join(f"- {p}" for p in current)
    else:
        text = "✅ Health check recovered -- all periodic jobs are ticking normally again."
    await Bot(token=admin_bot_token).send_message(chat_id=admin_chat_id, text=text)
