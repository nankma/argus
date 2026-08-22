import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import AIMessage

import news_cache
import news_push
import news_sources
import users_db
from tests.fakes import FakeToolCallingModel


# Selection keys on fetched_at (when we downloaded it), not published_dt --
# see news_push.select_candidate_articles. fetched_at defaults to published_dt
# so the many tests that only care about ordering stay readable; tests about
# the delay case set them apart explicitly.
def _article(link, published_dt=None, title="Some title", source="TestSource", categories=None,
             source_key="test", fetched_at=None):
    return {
        "title": title,
        "link": link,
        "source": source,
        "source_key": source_key,
        "summary": None,
        "published_dt": published_dt,
        "fetched_at": fetched_at if fetched_at is not None else published_dt,
        "categories": categories or [],
    }


# A fixed "now" for the age guard (MAX_ARTICLE_AGE_HOURS). Tests that use
# dated fixtures pass this so they don't start failing as the wall clock
# moves past the guard -- the same reason run_push_cycle takes `now`.
NOW = datetime(2026, 8, 6, tzinfo=timezone.utc)


# --- select_candidate_articles (stage 1: category filter) -----------------


def test_select_candidate_articles_ignores_dates_when_deciding_already_seen():
    """A date ranks; it never filters. Both of these were published well
    before the subscriber's last push, and neither has been sent -- so both
    must come through. This is the GNews case: a source publishing ~12h
    behind was excluded outright by the old `published_dt <= since` test,
    stranding 227 cached articles that could never reach a digest."""
    since = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
    old_but_unsent = _article("https://example.com/a", published_dt=NOW - timedelta(hours=30))
    older_but_unsent = _article("https://example.com/b", published_dt=NOW - timedelta(hours=40))

    result = news_push.select_candidate_articles(
        [old_but_unsent, older_but_unsent], ["AI"], {}, since, set(), now=NOW
    )

    assert [a["link"] for a in result] == ["https://example.com/a", "https://example.com/b"]


def test_select_candidate_articles_keeps_offering_an_unsent_article():
    """An article that lost the max_per_topic cut is not "seen" -- it stays a
    candidate until it is actually sent or ages out of the cache. Under the
    previous timestamp-based filter it would have been excluded forever,
    unsent and unrecorded."""
    articles = [
        _article(f"https://example.com/{i}", published_dt=NOW - timedelta(hours=i))
        for i in range(1, 6)
    ]

    first = news_push.select_candidate_articles(articles, ["AI"], {}, None, set(), max_per_topic=2, now=NOW)
    assert [a["link"] for a in first] == ["https://example.com/1", "https://example.com/2"]

    # next cycle: only what was actually sent is excluded
    second = news_push.select_candidate_articles(
        articles, ["AI"], {}, None, {a["link"] for a in first}, max_per_topic=2, now=NOW
    )
    assert [a["link"] for a in second] == ["https://example.com/3", "https://example.com/4"]


def test_select_candidate_articles_drops_articles_older_than_the_age_guard():
    """Guards the fetched_at rule against genuinely ancient content: Perigon's
    one successful fetch returned 50 articles whose newest was over a year
    old (security-plan.md finding 21). Freshly downloaded, but not news."""
    ancient = _article(
        "https://example.com/ancient",
        published_dt=NOW - timedelta(days=400),
        fetched_at=NOW,
    )
    recent = _article("https://example.com/recent", published_dt=NOW - timedelta(hours=2), fetched_at=NOW)

    result = news_push.select_candidate_articles([ancient, recent], ["AI"], {}, None, set(), now=NOW)

    assert [a["link"] for a in result] == ["https://example.com/recent"]


def test_select_candidate_articles_keeps_articles_with_unparseable_published_dt():
    """Fails open, same instinct as the rest of the pipeline -- an article
    whose date didn't parse isn't assumed ancient."""
    undated = _article("https://example.com/undated", published_dt=None, fetched_at=NOW)

    result = news_push.select_candidate_articles([undated], ["AI"], {}, None, set(), now=NOW)

    assert [a["link"] for a in result] == ["https://example.com/undated"]


def test_select_candidate_articles_never_resends_a_pushed_link():
    """already_pushed_links is now checked unconditionally, not only when the
    date is unparseable -- so it guards every path."""
    already_sent = _article(
        "https://example.com/sent", published_dt=NOW - timedelta(hours=1), fetched_at=NOW
    )

    result = news_push.select_candidate_articles(
        [already_sent], ["AI"], {}, None, {"https://example.com/sent"}, now=NOW
    )

    assert result == []


def test_select_candidate_articles_falls_back_to_pushed_links_for_unparsed_dates():
    seen = _article("https://example.com/seen", published_dt=None)
    unseen = _article("https://example.com/unseen", published_dt=None)

    result = news_push.select_candidate_articles(
        [seen, unseen], ["AI"], {}, None, {"https://example.com/seen"}
    )

    assert [a["link"] for a in result] == ["https://example.com/unseen"]


def test_select_candidate_articles_dedupes_across_topics():
    article = _article("https://example.com/shared", published_dt=NOW - timedelta(hours=1))

    result = news_push.select_candidate_articles([article], ["AI", "robotics"], {}, None, set(), now=NOW)

    assert len(result) == 1


def test_select_candidate_articles_unrestricted_topic_matches_any_category():
    # A topic with no cached category mapping (classifier miss) shouldn't
    # be starved -- it matches an article regardless of that article's
    # own categories.
    article = _article("https://example.com/a", categories=["Policy"])

    result = news_push.select_candidate_articles([article], ["AAOI"], {}, None, set())

    assert len(result) == 1


