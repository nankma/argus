"""
Periodic news-push digest: read the shared local cache, filter to new +
relevant, write, send. See docs/plans/bot-features-plan.md item 5 and
docs/plans/local-news-cache-plan.md's "Interaction with news_push.py" section.

Reads from news_cache.py (populated on a schedule by news_ingest.py)
rather than calling news_sources.py live -- converged onto the cache
2026-08-15, per docs/plans/local-news-cache-plan.md's item 6. Before this, every
push cycle called every enabled source live for every subscriber's every
interest, with no relevance filter beyond "does the source's own query
support happen to work" -- most sources are query-blind RSS feeds that
return their latest N items regardless of query (see news_sources.py),
so a subscriber's digest could and did include whatever a broad
mainstream-press feed's front page happened to be that day (a real
incident: Nikkei Asia's general top-stories feed put an Indonesia
earthquake and a Japan-society piece into a push digest -- nothing tech-
related about either, and nothing in the old pipeline could have caught
it, since no relevance check existed before the digest-writing prompt).

Two-stage filtering, per docs/plans/local-news-cache-plan.md:
  Stage 1 (category filter, in code) -- select_candidate_articles narrows
  the shared cache to articles whose classifier-assigned categories
  overlap with a subscriber's own classified interests, before the model
  ever sees anything.
  Stage 2 (content filter) -- folded into the existing single
  write_push_digest call rather than a separate LLM call: the prompt now
  explicitly tells the model to still exercise judgment and omit
  candidates that survived the category filter but aren't genuinely
  about the topic, instead of forcing everything given into the report.

Deliberately still does NOT go through agent.py's tool-calling agent
(build_agent/run_agent): a single plain model.invoke (no tool loop) is
enough once the candidate list is already assembled in code -- there's
nothing left for tools to do.

"New" separates two things that were previously conflated, and the
distinction is load-bearing -- see select_candidate_articles:

- FILTERING is done by users_db's pushed_links alone. "Already seen" means
  "we actually sent this to this subscriber", nothing else.
- RANKING is done by published_dt, newest first, with a maximum-age gate.
  A publish date decides what's most worth showing, never what's eligible.

Filtering on the date was the earlier design and it was wrong: an article
that lost one cycle's cut was excluded permanently, without ever having
been seen by anyone.
"""

import re
from datetime import datetime, timedelta, timezone

import agent
import guardrails
import healthcheck
import news_cache
import news_classify
import news_sources
import users_db
from opentelemetry import trace

MAX_ARTICLES_PER_TOPIC = 5

# Ceiling on how old an article's own publication date may be to still be
# pushed, regardless of when we downloaded it. Needed as of 2026-08-19,
# when candidate selection switched from published_dt to fetched_at (see
# select_candidate_articles): "new to us" is the right test for a source
# that publishes on a delay, but on its own it would also happily push
# genuinely ancient content. Real case that forced this: Perigon's one
# successful fetch returned 50 articles whose NEWEST was over a year old
# (docs/plans/security-plan.md finding 21) -- under a fetched_at-only rule
# every one of them would have been eligible.
#
# 7 days is deliberately generous rather than tight. It exists to exclude
# absurdly stale content, not to enforce freshness -- the fetched_at check
# already does that. arXiv's own indexing runs ~3 days behind
# (docs/current/ai-news-sources.md), and those papers are legitimately worth
# sending, so a tighter bound would silently drop a real source.
MAX_ARTICLE_AGE_HOURS = 168

_PUSH_DIGEST_PROMPT = (
    "You are a technology industry analyst writing a periodic news digest "
    "for a Telegram subscriber, covering AI and the broader tech industry. "
    "Below is a list of candidate articles that are new since their last "
    "digest, grouped by the topic they matched during a coarse category "
    "filter. That filter is not perfect -- some candidates may not "
    "actually be about the subscriber's topic, or may not be genuinely "
    "tech-industry content at all (e.g. general news that happened to "
    "come from a source that also covers tech). Use your own judgment: "
    "write a short trend report covering ONLY the candidates that are "
    "genuinely relevant, silently omitting any that aren't -- do not "
    "force an irrelevant candidate into the report just because it was "
    "in the list, and do not invent or reference anything not in the "
    "list either. If NONE of the candidates are genuinely relevant, "
    "write nothing (an empty reply) rather than reporting on off-topic "
    "content. Do not mention that this is an automated or periodic "
    "message, and do not mention the filtering process itself.\n\n"
    + agent.HTML_FORMATTING_RULES
    + "\n\n"
    + agent.TREND_REPORT_STRUCTURE
)


