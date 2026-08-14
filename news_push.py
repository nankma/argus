"""
Periodic news-push digest: fetch, filter-to-new, write, send. See
docs/bot-features-plan.md item 5.

Deliberately does NOT go through agent.py's tool-calling agent
(build_agent/run_agent) to gather articles: if it did, the model would
decide on its own how to call search_news, with no guarantee it wouldn't
just re-fetch and re-report the same top-N-by-recency articles every push
-- exactly the "always returns similar news" complaint that also motivated
news_sources.py's published_dt work. Instead this module fetches
deterministically via news_sources directly, filters to only genuinely-new
articles, and only then asks the model to write a report from that
pre-filtered list (a single plain model.invoke, not the full agent loop --
there's nothing left for tools to do once the articles are already in
hand) -- guaranteeing no repeats instead of hoping the model avoids them.

"New" is judged two ways, per user request: primarily by published_dt
(skip anything published at or before the user's last push), and as a
fallback for sources whose date didn't parse, by checking against
users_db's remembered pushed_links from recent pushes.
"""

from datetime import datetime, timezone

import agent
import guardrails
import news_sources
import users_db

MAX_RESULTS_PER_SOURCE = 5

_PUSH_DIGEST_PROMPT = (
    "You are a technology industry analyst writing a periodic news digest "
    "for a Telegram subscriber, covering AI and the broader tech industry. "
    "Below is a list of articles that are new since their last digest, "
    "grouped by the topic they matched. Write a short trend report "
    "covering ONLY these articles -- do not invent or reference anything "
    "else, and do not mention that this is an automated or periodic "
    "message.\n\n" + agent.HTML_FORMATTING_RULES + "\n\n" + agent.TREND_REPORT_STRUCTURE
)


def fetch_new_articles(
    topics: list[str],
    since: datetime | None,
    already_pushed_links: set[str],
    max_results_per_source: int = MAX_RESULTS_PER_SOURCE,
    include_restricted: bool = False,
) -> list[dict]:
    """Fetches across all enabled sources for each topic, returns only
    articles judged "new": published after `since` when published_dt
    parsed successfully, otherwise not in `already_pushed_links`.
    Deduplicated by link across topics/sources within this call too.

    `include_restricted` defaults to False (unlike news_sources.
    enabled_sources itself) because a push cycle has no per-request
    caller to gate the way agent.py's search_news tool does -- the
    subscriber's own restricted_sources_enabled flag must be passed
    explicitly by the caller (see run_push_cycle) or this silently
    reverts to including NewsAPI/Perigon for every subscriber, not just
    admin-approved ones. Real incident, 2026-08-14: this defaulted to
    True (via enabled_sources' own default) for every push cycle before
    this parameter existed, so restricted sources were live in every
    subscriber's push digest regardless of their DB flag."""
    seen_links = set()
    new_articles = []
    for topic in topics:
        for _name, fetch in news_sources.enabled_sources(include_restricted=include_restricted):
            try:
                articles = fetch(topic, max_results_per_source)
            except Exception:
                continue
            for article in articles:
                link = article.get("link")
                if not link or link in seen_links:
                    continue
                published_dt = article.get("published_dt")
                if published_dt is not None:
                    if since is not None and published_dt <= since:
                        continue
                elif link in already_pushed_links:
                    continue
                seen_links.add(link)
                new_articles.append({**article, "topic": topic})
    return new_articles


def write_push_digest(model, articles: list[dict], language: str | None = None) -> str:
    """A single direct model call (no tool loop -- see module docstring)
    that turns a pre-filtered article list into a Telegram HTML digest.
    `language`, when set, is the subscriber's stored reply-language
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


def is_subscriber_due(last_push_at: datetime | None, interval_hours: int, now: datetime) -> bool:
    if last_push_at is None:
        return True
    elapsed_hours = (now - last_push_at).total_seconds() / 3600
    return elapsed_hours >= interval_hours


async def run_push_cycle(model, send: "callable", now: datetime | None = None) -> None:
    """One scheduler tick: for every push-enabled, due subscriber with at
    least one interest, fetch new articles, and if there are any, write
    and send a digest. `send` is `async def send(chat_id, html_text)`
    (bound to the real bot's send_message in production, faked in tests)
    -- kept generic so this module doesn't need a live Bot/Application to
    be tested. One subscriber's failure (a bad fetch, a blocked send)
    doesn't stop the others, same isolation pattern as search_news's
    per-source error handling -- but unlike that isolation, every outcome
    here is printed (docker logs captures stdout) rather than swallowed
    silently, including ticks where nobody was due -- not just when
    something actually sends.

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
    subscribers = users_db.list_push_enabled_subscribers()
    print(f"[news_push] tick at {now.isoformat()}: {len(subscribers)} push-enabled subscriber(s)")
    for subscriber in subscribers:
        chat_id = subscriber["chat_id"]
        interests = subscriber["interests"]
        if not interests:
            print(f"[news_push] chat_id={chat_id}: push enabled but no interests set -- skipping")
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

        try:
            new_articles = fetch_new_articles(
                interests,
                subscriber["last_push_at"],
                set(subscriber["pushed_links"]),
                include_restricted=subscriber["restricted_sources_enabled"],
            )
            if not new_articles:
                # Nothing new this cycle -- still advance last_push_at so
                # the next check is a full interval away instead of
                # re-fetching every tick until something new shows up.
                print(f"[news_push] chat_id={chat_id}: due, but no new articles -- advancing last_push_at only")
                users_db.record_push(chat_id, [], now)
                continue

            digest = write_push_digest(model, new_articles, subscriber["language"])

            # A stored interest is user-supplied, unsanitized text (see
            # agent.py's update_interests) that ends up embedded in the
            # digest prompt above -- the same output-guardrail check
            # bot.py runs on chat replies applies here too, since this is
            # also model output about to be sent to a real user unread.
            if guardrails.is_output_on_topic(model, digest):
                await send(chat_id, digest)
                print(f"[news_push] chat_id={chat_id}: sent digest with {len(new_articles)} article(s)")
            else:
                print(f"[news_push] chat_id={chat_id}: digest blocked by output guardrail, not sent")

            # Advance the dedup state regardless of whether the guardrail
            # blocked the send: these articles were considered this cycle
            # either way, and not recording them would just retry (and
            # re-fetch) every tick until the interval naturally moves on.
            users_db.record_push(chat_id, [a["link"] for a in new_articles], now)
        except Exception as exc:
            print(f"[news_push] chat_id={chat_id}: cycle failed with {exc!r}")
            continue
