"""
Data Access Layer for per-cycle push outcomes -- the incident-monitoring
data news_push.py records and docs/plans/incident-monitoring-plan.md's
criteria read. See git history (pre-refactor users_db.py) for the full
per-outcome reasoning.
"""

from datetime import datetime, timedelta, timezone

from app_settings import get_settings
from storage import get_storage

PUSH_DELIVERED = "delivered"
PUSH_NOTHING_NEW = "nothing_new"
PUSH_NOT_RELEVANT = "not_relevant"
PUSH_BLOCKED = "blocked"
PUSH_NO_INTERESTS = "no_interests"
PUSH_CHAT_NOT_FOUND = "chat_not_found"
PUSH_MODEL_ERROR = "model_error"
PUSH_CYCLE_FAILED = "cycle_failed"
PUSH_DISABLED = "disabled"

# Outcomes meaning an LLM was actually called to write a digest -- the
# denominator of the delivered/generated ratio (criterion 3,
# docs/plans/incident-monitoring-plan.md). nothing_new/no_interests are
# excluded: they return before write_push_digest, so no digest was paid
# for, and counting them would let idle subscribers hide a collapsed
# delivery rate.
PUSH_GENERATED_OUTCOMES = frozenset({
    PUSH_DELIVERED,
    PUSH_NOT_RELEVANT,
    PUSH_BLOCKED,
    PUSH_CHAT_NOT_FOUND,
    PUSH_MODEL_ERROR,
})

PUSH_OUTCOME_RETENTION_DAYS = get_settings().resolved("storage.push_outcomes_ttl_days", default=90)


def record_push_outcome(chat_id: int, outcome: str, recorded_at: datetime, detail: str | None = None) -> None:
    """Call this from news_push._record rather than directly -- the point
    is that the human log line and this row are written together."""
    ts = recorded_at if recorded_at.tzinfo else recorded_at.replace(tzinfo=timezone.utc)
    get_storage().record_push_outcome(chat_id, outcome, ts.isoformat(), detail)


def push_outcome_counts(since: datetime) -> dict[str, int]:
    """{outcome: count} across all subscribers since `since`."""
    cutoff = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    rows = get_storage().push_outcome_counts(cutoff.isoformat())
    return {outcome: count for outcome, count in rows}


def push_delivery_ratio(since: datetime) -> tuple[int, int]:
    """(delivered, generated) since `since` -- criterion 3's numerator and
    denominator, derived from the same counts so they can't drift apart.
    (0, 0) means nothing was generated -- callers must treat that as "no
    opinion", not a 0% delivery rate."""
    counts = push_outcome_counts(since)
    generated = sum(n for outcome, n in counts.items() if outcome in PUSH_GENERATED_OUTCOMES)
    return counts.get(PUSH_DELIVERED, 0), generated


def recent_outcomes_for(chat_id: int, limit: int = 20) -> list[str]:
    """This subscriber's most recent outcomes, newest first."""
    return get_storage().recent_outcomes_for(chat_id, limit)


def consecutive_chat_not_found(chat_id: int, limit: int = 50) -> int:
    """How many `chat_not_found` outcomes have piled up since the last
    proof this chat was reachable. Only `delivered` breaks the streak;
    every other outcome is skipped rather than treated as either -- see
    git history for the full reasoning."""
    streak = 0
    for outcome in recent_outcomes_for(chat_id, limit):
        if outcome == PUSH_DELIVERED:
            break
        if outcome == PUSH_CHAT_NOT_FOUND:
            streak += 1
    return streak


def prune_push_outcomes(now: datetime, days: int = PUSH_OUTCOME_RETENTION_DAYS) -> int:
    cutoff = (now - timedelta(days=days)).isoformat()
    return get_storage().prune_push_outcomes(cutoff)