def resolve_interest_categories(model, interests: list[str]) -> dict[str, list[str]]:
    """Stage-1 setup: maps each interest to its category tags, using
    users_db's persistent cache and classifying only what's missing --
    interest text is stable vocabulary (unlike article content), so this
    should be a cache hit for any interest that's been pushed before.

    An interest the classifier FAILED on is left out of the cache entirely,
    so the next cycle retries it. Only a real answer gets cached -- including
    a genuinely empty one, which is a valid classification and should not be
    re-paid for every cycle.

    Caching failures as [] is what poisoned the live cache during the
    2026-08-17 classification outage: "AI", "Bitcoin", "機器人科技" and
    others were all stored as [], permanently, with no retry. An empty
    mapping matches every article (see select_candidate_articles), so those
    subscribers were receiving entirely unfiltered news -- the exact failure
    the category filter exists to prevent."""
    resolved = users_db.get_cached_interest_categories(interests)
    missing = [i for i in interests if i not in resolved]
    if missing:
        taxonomy = news_classify.Taxonomy.from_rows(users_db.get_active_categories())
        newly_classified = news_classify.classify_interests(model, missing, taxonomy)
        failed = [i for i in missing if i not in newly_classified]
        for interest, categories in newly_classified.items():
            users_db.set_interest_categories(interest, categories)
            resolved[interest] = categories
        if failed:
            print(f"[news_push] could not classify {len(failed)} interest(s), "
                  f"will retry next cycle: {', '.join(failed)}")
    return resolved


