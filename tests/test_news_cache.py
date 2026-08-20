from datetime import datetime, timedelta, timezone

import news_cache


def _article(link="https://example.com/a", title="Title", source="BBC Business", summary="Summary"):
    return {
        "title": title,
        "link": link,
        "source": source,
        "summary": summary,
        "published": "Thu, 13 Aug 2026 10:00:00 GMT",
        "published_dt": datetime(2026, 8, 13, 10, 0, 0, tzinfo=timezone.utc),
    }


def test_write_article_creates_a_file_named_by_source_and_link_hash(isolated_news_cache):
    now = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
    path = news_cache.write_article("bbc_business", _article(), ["Finance"], now)
    assert path.exists()
    assert path.name.startswith("bbc_business-")
    assert path.suffix == ".yaml"


def test_write_then_read_all_round_trips_the_article(isolated_news_cache):
    now = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
    news_cache.write_article("bbc_business", _article(), ["Finance", "Stock"], now)

    articles = news_cache.read_all()
    assert len(articles) == 1
    a = articles[0]
    assert a["title"] == "Title"
    assert a["link"] == "https://example.com/a"
    assert a["source_key"] == "bbc_business"
    assert a["categories"] == ["Finance", "Stock"]
    assert a["fetched_at"] == now
    assert a["published_dt"] == datetime(2026, 8, 13, 10, 0, 0, tzinfo=timezone.utc)


def test_re_fetching_the_same_link_overwrites_not_duplicates(isolated_news_cache):
    t1 = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 8, 13, 16, 0, 0, tzinfo=timezone.utc)
    news_cache.write_article("bbc_business", _article(), ["Finance"], t1)
    news_cache.write_article("bbc_business", _article(), ["Finance", "Stock"], t2)

    articles = news_cache.read_all()
    assert len(articles) == 1
    assert articles[0]["fetched_at"] == t2
    assert articles[0]["categories"] == ["Finance", "Stock"]


def test_different_sources_same_link_are_kept_separate(isolated_news_cache):
    now = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
    news_cache.write_article("bbc_business", _article(link="https://x.com/1"), ["Finance"], now)
    news_cache.write_article("guardian_business", _article(link="https://x.com/1"), ["Finance"], now)

    assert len(news_cache.read_all()) == 2


def test_cleanup_expired_removes_files_past_ttl(isolated_news_cache):
    now = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
    old = now - timedelta(hours=49)
    fresh = now - timedelta(hours=1)
    news_cache.write_article("bbc_business", _article(link="https://x.com/old"), [], old)
    news_cache.write_article("bbc_business", _article(link="https://x.com/fresh"), [], fresh)

    deleted = news_cache.cleanup_expired(now, ttl_hours=48)

    assert deleted == 1
    remaining = news_cache.read_all()
    assert len(remaining) == 1
    assert remaining[0]["link"] == "https://x.com/fresh"


def test_cleanup_expired_keeps_everything_under_ttl(isolated_news_cache):
    now = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
    news_cache.write_article("bbc_business", _article(), [], now - timedelta(hours=1))

    deleted = news_cache.cleanup_expired(now, ttl_hours=48)

    assert deleted == 0
    assert len(news_cache.read_all()) == 1


def test_read_all_on_empty_cache_returns_empty_list(isolated_news_cache):
    assert news_cache.read_all() == []


def test_write_article_preserves_none_summary(isolated_news_cache):
    now = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
    news_cache.write_article("hackernews", _article(summary=None), [], now)
    assert news_cache.read_all()[0]["summary"] is None


def test_cleanup_archives_instead_of_deleting_when_an_archive_is_configured(
    isolated_news_cache, monkeypatch, tmp_path
):
    archive = tmp_path / "archive"
    monkeypatch.setattr(news_cache, "ARCHIVE_DIR", str(archive))
    now = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)
    fetched = now - timedelta(hours=72)
    path = news_cache.write_article("bbc_business", _article(), ["Finance"], fetched)

    assert news_cache.cleanup_expired(now, ttl_hours=48) == 1

    assert not path.exists(), "the file must leave the active cache"
    assert news_cache.read_all() == [], "and must not come back from read_all"
    # filed under the day it was fetched, not the day it expired
    archived = archive / "2026-08-12" / path.name
    assert archived.exists()


def test_archived_article_keeps_its_content(isolated_news_cache, monkeypatch, tmp_path):
    archive = tmp_path / "archive"
    monkeypatch.setattr(news_cache, "ARCHIVE_DIR", str(archive))
    now = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)
    fetched = now - timedelta(hours=72)
    news_cache.write_article("bbc_business", _article(title="Archived title"),
                             ["Finance"], fetched)
    news_cache.cleanup_expired(now, ttl_hours=48)

    # The whole point is building a corpus later, so the archived record has
    # to still parse into the same shape read_all produces.
    import yaml
    files = list((archive / "2026-08-12").glob("*.yaml"))
    assert len(files) == 1
    record = yaml.safe_load(files[0].read_text(encoding="utf-8"))
    assert record["title"] == "Archived title"
    assert record["categories"] == ["Finance"]
    assert record["source_key"] == "bbc_business"


def test_re_expiring_the_same_link_replaces_the_archived_copy(
    isolated_news_cache, monkeypatch, tmp_path
):
    """The filename is a hash of the link, so a collision in the archive
    means the same article was fetched and expired twice. Keeping the newer
    copy dedupes the corpus by link for free."""
    archive = tmp_path / "archive"
    monkeypatch.setattr(news_cache, "ARCHIVE_DIR", str(archive))
    now = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)
    fetched = now - timedelta(hours=72)

    news_cache.write_article("bbc_business", _article(title="First"), [], fetched)
    news_cache.cleanup_expired(now, ttl_hours=48)
    news_cache.write_article("bbc_business", _article(title="Second"), [], fetched)
    news_cache.cleanup_expired(now, ttl_hours=48)

    import yaml
    files = list((archive / "2026-08-12").glob("*.yaml"))
    assert len(files) == 1, "same link must not accumulate copies"
    assert yaml.safe_load(files[0].read_text(encoding="utf-8"))["title"] == "Second"


def test_unarchivable_file_is_deleted_rather_than_left_in_the_cache(
    isolated_news_cache, monkeypatch, tmp_path
):
    """If the archive can't be written to, the file must still leave the
    active cache -- otherwise it stays an expiry candidate forever and the
    failure repeats on every single ingestion cycle."""
    monkeypatch.setattr(news_cache, "ARCHIVE_DIR", str(tmp_path / "archive"))
    now = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)
    fetched = now - timedelta(hours=72)
    path = news_cache.write_article("bbc_business", _article(), [], fetched)

    def boom(self, target):
        raise OSError("cross-device link")

    monkeypatch.setattr(news_cache.Path, "replace", boom)
    assert news_cache.cleanup_expired(now, ttl_hours=48) == 1
    assert not path.exists()


def test_cleanup_still_deletes_when_no_archive_is_configured(isolated_news_cache, monkeypatch):
    monkeypatch.setattr(news_cache, "ARCHIVE_DIR", None)
    now = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)
    path = news_cache.write_article("bbc_business", _article(), [], now - timedelta(hours=72))
    assert news_cache.cleanup_expired(now, ttl_hours=48) == 1
    assert not path.exists()