def test_select_candidate_articles_explicit_empty_mapping_is_also_unrestricted():
    """The "unrestricted" branch (test above) is normally exercised by a
    topic simply ABSENT from topic_categories (a classifier miss). This
    pins the other way an interest can land there: a topic present in the
    dict but explicitly mapped to [] -- e.g. an interest that once mapped
    to a category later retired and never re-mapped (see
    users_db._migrate_split_policy's docstring and
    test_users_db.test_policy_split_does_not_touch_pre_existing_interest_category_mappings
    -- no code path currently produces this for Policy specifically, but
    nothing would stop it for a category retired in the future). Both cases
    hit the same `topic_cats and not (...)` branch and are therefore
    indistinguishable to this function -- which is exactly the gap: nothing
    downstream can tell "never classified" apart from "used to be
    restricted, now silently isn't"."""
    article = _article("https://example.com/a", categories=["Government"])

    result = news_push.select_candidate_articles(
        [article], ["legacy policy watcher"], {"legacy policy watcher": []}, None, set()
    )

    assert len(result) == 1


def test_select_candidate_articles_excludes_off_category_article():
    # The Nikkei Asia incident this was built to fix: an uncategorized
    # (or off-category) article shouldn't reach a subscriber whose topic
    # DID classify into real categories.
    earthquake = _article("https://example.com/earthquake", categories=[])
    ai_topic_categories = {"AI": ["AI", "Research"]}

    result = news_push.select_candidate_articles([earthquake], ["AI"], ai_topic_categories, None, set())

    assert result == []


def test_select_candidate_articles_includes_overlapping_category_article():
    article = _article("https://example.com/a", categories=["AI", "Startups"])
    topic_categories = {"AI": ["AI", "Research"]}

    result = news_push.select_candidate_articles([article], ["AI"], topic_categories, None, set())

    assert len(result) == 1


def test_select_candidate_articles_excludes_restricted_sources_by_default():
    article = _article("https://example.com/a", source_key="perigon")

    result = news_push.select_candidate_articles([article], ["AI"], {}, None, set())

    assert result == []


def test_select_candidate_articles_includes_restricted_sources_when_enabled():
    article = _article("https://example.com/a", source_key="perigon")

    result = news_push.select_candidate_articles([article], ["AI"], {}, None, set(), include_restricted=True)

    assert len(result) == 1


def test_select_candidate_articles_caps_per_topic():
    articles = [
        _article(f"https://example.com/{i}", published_dt=NOW - timedelta(hours=8 - i))
        for i in range(1, 8)
    ]

    result = news_push.select_candidate_articles(articles, ["AI"], {}, None, set(), max_per_topic=3, now=NOW)

    assert len(result) == 3
    # newest-first
    assert result[0]["link"] == "https://example.com/7"


# --- resolve_interest_categories -------------------------------------------


def test_resolve_interest_categories_uses_cache_when_available(monkeypatch):
    monkeypatch.setattr(users_db, "get_cached_interest_categories", lambda interests: {"AI": ["AI"]})
    classify = MagicMock()
    monkeypatch.setattr(news_push.news_classify, "classify_interests", classify)

    result = news_push.resolve_interest_categories("fake-model", ["AI"])

    assert result == {"AI": ["AI"]}
    classify.assert_not_called()


def test_resolve_interest_categories_classifies_and_caches_misses(monkeypatch, isolated_subscribers_db):
    monkeypatch.setattr(users_db, "get_cached_interest_categories", lambda interests: {})
    monkeypatch.setattr(news_push.news_classify, "classify_interests", lambda model, interests, taxonomy: {"AAOI": ["Stock"]})
    set_categories = MagicMock()
    monkeypatch.setattr(users_db, "set_interest_categories", set_categories)

    result = news_push.resolve_interest_categories("fake-model", ["AAOI"])

    assert result == {"AAOI": ["Stock"]}
    set_categories.assert_called_once_with("AAOI", ["Stock"])


def test_resolve_interest_categories_caches_a_genuinely_empty_result(monkeypatch, isolated_subscribers_db):
    """The model answered "no category applies". That is a real answer and
    belongs in the cache -- re-classifying it every cycle would just re-pay
    for the same conclusion."""
    monkeypatch.setattr(users_db, "get_cached_interest_categories", lambda interests: {})
    monkeypatch.setattr(news_push.news_classify, "classify_interests",
                        lambda model, interests, taxonomy: {"Some obscure ticker": []})
    set_categories = MagicMock()
    monkeypatch.setattr(users_db, "set_interest_categories", set_categories)

    result = news_push.resolve_interest_categories("fake-model", ["Some obscure ticker"])

    assert result == {"Some obscure ticker": []}
    set_categories.assert_called_once_with("Some obscure ticker", [])


def test_resolve_interest_categories_does_not_cache_a_failure(monkeypatch, isolated_subscribers_db):
    """Regression test for a live bug. classify_interests omits an interest
    it failed on; caching that as [] made the failure permanent, and an
    empty mapping matches every article, so affected subscribers were sent
    entirely unfiltered news. On the live DB this had poisoned "AI",
    "Bitcoin" and "機器人科技" among others -- "AI" cached as [] despite AI
    being one of the 13 categories."""
    monkeypatch.setattr(users_db, "get_cached_interest_categories", lambda interests: {})
    monkeypatch.setattr(news_push.news_classify, "classify_interests",
                        lambda model, interests, taxonomy: {})
    set_categories = MagicMock()
    monkeypatch.setattr(users_db, "set_interest_categories", set_categories)

    result = news_push.resolve_interest_categories("fake-model", ["AI"])

    set_categories.assert_not_called()
    assert result == {}, "unresolved, so the next cycle retries it"


# --- write_push_digest ------------------------------------------------------


