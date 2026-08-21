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