def select_candidate_articles(
    cached_articles: list[dict],
    topics: list[str],
    topic_categories: dict[str, list[str]],
    since: datetime | None,
    already_pushed_links: set[str],
    include_restricted: bool = False,
    max_per_topic: int = MAX_ARTICLES_PER_TOPIC,
    now: datetime | None = None,
) -> list[dict]:
    """Stage 1 (category filter): narrows the shared cache to one
    subscriber's candidate articles, before the digest-writing model
    (stage 2, in write_push_digest's prompt) ever sees anything.

    An article is a candidate for `topic` when its own categories (set at
    ingestion time by news_classify.py) overlap with that topic's mapped
    categories from `topic_categories`. A topic that mapped to NO
    categories at all (a classifier miss) is treated as unrestricted --
    matches any article -- rather than matching nothing: a subscriber's
    own stated interest is presumably tech/industry-relevant by
    construction (they set it on a tech-industry bot), and a
    classification miss on the INTEREST shouldn't silently starve them.
    An article with no categories (the classifier found nothing that
    plausibly applies, e.g. a general-news piece with no tech angle at
    all) is excluded whenever the topic itself has real categories to
    match against -- this is the exact mechanism that would have kept
    the Nikkei Asia earthquake/society articles out of a digest, since
    neither classifies into any of the 13 tech-industry categories.

    Since 2026-08-20 the cache distinguishes "the classifier found nothing
    applicable" (users_db.UNCLASSIFIABLE) from "never classified" (None).
    This function deliberately still treats them alike -- neither has a
    category that can match a topic, so both are excluded the same way --
    but the distinction now exists in the data for anything that needs it,
    which is how a silent classification outage becomes detectable.
    Known, accepted overlap with a separate case this can't distinguish:
    an article left uncategorized because news_classify.classify_articles
    failed for its whole ingestion batch (fails open, see that
    function's docstring) looks identical to a genuine "nothing applies"
    result here -- both get excluded. Not solved here; a batch
    classification failure already means that cycle's articles are
    "harder to find... until the next cycle re-fetches and reclassifies
    it," per that function's own docstring, so this is consistent with
    an existing accepted limitation, not a new one.

    **A date is a ranking signal, never a "have they seen it" filter.**
    Restructured 2026-08-19 around that separation:

    - **Filter** -- `already_pushed_links`, and nothing else. That set is
      the direct record of what this subscriber was actually sent, so it
      answers the question exactly rather than approximating it.
    - **Rank** -- `published_dt`, newest first. What a date is genuinely
      good for.
    - **Quality gate** -- `MAX_ARTICLE_AGE_HOURS`, which drops absurdly
      stale articles regardless of when we downloaded them. A gate on
      worth-sending, not on already-seen.

    Why this matters, from two real failures. The original code filtered on
    `published_dt <= since`, which structurally excluded every source with
    a publication delay: GNews publishes ~12h behind, so for any subscriber
    pushed more recently than that, its articles were skipped outright --
    227 of them sat in the cache, correctly fetched and classified, and not
    one could ever reach a digest. Switching that filter to `fetched_at`
    fixed the delay case but kept the deeper flaw: an article that was a
    candidate and simply lost the `max_per_topic` cut would have its
    timestamp fall behind the next `since` and be excluded forever, unsent
    and unrecorded.

    Both failures come from the same mistake -- using a timestamp as a
    proxy for "already seen" when the actual record exists. Anything not
    yet sent stays a candidate until it is sent or ages out of the cache,
    so the pool drains in publication order and nothing starves.

    Restricted-source gating is unchanged: NewsAPI/Perigon articles are
    skipped entirely unless `include_restricted`.

    `since` is retained in the signature but no longer filters -- it stays
    only because callers already pass it and removing it would be a
    breaking change for no gain. `now` is a parameter rather than read from
    the clock so the age guard is deterministic in tests, same convention
    as run_push_cycle's own `now`."""
    now = now or datetime.now(timezone.utc)
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    ordered = sorted(
        cached_articles,
        key=lambda a: a.get("published_dt") or epoch,
        reverse=True,
    )
    max_age = timedelta(hours=MAX_ARTICLE_AGE_HOURS)
    seen_links = set()
    candidates = []
    for topic in topics:
        topic_cats = set(topic_categories.get(topic, []))
        count_for_topic = 0
        for article in ordered:
            if count_for_topic >= max_per_topic:
                break
            link = article.get("link")
            if not link or link in seen_links:
                continue
            if not include_restricted and article.get("source_key") in news_sources.RESTRICTED_SOURCES:
                continue
            article_cats = set(article.get("categories") or [])
            if topic_cats and not (article_cats & topic_cats):
                continue
            # The one and only "has this subscriber seen it" test.
            if link in already_pushed_links:
                continue
            # Not already-seen, but not worth sending either if it's ancient.
            # Articles whose published_dt didn't parse pass this guard rather
            # than being dropped -- same fail-open instinct as the rest of
            # the pipeline.
            published_dt = article.get("published_dt")
            if published_dt is not None and now - published_dt > max_age:
                continue
            seen_links.add(link)
            candidates.append({**article, "topic": topic})
            count_for_topic += 1
    return candidates


def write_push_digest(model, articles: list[dict], language: str | None = None) -> str:
    """A single direct model call (no tool loop -- see module docstring)
    that turns a stage-1-filtered candidate list into a Telegram HTML
    digest, applying stage-2 (content) filtering itself per the prompt
    above. `language`, when set, is the subscriber's stored reply-language
    preference (users_db.get_language) -- pushes go through this module's
    own prompt rather than agent.py's dynamic_prompt middleware, so the
    preference has to be threaded in here too, not just in _compose_prompt."""
    listing = "\n".join(
        f"- [{a.get('topic')}] {a['title']} ({a.get('source')}, "
        f"published {a.get('published') or 'date unknown'}) — {a.get('link')}"
        for a in articles
    )
    system_prompt = _PUSH_DIGEST_PROMPT
    if language:
        system_prompt += (
            f"\n\nWrite your ENTIRE reply in {language}, regardless of what "
            "language the article titles/summaries above are in. If this "
            "is a specific script/variant (e.g. Traditional vs Simplified "
            "Chinese, Brazilian vs European Portuguese), use exactly that "
            "variant's script and spelling conventions throughout."
        )
    response = model.invoke(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": listing},
        ]
    )
    return response.content


_HREF_RE = re.compile(r"""href=["']([^"']+)["']""", re.IGNORECASE)