def test_write_push_digest_returns_model_output():
    model = FakeToolCallingModel(responses=[AIMessage(content="<b>Digest</b>")])
    articles = [{**_article("https://example.com/a"), "topic": "AI"}]

    result = news_push.write_push_digest(model, articles)

    assert result == "<b>Digest</b>"


def test_write_push_digest_includes_language_directive_when_set():
    captured = {}

    class RecordingModel(FakeToolCallingModel):
        def invoke(self, messages, *args, **kwargs):
            captured["system_prompt"] = messages[0]["content"]
            return super().invoke(messages, *args, **kwargs)

    model = RecordingModel(responses=[AIMessage(content="<b>Digest</b>")])
    articles = [{**_article("https://example.com/a"), "topic": "AI"}]

    news_push.write_push_digest(model, articles, language="Spanish")

    assert "Spanish" in captured["system_prompt"]


def test_write_push_digest_no_language_directive_when_unset():
    captured = {}

    class RecordingModel(FakeToolCallingModel):
        def invoke(self, messages, *args, **kwargs):
            captured["system_prompt"] = messages[0]["content"]
            return super().invoke(messages, *args, **kwargs)

    model = RecordingModel(responses=[AIMessage(content="<b>Digest</b>")])
    articles = [{**_article("https://example.com/a"), "topic": "AI"}]

    news_push.write_push_digest(model, articles)

    assert captured["system_prompt"] == news_push._PUSH_DIGEST_PROMPT


def test_is_subscriber_due_true_when_never_pushed():
    assert news_push.is_subscriber_due(None, 24, datetime.now(timezone.utc)) is True


def test_is_subscriber_due_false_within_interval():
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    last_push = now - timedelta(hours=2)
    assert news_push.is_subscriber_due(last_push, 24, now) is False


def test_is_subscriber_due_true_after_interval():
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    last_push = now - timedelta(hours=25)
    assert news_push.is_subscriber_due(last_push, 24, now) is True


def _subscriber(
    chat_id,
    interests=("AI",),
    interval=24,
    last_push_at=None,
    pushed_links=(),
    language=None,
    restricted_sources_enabled=False,
):
    return {
        "chat_id": chat_id,
        "interests": list(interests),
        "push_interval_hours": interval,
        "last_push_at": last_push_at,
        "pushed_links": list(pushed_links),
        "language": language,
        "restricted_sources_enabled": restricted_sources_enabled,
    }


def _stub_cache_and_categories(monkeypatch, cached_articles=(), topic_categories=None):
    monkeypatch.setattr(news_cache, "read_all", lambda: list(cached_articles))
    monkeypatch.setattr(
        news_push, "resolve_interest_categories", lambda model, interests: topic_categories or {}
    )


def test_run_push_cycle_skips_subscriber_with_no_interests(monkeypatch, isolated_subscribers_db):
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers", lambda: [_subscriber(1, interests=[])])
    record_push = MagicMock()
    monkeypatch.setattr(users_db, "record_push", record_push)
    send = AsyncMock()

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=send))

    send.assert_not_called()
    record_push.assert_not_called()


def test_run_push_cycle_skips_subscriber_not_due(monkeypatch, isolated_subscribers_db):
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    recently_pushed = _subscriber(2, last_push_at=now - timedelta(hours=1), interval=24)
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers", lambda: [recently_pushed])
    record_push = MagicMock()
    monkeypatch.setattr(users_db, "record_push", record_push)
    select = MagicMock()
    monkeypatch.setattr(news_push, "select_candidate_articles", select)
    send = AsyncMock()

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=send, now=now))

    select.assert_not_called()
    send.assert_not_called()
    record_push.assert_not_called()


def test_run_push_cycle_sends_and_records_when_new_articles_found(monkeypatch, isolated_subscribers_db):
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers", lambda: [_subscriber(3)])
    record_push = MagicMock()
    monkeypatch.setattr(users_db, "record_push", record_push)
    new_articles = [{**_article("https://example.com/new"), "topic": "AI"}]
    _stub_cache_and_categories(monkeypatch)
    monkeypatch.setattr(news_push, "select_candidate_articles", MagicMock(return_value=new_articles))
    # the digest must actually cite the article for it to count as sent
    digest = '<b>Digest</b> 🔗 <a href="https://example.com/new">Source</a>'
    monkeypatch.setattr(news_push, "write_push_digest", MagicMock(return_value=digest))
    monkeypatch.setattr(news_push.guardrails, "is_output_on_topic", MagicMock(return_value=True))
    send = AsyncMock()

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=send, now=now))

    send.assert_called_once_with(3, digest)
    record_push.assert_called_once_with(3, ["https://example.com/new"], now)


def test_run_push_cycle_only_records_articles_the_digest_actually_cited(
    monkeypatch, isolated_subscribers_db
):
    """Stage 2 (the digest prompt) drops candidates it judges irrelevant. A
    dropped candidate was never seen by the subscriber, so it must stay
    eligible for a later digest rather than being retired unread."""
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers", lambda: [_subscriber(9)])
    record_push = MagicMock()
    monkeypatch.setattr(users_db, "record_push", record_push)
    candidates = [
        {**_article("https://example.com/used"), "topic": "AI"},
        {**_article("https://example.com/dropped"), "topic": "AI"},
    ]
    _stub_cache_and_categories(monkeypatch)
    monkeypatch.setattr(news_push, "select_candidate_articles", MagicMock(return_value=candidates))
    monkeypatch.setattr(
        news_push, "write_push_digest",
        MagicMock(return_value='📰 <a href="https://example.com/used">Only this one</a>'),
    )
    monkeypatch.setattr(news_push.guardrails, "is_output_on_topic", MagicMock(return_value=True))

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=AsyncMock(), now=now))

    record_push.assert_called_once_with(9, ["https://example.com/used"], now)


