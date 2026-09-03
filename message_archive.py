"""
Archives every message actually sent to a subscriber -- timestamp,
recipient, kind, topic, and the text delivered -- for after-the-fact
inspection (a real user requirement, not an enhancement). Mirrors
news_cache.py's storage.news_archive_dir pattern (settings.yml, one file
per item, pruned by age) but always-on rather than optional:
news_archive_dir defaults to unset (archiving off), message_archive_dir
defaults to a local relative directory (archiving on), same convention
as news_cache_dir -- this is meant to always be recording, not to
degrade gracefully to "off" when unconfigured.

In production, storage.message_archive_dir points inside the same
persistent named Docker volume (myfirstagent-data:/data, see
local-infra/infrastructure.yaml) that already holds subscribers.db and
news_cache -- survives a redeploy, unlike docker logs.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import users_db
from app_settings import get_settings
from telemetry import EventLogger, get_event_logger
from telemetry_providers import Level

_events: EventLogger = get_event_logger("argus.message_archive")

# required=True, no default -- archiving is always on, so an absent
# key here is a deployment mistake, not a legitimate "unset" state. See
# docs/standaloneplan/01-settings-migration.md's "Migration methodology".
ARCHIVE_DIR = get_settings().resolved("storage.message_archive_dir.path", required=True)

DEFAULT_TTL_DAYS = get_settings().resolved("storage.message_archive_dir.ttl_days", default=7)

# strftime("%Y%m%dT%H%M%S%f") is always exactly this many characters
# (4+2+2+1+2+2+2+6) -- a fixed-width prefix lets prune_message_archive
# recover the timestamp by slicing, not by splitting on a delimiter.
# Splitting would be wrong: users_db.external_id's own values contain
# underscores ("sub_"/"anon_" + hex), so "up to the first _" would cut
# the timestamp short for some recipients and not others.
_TIMESTAMP_LEN = 21


def _archive_path(chat_id: int, now: datetime) -> Path:
    timestamp = now.strftime("%Y%m%dT%H%M%S%f")
    assert len(timestamp) == _TIMESTAMP_LEN
    return Path(ARCHIVE_DIR) / f"{timestamp}_{users_db.external_id(chat_id)}.json"


def archive_message(chat_id: int, kind: str, text: str, topic: str | None = None,
                     now: datetime | None = None) -> None:
    """Writes one JSON file per sent message: timestamp, recipient (opaque
    id via users_db.external_id -- same reasoning as news_push._record's
    span attribute: chat_id is a real Telegram identifier and this file
    may be inspected more casually than the DB), kind
    ('push_digest' | 'chat_reply'), topic (the subscriber's interest for
    a push digest; the guardrail-classified category for a chat reply --
    the closest analog, since an interactive reply has no fixed topic),
    and the text actually sent -- what was delivered, including any
    BadRequest strip-to-plain fallback (bot.py), not the raw pre-fallback
    model output (news_push's html_validation_exhausted span already
    covers that diagnosis need).

    Fails open: archiving is a convenience for after-the-fact inspection,
    never something that should raise over a message that was already
    genuinely sent."""
    now = now or datetime.now(timezone.utc)
    record = {
        "timestamp": now.isoformat(),
        "recipient": users_db.external_id(chat_id),
        "kind": kind,
        "topic": topic,
        "text": text,
    }
    path = _archive_path(chat_id, now)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        _events.log("archive_write_failed",
                     {"message": f"could not archive a {kind!r} message",
                      "kind": kind},
                     level=Level.WARN, exc=exc)


def prune_message_archive(now: datetime, ttl_days: int = DEFAULT_TTL_DAYS) -> int:
    """Deletes archived messages older than ttl_days, returns the count
    removed. Same shape as news_cache.cleanup_expired -- age comes from
    the filename's own timestamp (see _TIMESTAMP_LEN), not the file's
    mtime, so it stays correct regardless of how the file was
    copied/touched. A filename that doesn't start with a well-formed
    timestamp (shouldn't happen -- archive_message always writes one) is
    treated as expired rather than kept forever by accident, same
    fail-open instinct as news_cache.cleanup_expired."""
    directory = Path(ARCHIVE_DIR)
    if not directory.is_dir():
        return 0
    cutoff = now.timestamp() - ttl_days * 86400
    removed = 0
    for path in directory.glob("*.json"):
        try:
            file_time = datetime.strptime(
                path.name[:_TIMESTAMP_LEN], "%Y%m%dT%H%M%S%f").replace(tzinfo=timezone.utc)
            expired = file_time.timestamp() < cutoff
        except ValueError:
            expired = True
        if expired:
            path.unlink(missing_ok=True)
            removed += 1
    return removed