def links_actually_sent(digest: str, candidates: list[dict]) -> list[str]:
    """Which candidate articles genuinely appear in the delivered digest.

    The digest is free-form prose the model writes from the candidate list,
    and _PUSH_DIGEST_PROMPT explicitly tells it to omit candidates that
    aren't relevant -- so "what we offered" and "what the subscriber saw"
    are different sets. Only the latter should count as seen: marking an
    omitted candidate as sent would retire an article nobody ever read.

    Recovered by matching the digest's own <a href> targets against the
    candidate links, since TREND_REPORT_STRUCTURE requires every cited item
    to carry its source link and forbids inventing URLs."""
    hrefs = {h.strip() for h in _HREF_RE.findall(digest or "")}
    return [a["link"] for a in candidates if a.get("link") in hrefs]


# Liveness for the dead man's switch in
# docs/plans/observability-platform-plan.md.
#
# The alarm asks "has ANY span arrived in the last 30 minutes". Without
# this, the answer is legitimately "no" on a healthy system: every LLM call
# in a push cycle sits inside the per-subscriber loop AFTER the due check,
# so a tick where nobody is due emits nothing at all. Two subscribers on a
# 6-hour interval leave most of the 96 daily ticks silent, and an alarm
# that cries wolf on an idle Sunday is an alarm nobody reads.
#
# A no-op when no tracer provider is configured -- which is every test and
# CI run -- because OpenTelemetry's default is a no-op tracer. No env check
# needed, and nothing to remember to stub.
_tracer = trace.get_tracer("argus.news_push")


def _emit_heartbeat(subscriber_count: int) -> None:
    """One span per tick, whether or not there was any work to do.

    Carries the subscriber count as an attribute rather than just existing:
    the same span then answers "is it running" and "how many subscribers
    did it see", and the second is the number whose sudden growth was the
    2026-08-21 incident."""
    with _tracer.start_as_current_span("argus_heartbeat") as span:
        span.set_attribute("heartbeat.job", "push_tick")
        span.set_attribute("heartbeat.push_enabled_subscribers", subscriber_count)


class _ModelStageError(Exception):
    """Marks a failure that came out of an LLM call rather than out of
    local filtering or the database.

    The alternative -- deciding after the fact whether an exception "looks
    like" a model error by matching its message -- breaks the first time a
    provider rewords one, and breaks silently, in the direction of
    under-counting. Classifying by WHERE the call was made cannot drift:
    `_model_call` wraps exactly the LLM calls and nothing else."""


def _model_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        raise _ModelStageError(exc) from exc


# Telegram's wording for "this chat can no longer receive anything."
# Both are permanent for practical purposes and both cost a full digest
# generation per cycle, so criterion 1 treats them identically.
_UNREACHABLE_CHAT_MARKERS = (
    "chat not found",       # BadRequest -- the chat never existed or was deleted
    "bot was blocked",      # Forbidden -- the user blocked the bot
    "user is deactivated",  # Forbidden -- the account is gone
)


def _classify_send_failure(exc: Exception) -> str:
    """Narrows a delivery failure to chat_not_found where the message says
    so, and leaves everything else as a generic cycle failure.

    Matching on the message rather than the exception type because the
    type does not separate these: `BadRequest` covers both a dead chat and
    malformed HTML, and only the text tells them apart. The failure mode
    of a reworded message is that a genuinely unreachable chat records as
    `cycle_failed` -- criterion 1 goes quiet, rather than every failure
    being mislabelled as unreachable and subscribers being disabled
    wrongly. Wrong in the safe direction, deliberately."""
    text = str(exc).lower()
    if any(marker in text for marker in _UNREACHABLE_CHAT_MARKERS):
        return users_db.PUSH_CHAT_NOT_FOUND
    return users_db.PUSH_CYCLE_FAILED


# Consecutive undeliverable digests before push is turned off for that
# subscriber -- criterion 1 in docs/plans/incident-monitoring-plan.md.
#
# Three rather than one because delivery can fail for reasons that are not
# about the chat being gone, and turning a real subscriber off is the more
# expensive mistake of the two: they simply stop receiving news, with no
# error to notice. Three consecutive failures with no successful delivery
# between them is not a blip.
UNREACHABLE_STRIKES = 3