def test_run_push_cycle_passes_subscriber_language_to_digest(monkeypatch, isolated_subscribers_db):
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        users_db, "list_push_enabled_subscribers", lambda: [_subscriber(8, language="French")]
    )
    monkeypatch.setattr(users_db, "record_push", MagicMock())
    new_articles = [{**_article("https://example.com/new"), "topic": "AI"}]
    _stub_cache_and_categories(monkeypatch)
    monkeypatch.setattr(news_push, "select_candidate_articles", MagicMock(return_value=new_articles))
    write_digest = MagicMock(return_value="<b>Digest</b>")
    monkeypatch.setattr(news_push, "write_push_digest", write_digest)
    monkeypatch.setattr(news_push.guardrails, "is_output_on_topic", MagicMock(return_value=True))

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=AsyncMock(), now=now))

    write_digest.assert_called_once_with("fake-model", new_articles, "French")


def test_run_push_cycle_passes_subscribers_own_restricted_sources_flag(monkeypatch, isolated_subscribers_db):
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        users_db,
        "list_push_enabled_subscribers",
        lambda: [_subscriber(9, restricted_sources_enabled=True), _subscriber(10, restricted_sources_enabled=False)],
    )
    monkeypatch.setattr(users_db, "record_push", MagicMock())
    _stub_cache_and_categories(monkeypatch)
    select = MagicMock(return_value=[])
    monkeypatch.setattr(news_push, "select_candidate_articles", select)

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=AsyncMock(), now=now))

    assert select.call_args_list[0].kwargs["include_restricted"] is True
    assert select.call_args_list[1].kwargs["include_restricted"] is False


def test_run_push_cycle_reads_cache_once_and_reuses_across_subscribers(monkeypatch, isolated_subscribers_db):
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        users_db, "list_push_enabled_subscribers", lambda: [_subscriber(11), _subscriber(12)]
    )
    monkeypatch.setattr(users_db, "record_push", MagicMock())
    read_all = MagicMock(return_value=[])
    monkeypatch.setattr(news_cache, "read_all", read_all)
    monkeypatch.setattr(news_push, "resolve_interest_categories", lambda model, interests: {})
    monkeypatch.setattr(news_push, "select_candidate_articles", MagicMock(return_value=[]))

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=AsyncMock(), now=now))

    read_all.assert_called_once()


def test_run_push_cycle_no_new_articles_records_without_sending(monkeypatch, isolated_subscribers_db):
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers", lambda: [_subscriber(4)])
    record_push = MagicMock()
    monkeypatch.setattr(users_db, "record_push", record_push)
    _stub_cache_and_categories(monkeypatch)
    monkeypatch.setattr(news_push, "select_candidate_articles", MagicMock(return_value=[]))
    send = AsyncMock()

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=send, now=now))

    send.assert_not_called()
    record_push.assert_called_once_with(4, [], now)


def test_run_push_cycle_empty_digest_records_without_sending(monkeypatch, isolated_subscribers_db):
    """Stage 2 (the model's own judgment inside write_push_digest) decided
    none of the stage-1 candidates were genuinely relevant, so it wrote
    nothing. Nothing was delivered, so nothing may be recorded as seen.

    This test previously asserted the opposite -- that every candidate's
    link was recorded -- and so pinned a real bug in place. pushed_links is
    the "already seen" filter, and an article that merely lost one cycle's
    relevance judgment has not been seen; recording it here retired
    articles permanently that no subscriber ever read. The three sibling
    branches (no candidates, guardrail-blocked, delivered) all handled this
    correctly; only this one didn't, while its comment claimed it matched
    them."""
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers", lambda: [_subscriber(13)])
    record_push = MagicMock()
    monkeypatch.setattr(users_db, "record_push", record_push)
    new_articles = [{**_article("https://example.com/new"), "topic": "AI"}]
    _stub_cache_and_categories(monkeypatch)
    monkeypatch.setattr(news_push, "select_candidate_articles", MagicMock(return_value=new_articles))
    monkeypatch.setattr(news_push, "write_push_digest", MagicMock(return_value="   "))
    send = AsyncMock()

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=send, now=now))

    send.assert_not_called()
    record_push.assert_called_once_with(13, [], now)


def test_run_push_cycle_blocked_by_output_guardrail_does_not_send(monkeypatch, isolated_subscribers_db):
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers", lambda: [_subscriber(5)])
    record_push = MagicMock()
    monkeypatch.setattr(users_db, "record_push", record_push)
    new_articles = [{**_article("https://example.com/new"), "topic": "AI"}]
    _stub_cache_and_categories(monkeypatch)
    monkeypatch.setattr(news_push, "select_candidate_articles", MagicMock(return_value=new_articles))
    monkeypatch.setattr(news_push, "write_push_digest", MagicMock(return_value="off-topic drift"))
    monkeypatch.setattr(news_push.guardrails, "is_output_on_topic", MagicMock(return_value=False))
    send = AsyncMock()

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=send, now=now))

    send.assert_not_called()
    # Nothing was delivered, so nothing is recorded as seen -- the articles
    # stay eligible for the next digest. last_push_at still advances, so the
    # next attempt is a full interval away rather than retrying every tick.
    record_push.assert_called_once_with(5, [], now)


def test_run_push_cycle_isolates_one_subscribers_failure(monkeypatch, isolated_subscribers_db):
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        users_db, "list_push_enabled_subscribers", lambda: [_subscriber(6), _subscriber(7)]
    )
    record_push = MagicMock()
    monkeypatch.setattr(users_db, "record_push", record_push)
    _stub_cache_and_categories(monkeypatch)

    call_count = {"n": 0}

    def select_side_effect(cached_articles, topics, topic_categories, since, pushed_links,
                           include_restricted=False, now=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("boom")
        return [{**_article("https://example.com/ok"), "topic": "AI"}]

    monkeypatch.setattr(news_push, "select_candidate_articles", MagicMock(side_effect=select_side_effect))
    monkeypatch.setattr(news_push, "write_push_digest", MagicMock(return_value="<b>Digest</b>"))
    monkeypatch.setattr(news_push.guardrails, "is_output_on_topic", MagicMock(return_value=True))
    send = AsyncMock()

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=send, now=now))

    # subscriber 6 failed silently; subscriber 7 still got its digest
    send.assert_called_once_with(7, "<b>Digest</b>")


