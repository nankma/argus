"""
Local file cache for news articles -- see docs/local-news-cache-plan.md.

One YAML file per article, named `{source}-{id}.yaml` where `id` is a
short hash of the article's link. Hashing the link (rather than an
incrementing counter, or a per-source ID field not every source provides)
means re-fetching the same article in a later ingestion cycle produces the
same filename and just overwrites harmlessly -- free deduplication, no
separate tracking state needed.

CACHE_DIR is configurable via NEWS_CACHE_DIR, same reasoning as
users_db.py's SUBSCRIBERS_DB_FILE -- local dev and the deployed container
need different paths, and a container restart shouldn't lose the cache if
NEWS_CACHE_DIR points at the same mounted volume as subscribers.db.

Retention is judged by `fetched_at` (when THIS system pulled the article),
not `published_dt` (the source's own claimed publish time) -- some
sources' publish dates don't parse at all (see news_sources.py), but
fetched_at is always known and controlled by our own code, so cleanup can
always trust it.
"""

import hashlib
import os
from datetime import datetime
from pathlib import Path

import yaml

CACHE_DIR = os.environ.get("NEWS_CACHE_DIR", "news_cache")
DEFAULT_TTL_HOURS = 48


def _cache_dir() -> Path:
    path = Path(CACHE_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _article_id(link: str) -> str:
    return hashlib.sha256(link.encode("utf-8")).hexdigest()[:12]


def _file_path(source_key: str, link: str) -> Path:
    return _cache_dir() / f"{source_key}-{_article_id(link)}.yaml"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_iso(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


def write_article(source_key: str, article: dict, categories: list[str], fetched_at: datetime) -> Path:
    """`source_key` is the registry key (e.g. "bbc_business", matching
    SOURCE_REGISTRY in news_sources.py), not the article's own "source"
    field (a display name like "BBC Business") -- the filename needs the
    short, filesystem-clean key, not the human-readable name. `article` is
    the normalized shape news_sources.py's fetch_* functions return
    (title/link/source/summary/published/published_dt). Overwrites
    silently if this exact link was already cached (from an earlier cycle,
    or a different source pulling the same story) -- the newer fetch and
    classification simply replace the older one."""
    link = article["link"]
    record = {
        "source": article.get("source"),
        "source_key": source_key,
        "title": article.get("title"),
        "link": link,
        "summary": article.get("summary"),
        "published": article.get("published"),
        "published_dt": _iso(article.get("published_dt")),
        "fetched_at": _iso(fetched_at),
        "categories": list(categories),
    }
    path = _file_path(source_key, link)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(record, f, allow_unicode=True, sort_keys=False)
    return path


def read_all() -> list[dict]:
    """Every currently-cached article, parsed back with published_dt and
    fetched_at as real datetimes (not the raw ISO strings written to disk)
    -- mirrors news_sources.py's own published/published_dt shape so
    downstream filtering code doesn't need to know the difference between
    a freshly-fetched article and one read back from the cache."""
    articles = []
    for path in _cache_dir().glob("*.yaml"):
        try:
            with open(path, encoding="utf-8") as f:
                record = yaml.safe_load(f)
        except (OSError, yaml.YAMLError):
            continue
        if not record:
            continue
        record["published_dt"] = _parse_iso(record.get("published_dt"))
        record["fetched_at"] = _parse_iso(record.get("fetched_at"))
        articles.append(record)
    return articles


def cleanup_expired(now: datetime, ttl_hours: int = DEFAULT_TTL_HOURS) -> int:
    """Deletes every cached file whose fetched_at is older than ttl_hours.
    A file with no parseable fetched_at (shouldn't happen -- we always
    write it -- but cheap to guard) is treated as expired rather than kept
    forever by accident."""
    deleted = 0
    for path in _cache_dir().glob("*.yaml"):
        try:
            with open(path, encoding="utf-8") as f:
                record = yaml.safe_load(f)
        except (OSError, yaml.YAMLError):
            path.unlink(missing_ok=True)
            deleted += 1
            continue
        fetched_at = _parse_iso((record or {}).get("fetched_at"))
        if fetched_at is None or (now - fetched_at).total_seconds() > ttl_hours * 3600:
            path.unlink(missing_ok=True)
            deleted += 1
    return deleted