def _strike_unreachable_subscriber(chat_id: int, now: datetime) -> None:
    """Turns push off once a chat has been undeliverable UNREACHABLE_STRIKES
    times running.

    This is the bound on the 2026-08-21 failure mode. An unreachable chat
    still costs a full digest generation -- three LLM calls -- every cycle,
    because generation happens before delivery is attempted and is billed
    whether or not the send lands. Without a stop, that recurs for as long
    as the subscriber row exists: the leak was open for eight days.

    Reversible on purpose: only `push_enabled` is cleared. The subscriber,
    their interests and their language survive, so a user who blocked the
    bot and later unblocks it turns push back on and continues, rather than
    discovering their settings were deleted.

    Re-enabling does NOT reset the strike count -- only a delivered digest
    does (see users_db.consecutive_chat_not_found). So a subscriber who
    turns push back on while still unreachable is disabled again after a
    single further failure rather than after three. That is deliberate: a
    transient outage records `cycle_failed` and accrues no strikes at all,
    so one more `chat_not_found` really does mean still unreachable, and
    granting a fresh allowance would just pay for three more undeliverable
    digests."""
    strikes = users_db.consecutive_chat_not_found(chat_id)
    if strikes < UNREACHABLE_STRIKES:
        return
    users_db.set_push_enabled(chat_id, False)
    detail = f"undeliverable {strikes} cycles running"
    _record(chat_id, users_db.PUSH_DISABLED,
            f"{detail} -- push turned off (they can turn it back on)",
            now, detail=detail)


def _record(chat_id: int, outcome: str, message: str, now: datetime,
            detail: str | None = None) -> None:
    """Reports a push outcome three ways, from one call site so they cannot
    disagree: a log line, a database row, and a span.

    Each reaches a different reader and none replaces the others.

    The PRINT goes to `docker logs`, where someone looks when debugging a
    specific cycle. It is free text with no timestamp of its own, it cannot
    be read from inside the container, and a deploy destroys it -- fine for
    a human reading a single cycle, useless as an alarm's input.

    The ROW is what `/status` and any local query read. It survives with no
    network at all, which is what makes it the floor: if telemetry export
    breaks, this still answers "what happened".

    The SPAN is what an ALARM can query. Criteria 2 and 3 of
    docs/plans/incident-monitoring-plan.md live in Logfire, and Logfire can
    only alert on what it has received -- the SQLite rows sit on the bot VM
    where the alerting engine cannot see them. A no-op when no tracer
    provider is configured, which is every test and CI run."""
    print(f"[news_push] chat_id={chat_id}: {message}")
    users_db.record_push_outcome(chat_id, outcome, now, detail=detail)
    with _tracer.start_as_current_span("push_outcome") as span:
        # Flat, low-cardinality attributes: an alert query filters on
        # `outcome` and counts, so it must not have to parse prose.
        span.set_attribute("push.outcome", outcome)
        span.set_attribute("push.chat_id", chat_id)
        # Whether an LLM was paid for this cycle -- criterion 3's
        # denominator, computed here rather than by re-deriving the outcome
        # set inside every alert query.
        span.set_attribute("push.generated", outcome in users_db.PUSH_GENERATED_OUTCOMES)
        if detail:
            span.set_attribute("push.detail", detail)


def is_subscriber_due(last_push_at: datetime | None, interval_hours: int, now: datetime) -> bool:
    if last_push_at is None:
        return True
    elapsed_hours = (now - last_push_at).total_seconds() / 3600
    return elapsed_hours >= interval_hours


