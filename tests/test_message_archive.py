import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import message_archive
import users_db

NOW = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)


def test_archive_message_writes_one_file_with_the_right_fields(
    isolated_message_archive, isolated_subscribers_db
):
    users_db.set_interests(42, ["AI"])
    message_archive.archive_message(42, "push_digest", "<b>Digest</b>", topic="AI", now=NOW)

    files = list(isolated_message_archive.glob("*.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_text(encoding="utf-8"))
    assert record["timestamp"] == NOW.isoformat()
    assert record["recipient"] == users_db.external_id(42)
    assert record["kind"] == "push_digest"
    assert record["topic"] == "AI"
    assert record["text"] == "<b>Digest</b>"


def test_archive_message_recipient_is_the_opaque_id_not_the_chat_id(
    isolated_message_archive, isolated_subscribers_db
):
    """Same hygiene reasoning as news_push._record's span attribute:
    chat_id is a real Telegram identifier and this file may be inspected
    more casually than the DB."""
    message_archive.archive_message(42, "chat_reply", "hi", now=NOW)

    files = list(isolated_message_archive.glob("*.json"))
    record = json.loads(files[0].read_text(encoding="utf-8"))
    assert record["recipient"] != "42"  # not the literal chat_id
    assert record["recipient"] == users_db.external_id(42)


def test_archive_message_topic_defaults_to_none_for_a_chat_reply(
    isolated_message_archive, isolated_subscribers_db
):
    message_archive.archive_message(42, "chat_reply", "hi")

    files = list(isolated_message_archive.glob("*.json"))
    record = json.loads(files[0].read_text(encoding="utf-8"))
    assert record["topic"] is None


def test_archive_message_fails_open_on_a_write_error(
    isolated_message_archive, isolated_subscribers_db, monkeypatch, capsys
):
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "mkdir", boom)

    message_archive.archive_message(42, "push_digest", "text", now=NOW)  # must not raise

    assert "could not archive" in capsys.readouterr().out


# --- prune_message_archive --------------------------------------------------


def test_prune_message_archive_removes_files_past_ttl(isolated_message_archive, isolated_subscribers_db):
    old = NOW - timedelta(days=8)
    fresh = NOW - timedelta(hours=1)
    message_archive.archive_message(1, "push_digest", "old one", now=old)
    message_archive.archive_message(1, "push_digest", "fresh one", now=fresh)

    removed = message_archive.prune_message_archive(NOW, ttl_days=7)

    assert removed == 1
    remaining = list(isolated_message_archive.glob("*.json"))
    assert len(remaining) == 1
    assert json.loads(remaining[0].read_text(encoding="utf-8"))["text"] == "fresh one"


def test_prune_message_archive_keeps_everything_under_ttl(isolated_message_archive, isolated_subscribers_db):
    message_archive.archive_message(1, "push_digest", "recent", now=NOW - timedelta(hours=1))

    removed = message_archive.prune_message_archive(NOW, ttl_days=7)

    assert removed == 0
    assert len(list(isolated_message_archive.glob("*.json"))) == 1


def test_prune_message_archive_does_nothing_on_a_missing_directory(isolated_message_archive):
    # isolated_message_archive points at a path that doesn't exist yet --
    # archive_message creates it lazily, and no message has been archived.
    assert message_archive.prune_message_archive(NOW) == 0


def test_prune_message_archive_treats_an_unparseable_filename_as_expired(isolated_message_archive):
    isolated_message_archive.mkdir(parents=True)
    (isolated_message_archive / "not-a-real-timestamp.json").write_text("{}", encoding="utf-8")

    removed = message_archive.prune_message_archive(NOW, ttl_days=7)

    assert removed == 1
    assert list(isolated_message_archive.glob("*.json")) == []