# --- links_actually_sent --------------------------------------------------
#
# This decides what gets recorded as "already seen", so a bug here either
# retires articles nobody read or re-sends ones they already got.


def test_links_actually_sent_returns_only_candidates_present_in_the_digest():
    candidates = [_article("https://example.com/a"), _article("https://example.com/b")]
    digest = '<b>News</b><a href="https://example.com/a">Only A</a>'

    assert news_push.links_actually_sent(digest, candidates) == ["https://example.com/a"]


def test_links_actually_sent_handles_single_quoted_hrefs():
    candidates = [_article("https://example.com/a")]

    assert news_push.links_actually_sent(
        "<a href='https://example.com/a'>A</a>", candidates
    ) == ["https://example.com/a"]


def test_links_actually_sent_returns_nothing_for_a_digest_with_no_anchors():
    """A digest the model wrote without any links means nothing verifiable
    reached the subscriber, so nothing is recorded as seen."""
    candidates = [_article("https://example.com/a")]

    assert news_push.links_actually_sent("Just prose, no links at all", candidates) == []


def test_links_actually_sent_ignores_hrefs_that_match_no_candidate():
    """The model can cite a URL that wasn't in the candidate list. Only
    candidate links are recorded -- pushed_links is keyed to the cache."""
    candidates = [_article("https://example.com/a")]
    digest = '<a href="https://elsewhere.com/x">Elsewhere</a>'

    assert news_push.links_actually_sent(digest, candidates) == []


def test_links_actually_sent_on_empty_digest_is_empty():
    assert news_push.links_actually_sent("", [_article("https://example.com/a")]) == []
    assert news_push.links_actually_sent(None, [_article("https://example.com/a")]) == []


# --- push_outcomes: the queryable half of every log line ------------------
#
# Each of these asserts the OUTCOME recorded, not the print. The point of
# the table is that an alarm can distinguish cases `docker logs` only ever
# rendered as prose -- above all "generated but undeliverable", which used
# to land in the catch-all as an ordinary cycle failure.


def _cycle_with(monkeypatch, chat_id=1, subscriber=None, digest=None,
                on_topic=True, send=None, now=None, record_push=None):
    """Drives one full cycle far enough to produce a digest, so a test only
    has to say which step misbehaves.

    `record_push` is stubbed by default because most of these tests are
    about which OUTCOME was recorded; pass your own MagicMock when the
    test is about whether last_push_at advanced."""
    now = now or datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers",
                        lambda: [subscriber or _subscriber(chat_id)])
    monkeypatch.setattr(users_db, "record_push", record_push or MagicMock())
    _stub_cache_and_categories(monkeypatch)
    monkeypatch.setattr(news_push, "select_candidate_articles",
                        MagicMock(return_value=[{**_article("https://example.com/new"), "topic": "AI"}]))
    if digest is None:
        digest = '<b>Digest</b> 🔗 <a href="https://example.com/new">Source</a>'
    monkeypatch.setattr(news_push, "write_push_digest", MagicMock(return_value=digest))
    monkeypatch.setattr(news_push.guardrails, "is_output_on_topic", MagicMock(return_value=on_topic))
    asyncio.run(news_push.run_push_cycle(model="fake-model", send=send or AsyncMock(), now=now))
    return now


def test_run_push_cycle_records_delivered(monkeypatch, isolated_subscribers_db):
    _cycle_with(monkeypatch, chat_id=11)
    assert users_db.recent_outcomes_for(11) == [users_db.PUSH_DELIVERED]


def test_run_push_cycle_records_blocked_digest_as_its_own_outcome(monkeypatch, isolated_subscribers_db):
    _cycle_with(monkeypatch, chat_id=12, on_topic=False)
    assert users_db.recent_outcomes_for(12) == [users_db.PUSH_BLOCKED]


def test_run_push_cycle_records_chat_not_found_when_delivery_is_refused(monkeypatch, isolated_subscribers_db):
    """The 2026-08-21 signature: generation succeeded and was billed, only
    the send failed. Must NOT read as a generic cycle failure -- criterion
    1 keys on exactly this."""
    send = AsyncMock(side_effect=Exception("Chat not found"))
    _cycle_with(monkeypatch, chat_id=13, send=send)
    assert users_db.recent_outcomes_for(13) == [users_db.PUSH_CHAT_NOT_FOUND]


def test_run_push_cycle_records_blocked_user_as_chat_not_found(monkeypatch, isolated_subscribers_db):
    send = AsyncMock(side_effect=Exception("Forbidden: bot was blocked by the user"))
    _cycle_with(monkeypatch, chat_id=14, send=send)
    assert users_db.recent_outcomes_for(14) == [users_db.PUSH_CHAT_NOT_FOUND]


def test_run_push_cycle_records_model_error_when_an_llm_call_raises(monkeypatch, isolated_subscribers_db):
    """A 402 comes out of write_push_digest, not out of the send. Classified
    by which call raised rather than by what the message says, so a
    provider rewording its errors cannot silence criterion 2."""
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers", lambda: [_subscriber(15)])
    monkeypatch.setattr(users_db, "record_push", MagicMock())
    _stub_cache_and_categories(monkeypatch)
    monkeypatch.setattr(news_push, "select_candidate_articles",
                        MagicMock(return_value=[{**_article("https://example.com/new"), "topic": "AI"}]))
    monkeypatch.setattr(news_push, "write_push_digest",
                        MagicMock(side_effect=RuntimeError("Error code: 402 - Insufficient Balance")))
    asyncio.run(news_push.run_push_cycle(model="fake-model", send=AsyncMock(),
                                         now=datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)))

    assert users_db.recent_outcomes_for(15) == [users_db.PUSH_MODEL_ERROR]


