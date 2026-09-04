"""
Data Access Layer for subscriber accounts, interests, push preferences,
and language -- the business-shaped functions bot.py/admin_bot.py/
combined_bot.py/agent.py/news_push.py/message_archive.py/test_api.py call.
Storage-technology-agnostic: every function here calls storage.get_storage()
for the actual persistence and never touches SQL -- see storage/__init__.py
and docs/plans/data-layer-plan.md for the layering this package is part of.
"""

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from app_settings import get_settings
from storage import get_storage

PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"

DEFAULT_PUSH_INTERVAL_HOURS = get_settings().resolved("subscription.default_interval_hours", default=24)
MIN_PUSH_INTERVAL_HOURS = get_settings().resolved("subscription.min_interval_hours", default=1)
# How long a sent article's link is remembered per subscriber -- pruned by
# AGE, not by count, and deliberately longer than news_cache's TTL. See
# git history (pre-refactor users_db.py) for the full incident/reasoning.
PUSHED_LINK_RETENTION_HOURS = get_settings().resolved("subscription.pushed_link_retention_hours", default=72)
# How many interests one subscriber may follow -- set generously;
# MAX_INTERESTS_PER_PUSH in news_push.py is what actually bounds noise.
MAX_INTERESTS = get_settings().resolved("subscription.max_interests", default=10)


def get_status(chat_id: int) -> str | None:
    return get_storage().get_status(chat_id)


def request_access(chat_id: int, username: str | None, first_name: str | None) -> None:
    """Insert a pending request. A no-op if this chat_id already has a
    row (pending, approved, or denied) -- re-messaging shouldn't reset a
    decision back to pending."""
    get_storage().request_access(chat_id, username, first_name, PENDING, datetime.now().isoformat())


def decide(chat_id: int, approved: bool) -> None:
    get_storage().decide(chat_id, APPROVED if approved else DENIED, datetime.now().isoformat())


def list_pending() -> list[tuple]:
    return get_storage().list_pending(PENDING)


def get_interests(chat_id: int) -> list[str]:
    raw = get_storage().get_interests(chat_id)
    return json.loads(raw) if raw else []


def mark_test_account(chat_id: int) -> None:
    """Flags a subscriber as created by test_api.py, so push cycles skip
    it -- see git history (pre-refactor users_db.py) for the 2026-08-21
    abandoned-test-account incident this exists to prevent."""
    get_storage().mark_test_account(chat_id, APPROVED, datetime.now().isoformat())


def set_interests(chat_id: int, interests: list[str]) -> None:
    """Upserts -- a chat_id may not have a subscribers row yet (e.g. the
    admin, who bypasses request_access() entirely)."""
    get_storage().set_interests(chat_id, APPROVED, datetime.now().isoformat(), json.dumps(interests))


_DUPLICATE_TOPIC_SIMILARITY_THRESHOLD = 0.7