async def run_push_cycle(model, send: "callable", now: datetime | None = None) -> None:
    """One scheduler tick: for every push-enabled, due subscriber with at
    least one interest, select candidate articles from the shared cache,
    and if there are any, write and send a digest. `send` is
    `async def send(chat_id, html_text)` (bound to the real bot's
    send_message in production, faked in tests) -- kept generic so this
    module doesn't need a live Bot/Application to be tested. One
    subscriber's failure doesn't stop the others, same isolation pattern
    as search_news's per-source error handling -- but unlike that
    isolation, every outcome here is both printed (docker logs captures
    stdout) and recorded in push_outcomes rather than swallowed silently,
    including ticks where nobody was due -- not just when something
    actually sends. See _record for why both, and
    docs/plans/incident-monitoring-plan.md for what reads the rows.

    The cache is read ONCE per cycle and reused across every subscriber
    -- matches docs/plans/local-news-cache-plan.md's stated efficiency argument
    for a shared cache ("one Perigon call can satisfy every subscriber
    whose interests match it"), and avoids N redundant directory scans
    for N due subscribers in the same tick.

    Real incident, 2026-08-09, two parts: (1) a subscriber reported never
    receiving a push despite users_db showing a completed cycle -- there
    was no way to confirm from logs alone whether `send` actually ran,
    since nothing was ever printed either way; (2) fixing that alone
    turned out insufficient -- the container's stdout was block-buffered
    (Python's default when stdout isn't a TTY, true for any `docker run
    -d` container), so prints were never reaching `docker logs` at all
    regardless of what they said (fixed separately: PYTHONUNBUFFERED=1
    in the Dockerfile). Once both were fixed, a THIRD gap remained: the
    same subscriber asked why a nominal 30-minute interval sometimes took
    close to 45 -- unanswerable because only *due* subscribers were ever
    logged, not the tick itself or why a not-yet-due subscriber was
    skipped. This function now logs every tick's summary and every
    subscriber's due-check outcome, not just successful sends."""
    now = now or datetime.now(timezone.utc)
    # Recorded unconditionally, before any per-subscriber due-check -- see
    # healthcheck.py's docstring for why: every subscriber can legitimately
    # be "not due yet" for hours (push_interval_hours), which isn't the
    # same as this job having stopped running entirely.
    users_db.set_source_last_pulled_at(healthcheck.PUSH_TICK_KEY, now)
    # Bounded here rather than on a timer of its own: this is the only job
    # that writes the table, so it is the only one that can grow it.
    users_db.prune_push_outcomes(now)
    subscribers = users_db.list_push_enabled_subscribers()
    print(f"[news_push] tick at {now.isoformat()}: {len(subscribers)} push-enabled subscriber(s)")
    _emit_heartbeat(len(subscribers))
    cached_articles = news_cache.read_all()
    for subscriber in subscribers:
        chat_id = subscriber["chat_id"]
        interests = subscriber["interests"]
        if not interests:
            _record(chat_id, users_db.PUSH_NO_INTERESTS,
                    "push enabled but no interests set -- skipping", now)
            continue
        last_push_at = subscriber["last_push_at"]
        interval_hours = subscriber["push_interval_hours"]
        if not is_subscriber_due(last_push_at, interval_hours, now):
            elapsed = (now - last_push_at).total_seconds() / 3600 if last_push_at else None
            print(
                f"[news_push] chat_id={chat_id}: not due yet "
                f"(interval={interval_hours}h, elapsed={elapsed:.2f}h)"
            )
            continue
        print(f"[news_push] chat_id={chat_id}: due -- checking for new articles")

        # What record_push must be told, and whether it must be told at
        # all. None until an LLM has been paid to write a digest; a list
        # from then on. Load-bearing twice over:
        #
        # Not-None means the money is spent, so the next attempt must be a
        # full interval away even though this cycle failed. Leaving
        # last_push_at untouched instead -- which is what every failure
        # path used to do -- makes the subscriber due again on the very
        # next tick, and the tick is PUSH_TICK_SECONDS (15 minutes), not
        # their interval.
        #
        # Its CONTENTS matter for a narrower case: a send can succeed and a
        # later step still raise (the outcome insert, or record_push
        # itself). Recording [] there would leave articles the subscriber
        # genuinely received still eligible, and they would be sent again.
        delivered: list[str] | None = None
        try:
            topic_categories = _model_call(resolve_interest_categories, model, interests)
            new_articles = select_candidate_articles(
                cached_articles,
                interests,
                topic_categories,
                subscriber["last_push_at"],
                set(subscriber["pushed_links"]),
                include_restricted=subscriber["restricted_sources_enabled"],
                now=now,
            )
            if not new_articles:
                # Nothing new this cycle -- still advance last_push_at so
                # the next check is a full interval away instead of
                # re-fetching every tick until something new shows up.
                _record(chat_id, users_db.PUSH_NOTHING_NEW,
                        "due, but no new articles -- advancing last_push_at only", now)
                users_db.record_push(chat_id, [], now)
                continue

            digest = _model_call(write_push_digest, model, new_articles, subscriber["language"])
            delivered = []          # generated; nothing has reached them yet

            if not digest or not digest.strip():
                # Stage 2 (the model's own content judgment) decided none
                # of the stage-1 candidates were genuinely relevant -- see
                # _PUSH_DIGEST_PROMPT's explicit "write nothing" instruction.
                # Not an error. last_push_at still advances (record_push does
                # that regardless) so the next attempt is a full interval
                # away rather than retrying every tick.
                #
                # Records NO links, because nothing was sent. Recording the
                # candidates here would retire articles the subscriber never
                # saw: pushed_links is the "already seen" filter, and an
                # article that merely lost this cycle's relevance judgment
                # has not been seen and must stay eligible for a later
                # digest. Same rule as the guardrail-blocked branch below
                # and as links_actually_sent.
                _record(chat_id, users_db.PUSH_NOT_RELEVANT,
                        "candidates found but none judged relevant -- not sending", now)
                users_db.record_push(chat_id, [], now)
                continue

            # A stored interest is user-supplied, unsanitized text (see
            # agent.py's dispatch_settings) that ends up embedded in the
            # digest prompt above -- the same output-guardrail check
            # bot.py runs on chat replies applies here too, since this is
            # also model output about to be sent to a real user unread.
            if _model_call(guardrails.is_output_on_topic, model, digest):
                try:
                    await send(chat_id, digest)
                except Exception as exc:
                    # Generation already happened and was already billed;
                    # only delivery failed. Recorded as its own outcome so
                    # "we paid to write digests nobody can receive" is a
                    # question the data can answer -- the exact shape of
                    # the 2026-08-21 incident, which until now landed in
                    # the catch-all below as an ordinary "cycle failed".
                    outcome = _classify_send_failure(exc)
                    detail = repr(exc)
                    _record(chat_id, outcome,
                            f"digest generated but delivery failed with {detail}",
                            now, detail=detail)
                    # No links: nothing was seen, so nothing is retired.
                    # Same rule as the guardrail-blocked branch below.
                    users_db.record_push(chat_id, [], now)
                    if outcome == users_db.PUSH_CHAT_NOT_FOUND:
                        _strike_unreachable_subscriber(chat_id, now)
                    continue
                # Only what the subscriber actually saw is recorded as seen.
                # A candidate the model left out stays eligible for a later
                # digest rather than being silently retired unread -- see
                # links_actually_sent and select_candidate_articles.
                # The send landed, so retire what they actually saw before
                # anything below can fail -- see `delivered`'s comment.
                delivered = links_actually_sent(digest, new_articles)
                detail = f"{len(delivered)} of {len(new_articles)} candidate(s)"
                _record(chat_id, users_db.PUSH_DELIVERED,
                        f"sent digest -- {detail} appeared in it", now, detail=detail)
            else:
                # Blocked before delivery, so nothing was seen and nothing is
                # recorded as seen. last_push_at still advances (below), so
                # the next attempt is a full interval away rather than
                # retrying every tick.
                _record(chat_id, users_db.PUSH_BLOCKED,
                        "digest blocked by output guardrail, not sent", now)

            users_db.record_push(chat_id, delivered, now)
        except _ModelStageError as exc:
            # A 402, a rate limit, a provider outage. Unlike everything
            # else here this is never about one subscriber -- if the model
            # is down for this chat it is down for all of them -- which is
            # why criterion 2 alerts on a single occurrence rather than on
            # a threshold.
            detail = repr(exc.__cause__ or exc)
            _record(chat_id, users_db.PUSH_MODEL_ERROR,
                    f"model call failed with {detail}", now, detail=detail)
            if delivered is not None:
                users_db.record_push(chat_id, delivered, now)
            continue
        except Exception as exc:
            detail = repr(exc)
            _record(chat_id, users_db.PUSH_CYCLE_FAILED,
                    f"cycle failed with {detail}", now, detail=detail)
            if delivered is not None:
                users_db.record_push(chat_id, delivered, now)
            continue