def test_run_push_cycle_records_a_non_model_failure_as_cycle_failed(monkeypatch, isolated_subscribers_db):
    """select_candidate_articles is local filtering, not an LLM call. If it
    raises, that is a bug in our code -- it must not inflate the model-error
    count and page someone about the provider."""
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers", lambda: [_subscriber(16)])
    monkeypatch.setattr(users_db, "record_push", MagicMock())
    _stub_cache_and_categories(monkeypatch)
    monkeypatch.setattr(news_push, "select_candidate_articles",
                        MagicMock(side_effect=KeyError("published_dt")))
    asyncio.run(news_push.run_push_cycle(model="fake-model", send=AsyncMock(),
                                         now=datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)))

    assert users_db.recent_outcomes_for(16) == [users_db.PUSH_CYCLE_FAILED]


def test_run_push_cycle_records_nothing_new_without_calling_the_model(monkeypatch, isolated_subscribers_db):
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers", lambda: [_subscriber(17)])
    monkeypatch.setattr(users_db, "record_push", MagicMock())
    _stub_cache_and_categories(monkeypatch)
    monkeypatch.setattr(news_push, "select_candidate_articles", MagicMock(return_value=[]))
    write = MagicMock()
    monkeypatch.setattr(news_push, "write_push_digest", write)
    asyncio.run(news_push.run_push_cycle(model="fake-model", send=AsyncMock(),
                                         now=datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)))

    assert users_db.recent_outcomes_for(17) == [users_db.PUSH_NOTHING_NEW]
    write.assert_not_called()


def test_run_push_cycle_records_no_interests(monkeypatch, isolated_subscribers_db):
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers",
                        lambda: [_subscriber(18, interests=[])])
    monkeypatch.setattr(users_db, "record_push", MagicMock())
    asyncio.run(news_push.run_push_cycle(model="fake-model", send=AsyncMock()))

    assert users_db.recent_outcomes_for(18) == [users_db.PUSH_NO_INTERESTS]


def test_run_push_cycle_does_not_record_a_not_due_subscriber(monkeypatch, isolated_subscribers_db):
    """Every subscriber is 'not due' on almost every tick. Recording it
    would bury the outcomes that carry signal under ~96 rows a day each."""
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers",
                        lambda: [_subscriber(19, last_push_at=now - timedelta(hours=1), interval=24)])
    monkeypatch.setattr(users_db, "record_push", MagicMock())
    asyncio.run(news_push.run_push_cycle(model="fake-model", send=AsyncMock(), now=now))

    assert users_db.recent_outcomes_for(19) == []


def test_classify_send_failure_falls_back_to_cycle_failed():
    """Wrong in the safe direction: an unrecognised delivery error must not
    be read as 'this chat is dead', because criterion 1 disables
    subscribers on that verdict."""
    assert news_push._classify_send_failure(Exception("Timed out")) == users_db.PUSH_CYCLE_FAILED
    assert news_push._classify_send_failure(Exception("Chat not found")) == users_db.PUSH_CHAT_NOT_FOUND


# --- capping an undeliverable subscriber ----------------------------------
#
# Two defects, fixed together because they compound. (A) no failure path
# advanced last_push_at, so a failing subscriber was due again on the next
# 15-minute tick rather than after their interval. (B) nothing ever stopped
# it. Generation happens before delivery is attempted and is billed either
# way, so an unreachable chat billed three LLM calls every 15 minutes for
# as long as the row existed -- the 2026-08-21 incident.


def _failing_send(message="Chat not found"):
    return AsyncMock(side_effect=Exception(message))


def test_delivery_failure_advances_last_push_at(monkeypatch, isolated_subscribers_db):
    """(A). Without this the subscriber is due again in PUSH_TICK_SECONDS
    and pays for another digest, having already paid for this one."""
    record_push = MagicMock()
    monkeypatch.setattr(users_db, "record_push", record_push)
    now = _cycle_with(monkeypatch, chat_id=21, send=_failing_send(),
                      record_push=record_push)

    record_push.assert_called_once_with(21, [], now)


def test_delivery_failure_retires_no_links(monkeypatch, isolated_subscribers_db):
    """The digest was never seen, so its articles must stay eligible for a
    later one -- same rule as the guardrail-blocked branch."""
    record_push = MagicMock()
    _cycle_with(monkeypatch, chat_id=22, send=_failing_send(), record_push=record_push)

    assert record_push.call_args[0][1] == []


def test_three_consecutive_chat_not_found_turns_push_off(monkeypatch, isolated_subscribers_db):
    # Set explicitly: get_push_enabled returns False for a row that does not
    # exist, so without this the assertion below would pass without the
    # subscriber ever having been turned off.
    users_db.set_push_enabled(23, True)
    for _ in range(news_push.UNREACHABLE_STRIKES):
        _cycle_with(monkeypatch, chat_id=23, send=_failing_send(),
                    record_push=MagicMock())

    assert users_db.recent_outcomes_for(23)[0] == users_db.PUSH_DISABLED
    assert users_db.get_push_enabled(23) is False