def _topic_words(topic: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", topic.lower()))


def _is_duplicate_topic(a: str, b: str) -> bool:
    """Word-set Jaccard similarity, not exact/case-insensitive match --
    catches near-duplicate LLM-generated phrasing while still treating
    genuinely different topics sharing one word as distinct."""
    words_a, words_b = _topic_words(a), _topic_words(b)
    if not words_a or not words_b:
        return False
    union = words_a | words_b
    return len(words_a & words_b) / len(union) >= _DUPLICATE_TOPIC_SIMILARITY_THRESHOLD


def add_interest(chat_id: int, topic: str) -> list[str]:
    """Adds `topic` if not already present (fuzzy match). Raises ValueError
    at MAX_INTERESTS rather than silently dropping the addition -- same
    convention as set_push_interval_hours."""
    interests = get_interests(chat_id)
    if any(_is_duplicate_topic(t, topic) for t in interests):
        return interests
    if len(interests) >= MAX_INTERESTS:
        raise ValueError(
            f"you already follow {len(interests)} interests, which is the "
            f"maximum of {MAX_INTERESTS} -- remove one first")
    interests.append(topic)
    set_interests(chat_id, interests)
    return interests


def interests_by_staleness(chat_id: int, interests: list[str]) -> list[str]:
    """`interests`, reordered longest-un-pushed first. Never-pushed topics
    lead, in their existing order."""
    if not interests:
        return []
    rows = get_storage().get_interest_push_state(chat_id)
    seen = {r[0]: r[1] for r in rows}
    return sorted(interests, key=lambda t: seen.get(t, ""))


def mark_interest_pushed(chat_id: int, topic: str, when: datetime) -> None:
    get_storage().mark_interest_pushed(chat_id, topic, when.isoformat())


def remove_interest(chat_id: int, topic: str) -> list[str]:
    """Removes `topic` (case-insensitive match) if present. Returns the
    resulting full list."""
    interests = [t for t in get_interests(chat_id) if t.lower() != topic.lower()]
    set_interests(chat_id, interests)
    return interests


def get_push_enabled(chat_id: int) -> bool:
    raw = get_storage().get_push_enabled(chat_id)
    return bool(raw) if raw is not None else False


def set_push_enabled(chat_id: int, enabled: bool) -> None:
    get_storage().set_push_enabled(chat_id, APPROVED, datetime.now().isoformat(), int(enabled))


def get_push_interval_hours(chat_id: int) -> int:
    raw = get_storage().get_push_interval_hours(chat_id)
    return raw if raw is not None else DEFAULT_PUSH_INTERVAL_HOURS


def set_push_interval_hours(chat_id: int, hours: int) -> None:
    """Suggested presets are 24/12/6/4h, but any integer >=
    MIN_PUSH_INTERVAL_HOURS is accepted."""
    if hours < MIN_PUSH_INTERVAL_HOURS:
        raise ValueError(f"push interval must be at least {MIN_PUSH_INTERVAL_HOURS} hour(s)")
    get_storage().set_push_interval_hours(chat_id, APPROVED, datetime.now().isoformat(), hours)


def _parse_pushed_links(raw: str | None, now: datetime | None = None) -> dict[str, str]:
    """{link: iso_timestamp} for links still inside the retention window.
    Accepts the pre-2026-08-19 bare-list format too, treated as sent
    `now` (a full fresh retention window) -- re-sending a seen article is
    worse than remembering it slightly too long."""
    if not raw:
        return {}
    now = now or datetime.now(timezone.utc)
    data = json.loads(raw)
    if isinstance(data, list):  # legacy format
        return {link: now.isoformat() for link in data}
    cutoff = now - timedelta(hours=PUSHED_LINK_RETENTION_HOURS)
    kept = {}
    for link, sent_at in data.items():
        try:
            if datetime.fromisoformat(sent_at) > cutoff:
                kept[link] = sent_at
        except (TypeError, ValueError):
            kept[link] = now.isoformat()  # unparseable -- keep, don't risk a resend
    return kept


def get_pushed_links(chat_id: int, now: datetime | None = None) -> list[str]:
    raw = get_storage().get_pushed_links(chat_id)
    return list(_parse_pushed_links(raw, now))


def get_last_push_at(chat_id: int) -> datetime | None:
    raw = get_storage().get_last_push_at(chat_id)
    return datetime.fromisoformat(raw) if raw else None


def record_push(chat_id: int, article_links: list[str], pushed_at: datetime) -> None:
    """Advances the dedup state after a digest is sent (or a due check
    finds nothing new). `article_links` must be links that genuinely
    appeared in the delivered digest, not the candidate list."""
    now = pushed_at if pushed_at.tzinfo else pushed_at.replace(tzinfo=timezone.utc)
    raw = get_storage().get_pushed_links(chat_id)
    merged = _parse_pushed_links(raw, now)
    for link in article_links:
        merged[link] = now.isoformat()
    get_storage().record_push(chat_id, APPROVED, datetime.now().isoformat(), pushed_at.isoformat(), json.dumps(merged))


def list_push_enabled_subscribers() -> list[dict]:
    """Approved, push-enabled subscribers with everything news_push.py's
    scheduler needs. Excludes is_test accounts (mark_test_account)."""
    rows = get_storage().list_push_enabled_subscribers(APPROVED)
    result = []
    for chat_id, interests_json, interval_hours, last_push_at, pushed_links_json, language, restricted in rows:
        result.append({
            "chat_id": chat_id,
            "interests": json.loads(interests_json) if interests_json else [],
            "push_interval_hours": interval_hours if interval_hours is not None else DEFAULT_PUSH_INTERVAL_HOURS,
            "last_push_at": datetime.fromisoformat(last_push_at) if last_push_at else None,
            "pushed_links": list(_parse_pushed_links(pushed_links_json)),
            "language": language,
            "restricted_sources_enabled": bool(restricted),
        })
    return result


def get_language(chat_id: int) -> str | None:
    return get_storage().get_language(chat_id)


def set_language(chat_id: int, language: str | None) -> None:
    """`language` is free text, not a constrained code list. None/empty
    clears the preference, falling back to matching the user's own
    message language."""
    get_storage().set_language(chat_id, APPROVED, datetime.now().isoformat(), language or None)


def get_restricted_sources_enabled(chat_id: int) -> bool:
    """Per-user gate on search_news's use of RESTRICTED_SOURCES. Defaults
    False; bot.py grants it to the admin's own chat_id at startup."""
    return bool(get_storage().get_restricted_sources_enabled(chat_id))


def set_restricted_sources_enabled(chat_id: int, enabled: bool) -> None:
    get_storage().set_restricted_sources_enabled(chat_id, APPROVED, datetime.now().isoformat(), int(enabled))


def external_id(chat_id: int) -> str:
    """A stable, opaque id for this subscriber, minted once and stored --
    chat_id is a real Telegram identifier that should never reach
    telemetry. Falls back to a deterministic value for a chat_id with no
    subscriber row."""
    storage = get_storage()
    existing = storage.get_external_id(chat_id)
    if existing:
        return existing
    new_id = "sub_" + secrets.token_hex(6)
    if storage.set_external_id_if_null(chat_id, new_id):
        return new_id
    # No row to attach it to (a chat that was never a subscriber).
    db_key = get_settings().resolved("storage.database.sqlite.path", default="") or \
        get_settings().resolved("storage.database.postgres.dbname", default="")
    return "anon_" + hashlib.sha256(f"{db_key}:{chat_id}".encode()).hexdigest()[:12]


def list_all_interests() -> list[str]:
    """Distinct interests across every subscriber -- used by
    news_ingest.py to query budget-capped sources against real topics."""
    rows = get_storage().list_all_interests_raw()
    seen = []
    for (interests_json,) in rows:
        for topic in json.loads(interests_json):
            if topic not in seen:
                seen.append(topic)
    return seen
