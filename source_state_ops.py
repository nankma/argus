"""
Data Access Layer for per-source ingest bookkeeping -- news_ingest.py's
per-source due-check.
"""

from datetime import datetime

from storage import get_storage


def get_source_last_pulled_at(source: str) -> datetime | None:
    raw = get_storage().get_source_last_pulled_at(source)
    return datetime.fromisoformat(raw) if raw else None


def set_source_last_pulled_at(source: str, when: datetime) -> None:
    get_storage().set_source_last_pulled_at(source, when.isoformat())


def get_source_last_article_dt(source: str) -> datetime | None:
    """The published_dt of the newest article actually SEEN from this
    source -- deliberately different from get_source_last_pulled_at (when
    the job last RAN). Only advances when a newer article is actually
    observed, so it can't outrun what's genuinely been seen the way a
    wall-clock timestamp can -- see git history for the full 2026-08-16
    design correction this was made for."""
    raw = get_storage().get_source_last_article_dt(source)
    return datetime.fromisoformat(raw) if raw else None


def set_source_last_article_dt(source: str, when: datetime) -> None:
    """Callers should only pass a value >= the current one (news_ingest.py
    only ever passes the max published_dt observed this cycle) -- not
    enforced here, since the only caller already guarantees it."""
    get_storage().set_source_last_article_dt(source, when.isoformat())