def test_two_consecutive_chat_not_found_leaves_push_on(monkeypatch, isolated_subscribers_db):
    """Turning a real subscriber off is the more expensive mistake: they
    just stop getting news, with nothing to notice."""
    users_db.set_push_enabled(23, True)
    for _ in range(news_push.UNREACHABLE_STRIKES - 1):
        _cycle_with(monkeypatch, chat_id=23, send=_failing_send(),
                    record_push=MagicMock())

    assert users_db.PUSH_DISABLED not in users_db.recent_outcomes_for(23)
    assert users_db.get_push_enabled(23) is True


def test_a_successful_delivery_clears_the_strikes(monkeypatch, isolated_subscribers_db):
    """Delivery is the only positive proof the chat is reachable, so it is
    the only thing that resets the count."""
    users_db.set_push_enabled(24, True)
    _cycle_with(monkeypatch, chat_id=24, send=_failing_send(), record_push=MagicMock())
    _cycle_with(monkeypatch, chat_id=24, send=_failing_send(), record_push=MagicMock())
    _cycle_with(monkeypatch, chat_id=24, record_push=MagicMock())          # delivered
    _cycle_with(monkeypatch, chat_id=24, send=_failing_send(), record_push=MagicMock())

    assert users_db.get_push_enabled(24) is True


def test_a_quiet_cycle_between_failures_does_not_clear_the_strikes(monkeypatch, isolated_subscribers_db):
    """The policy decision this rests on. A `nothing_new` cycle attempts no
    send, so it is evidence of nothing -- if it reset the count, a dead chat
    that happens to have a quiet cycle every third tick would bill digests
    forever and never strike out."""
    users_db.set_push_enabled(25, True)
    _cycle_with(monkeypatch, chat_id=25, send=_failing_send(), record_push=MagicMock())
    _cycle_with(monkeypatch, chat_id=25, send=_failing_send(), record_push=MagicMock())

    # a cycle with no candidate articles: no send is attempted at all
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers", lambda: [_subscriber(25)])
    _stub_cache_and_categories(monkeypatch)
    monkeypatch.setattr(news_push, "select_candidate_articles", MagicMock(return_value=[]))
    asyncio.run(news_push.run_push_cycle(model="fake-model", send=AsyncMock(),
                                         now=datetime(2026, 8, 8, 13, 0, tzinfo=timezone.utc)))

    _cycle_with(monkeypatch, chat_id=25, send=_failing_send(), record_push=MagicMock())

    assert users_db.get_push_enabled(25) is False


def test_model_error_before_generation_does_not_advance_last_push_at(monkeypatch, isolated_subscribers_db):
    """Nothing was generated, so nothing was billed -- there is no reason to
    make the subscriber wait a full interval for a transient provider blip."""
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers", lambda: [_subscriber(26)])
    record_push = MagicMock()
    monkeypatch.setattr(users_db, "record_push", record_push)
    monkeypatch.setattr(news_cache, "read_all", lambda: [])
    monkeypatch.setattr(news_push, "resolve_interest_categories",
                        MagicMock(side_effect=RuntimeError("402 Insufficient Balance")))
    asyncio.run(news_push.run_push_cycle(model="fake-model", send=AsyncMock(),
                                         now=datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)))

    assert users_db.recent_outcomes_for(26) == [users_db.PUSH_MODEL_ERROR]
    record_push.assert_not_called()


def test_model_error_after_generation_does_advance_last_push_at(monkeypatch, isolated_subscribers_db):
    """The guardrail check is an LLM call too, and by the time it runs the
    digest has already been written and billed."""
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers", lambda: [_subscriber(27)])
    record_push = MagicMock()
    monkeypatch.setattr(users_db, "record_push", record_push)
    _stub_cache_and_categories(monkeypatch)
    monkeypatch.setattr(news_push, "select_candidate_articles",
                        MagicMock(return_value=[{**_article("https://example.com/new"), "topic": "AI"}]))
    monkeypatch.setattr(news_push, "write_push_digest", MagicMock(return_value="<b>Digest</b>"))
    monkeypatch.setattr(news_push.guardrails, "is_output_on_topic",
                        MagicMock(side_effect=RuntimeError("rate limited")))
    asyncio.run(news_push.run_push_cycle(model="fake-model", send=AsyncMock(),
                                         now=datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)))

    assert users_db.recent_outcomes_for(27) == [users_db.PUSH_MODEL_ERROR]
    record_push.assert_called_once()


def test_striking_out_leaves_the_subscriber_and_their_settings_intact(monkeypatch, isolated_subscribers_db):
    """Only push_enabled is cleared. A user who blocked the bot and later
    unblocks it turns push back on, rather than finding their interests
    gone."""
    users_db.set_interests(28, ["AI", "Robotics"])
    users_db.set_push_enabled(28, True)
    for _ in range(news_push.UNREACHABLE_STRIKES):
        _cycle_with(monkeypatch, chat_id=28, send=_failing_send(), record_push=MagicMock())

    assert users_db.get_push_enabled(28) is False
    assert users_db.get_interests(28) == ["AI", "Robotics"]


# --- heartbeat -------------------------------------------------------------


def test_push_tick_emits_a_heartbeat_even_when_nobody_is_due(monkeypatch, isolated_subscribers_db):
    """The reason this exists. Every LLM call in a push cycle sits inside
    the per-subscriber loop after the due check, so a tick where nobody is
    due emits no spans at all -- and the dead man's switch would read a
    perfectly healthy idle system as dead. See
    docs/plans/observability-platform-plan.md."""
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers",
                        lambda: [_subscriber(31, last_push_at=now - timedelta(hours=1), interval=24)])
    beats = []
    monkeypatch.setattr(news_push, "_emit_heartbeat", lambda n: beats.append(n))

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=AsyncMock(), now=now))

    assert beats == [1]


def test_push_tick_heartbeat_carries_the_subscriber_count(monkeypatch, isolated_subscribers_db):
    """Not just a pulse: the count is the number whose quiet growth was the
    2026-08-21 incident, so the liveness span answers that too."""
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers",
                        lambda: [_subscriber(32, interests=[]), _subscriber(33, interests=[])])
    beats = []
    monkeypatch.setattr(news_push, "_emit_heartbeat", lambda n: beats.append(n))

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=AsyncMock()))

    assert beats == [2]


def test_emit_heartbeat_is_a_noop_without_a_tracer_provider():
    """Hermeticity: with no provider configured -- every test and CI run --
    OpenTelemetry hands back a no-op tracer, so this needs no env guard and
    nothing to stub. If it ever raises here, it would raise in production
    the moment telemetry was switched off."""
    news_push._emit_heartbeat(3)


def test_a_failure_after_a_successful_send_still_retires_what_was_delivered(
        monkeypatch, isolated_subscribers_db):
    """The window between `await send(...)` returning and record_push being
    reached contains two database writes. If either raises, the cycle lands
    in the catch-all handler -- and recording [] there would leave articles
    the subscriber genuinely received still eligible, so they would be sent
    a second time.

    Simulated by making the outcome insert raise, which is the first thing
    that runs after a successful send."""
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers", lambda: [_subscriber(41)])
    record_push = MagicMock()
    monkeypatch.setattr(users_db, "record_push", record_push)
    _stub_cache_and_categories(monkeypatch)
    monkeypatch.setattr(news_push, "select_candidate_articles",
                        MagicMock(return_value=[{**_article("https://example.com/new"), "topic": "AI"}]))
    digest = '<b>Digest</b> 🔗 <a href="https://example.com/new">Source</a>'
    monkeypatch.setattr(news_push, "write_push_digest", MagicMock(return_value=digest))
    monkeypatch.setattr(news_push.guardrails, "is_output_on_topic", MagicMock(return_value=True))

    # Everything up to and including the send works; the outcome write for
    # `delivered` does not. Transient rather than permanent, because a
    # permanently failing writer also breaks the error handler's own
    # _record call and aborts the whole tick -- a separate weakness, noted
    # in docs/plans/incident-monitoring-plan.md, not what this pins.
    monkeypatch.setattr(users_db, "record_push_outcome",
                        MagicMock(side_effect=[RuntimeError("database is locked"), None]))

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=AsyncMock(), now=now))

    record_push.assert_called_once_with(41, ["https://example.com/new"], now)


def test_a_failure_before_the_send_retires_nothing(monkeypatch, isolated_subscribers_db):
    """The other side of the same rule: a digest that was generated but
    never delivered must not retire its articles, or they are lost unread."""
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers", lambda: [_subscriber(42)])
    record_push = MagicMock()
    monkeypatch.setattr(users_db, "record_push", record_push)
    _stub_cache_and_categories(monkeypatch)
    monkeypatch.setattr(news_push, "select_candidate_articles",
                        MagicMock(return_value=[{**_article("https://example.com/new"), "topic": "AI"}]))
    monkeypatch.setattr(news_push, "write_push_digest", MagicMock(return_value="<b>Digest</b>"))
    monkeypatch.setattr(news_push.guardrails, "is_output_on_topic",
                        MagicMock(side_effect=RuntimeError("rate limited")))

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=AsyncMock(), now=now))

    record_push.assert_called_once_with(42, [], now)


def test_heartbeat_span_carries_the_job_and_the_subscriber_count(monkeypatch):
    """The attributes, not just the call. The count is what makes this span
    useful beyond liveness -- its quiet growth was the 2026-08-21 incident
    -- so a future edit that drops it should fail here. Same FakeSpan
    pattern as test_news_sources' redaction test."""
    recorded = {}

    class FakeSpan:
        def set_attribute(self, k, v):
            recorded[k] = v
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(news_push._tracer, "start_as_current_span",
                        lambda name: FakeSpan())

    news_push._emit_heartbeat(7)

    assert recorded == {"heartbeat.job": "push_tick",
                        "heartbeat.push_enabled_subscribers": 7}


def test_record_emits_a_span_an_alert_can_query(monkeypatch, isolated_subscribers_db):
    """The third reader. Criteria 2 and 3 live in Logfire, and Logfire can
    only alert on what it receives -- the SQLite rows sit on the bot VM
    where the alerting engine cannot see them.

    `push.generated` is computed here rather than left for each alert query
    to re-derive the outcome set: the ratio's denominator has one
    definition, in users_db, and a query that reimplements it would drift
    from it silently."""
    recorded = {}

    class FakeSpan:
        def set_attribute(self, k, v):
            recorded[k] = v
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(news_push._tracer, "start_as_current_span",
                        lambda name: FakeSpan())
    now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)

    news_push._record(55, users_db.PUSH_CHAT_NOT_FOUND, "gone", now, detail="BadRequest")

    assert recorded == {
        "push.outcome": users_db.PUSH_CHAT_NOT_FOUND,
        "push.chat_id": 55,
        "push.generated": True,      # billed: the digest was written before the send
        "push.detail": "BadRequest",
    }


def test_record_marks_a_cycle_that_cost_nothing_as_not_generated(monkeypatch, isolated_subscribers_db):
    """The other side of the denominator. A subscriber with nothing new
    costs no LLM call, and counting them would let a crowd of idle
    subscribers hide a collapsed delivery rate."""
    recorded = {}

    class FakeSpan:
        def set_attribute(self, k, v):
            recorded[k] = v
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(news_push._tracer, "start_as_current_span",
                        lambda name: FakeSpan())

    news_push._record(56, users_db.PUSH_NOTHING_NEW, "quiet",
                      datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc))

    assert recorded["push.generated"] is False
    assert "push.detail" not in recorded      # nothing to say, so nothing sent
