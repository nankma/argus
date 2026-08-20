"""
Batched article classification for the local news cache -- see
docs/plans/local-news-cache-plan.md's "Classification mechanism" section.

One structured-output LLM call per ingestion cycle classifies every
newly-fetched article in that cycle at once, rather than one call per
article -- at the volumes this project's source registry produces, a
per-article call would mean hundreds of LLM calls/day for a task a single
batched call handles just as well. Same DeepSeek instance already used
elsewhere (see docs/plans/local-news-cache-plan.md's resolved "classification
model" question) -- docs/plans/model-portability-plan.md's cheaper per-stage
routing can swap this later without a redesign.

The taxonomy below is an explicit v1, not a final answer -- see the plan
doc's "Category taxonomy" section for the reasoning (cheap to revise
later against real classified data, not worth blocking on getting it
perfect up front).
"""

from typing import Literal

from pydantic import BaseModel

Category = Literal[
    "AI",
    "Software",
    "Hardware",
    "IT",
    "Startups",
    "Finance",
    "Stock",
    "Policy",
    "Security",
    "Research",
    "Consumer",
    "Robotics",
    "Crypto",
]

CATEGORIES: list[str] = list(Category.__args__)
_CATEGORY_SET: frozenset[str] = frozenset(CATEGORIES)


class ArticleCategories(BaseModel):
    index: int
    # Deliberately list[str], not list[Category]. A Literal here makes the
    # whole batch fail validation when the model returns one label outside
    # the taxonomy, discarding every correctly-classified article alongside
    # it. That happened in production on 2026-08-20: the model answered
    # "Education" for a single article and pydantic rejected the entire
    # 50-article ClassificationBatch, losing 49 good classifications.
    #
    # An LLM occasionally inventing a plausible label is ordinary behaviour,
    # not an exceptional condition, so unknown labels get filtered out after
    # parsing (see _valid_categories) rather than being allowed to fail the
    # request. The prompt still lists the 13 categories, so the model is
    # still aimed at them; this only changes what happens when it misses.
    categories: list[str]


class ClassificationBatch(BaseModel):
    items: list[ArticleCategories]


_CLASSIFY_PROMPT = (
    "You are a strict classifier, not an assistant. Below is a numbered "
    "list of news article titles (and summaries, where available). For "
    "EACH article, assign every category from this list that plausibly "
    "applies -- most articles need more than one:\n\n"
    "- AI: AI models, research, agents, LLMs\n"
    "- Software: software products, dev tools, programming\n"
    "- Hardware: chips, semiconductors, devices, infrastructure hardware\n"
    "- IT: enterprise IT, cloud, infrastructure, enterprise software\n"
    "- Startups: funding rounds, new companies, venture capital\n"
    "- Finance: business/financial industry news, economics, corporate deals\n"
    "- Stock: stock price moves, market reactions specifically -- distinct "
    "from Finance, which covers business news generally\n"
    "- Policy: regulation, government, legal, antitrust\n"
    "- Security: cybersecurity, breaches, vulnerabilities\n"
    "- Research: academic papers, science\n"
    "- Consumer: consumer gadgets, reviews, product launches for individual users\n"
    "- Robotics: robotics specifically\n"
    "- Crypto: cryptocurrency/blockchain\n\n"
    "Return one entry per article, using its exact index number from the "
    "list below. If nothing applies, return an empty categories list for "
    "that index rather than guessing or omitting the entry."
)


def _format_batch(articles: list[dict]) -> str:
    lines = []
    for i, article in enumerate(articles):
        title = article.get("title") or "(no title)"
        summary = article.get("summary")
        line = f"{i}. {title}"
        if summary:
            line += f" -- {summary}"
        lines.append(line)
    return "\n".join(lines)


def classify_interests(model, interests: list[str]) -> dict[str, list[str]]:
    """Classifies subscriber-stated interest strings (e.g. "機器人科技",
    "AAOI") into the same category taxonomy as articles -- the stage-1
    filter news_push.py uses to narrow the shared cache to a subscriber's
    topics before the digest-writing model ever sees anything. Thin
    wrapper around classify_articles: each interest is treated as a
    single-line pseudo-article (title=interest text, no summary). Callers
    are expected to cache the result (see users_db.get_cached_interest_categories/
    set_interest_categories) rather than re-classifying on every push
    cycle -- interest text is stable vocabulary, not fresh content.

    Interests the classifier failed on are OMITTED from the result rather
    than mapped to []. The caller must not cache an absent interest: an
    empty list is a real answer ("no category applies") and gets cached
    forever, while a failure has to be retried. Collapsing the two is what
    poisoned the live cache during the classification outage -- "AI" ended
    up cached as [] even though AI is one of the 13 categories, and an
    empty mapping matches every article, so those subscribers were being
    sent completely unfiltered news."""
    if not interests:
        return {}
    articles = [{"title": interest, "summary": None} for interest in interests]
    result = classify_articles(model, articles)
    return {interest: result[i] for i, interest in enumerate(interests) if i in result}


# Articles per classification call. One call per ingestion cycle was the
# original design and it worked while a cycle produced ~100 articles; it
# stopped working when docs/plans/local-news-cache-plan.md item 7 raised the
# RSS per-source cap from 5 to 200 and cycles started producing 100-1000+.
#
# Measured from production on 2026-08-19, by grouping cached articles by the
# tick that wrote them:
#
#     batch    1  -> 100% categorized      batch  113 -> 0%
#     batch   60  ->  95% categorized      batch  147 -> 0%
#     batch  109  ->  96% categorized      batch 1085 -> 0%
#
# All-or-nothing, with the cliff somewhere just above 110. The failure is
# almost certainly the structured-output response exceeding the model's
# output token limit -- one entry per article, so response length scales
# with batch size. Whatever the precise cause, the fix is the same: keep
# each call's response small enough to complete.
#
# 50 is deliberately well under the observed cliff rather than just below
# it, since the cliff will move with prompt/model changes and the cost of
# an extra call is far lower than the cost of silently losing a batch.
MAX_ARTICLES_PER_CALL = 50


def _classify_one_batch(model, articles: list[dict]) -> dict[int, list[str]]:
    """One structured-output call. Returns {} on any failure -- see
    classify_articles for the fail-open reasoning."""
    try:
        structured = model.with_structured_output(ClassificationBatch)
        result = structured.invoke(
            [
                {"role": "system", "content": _CLASSIFY_PROMPT},
                {"role": "user", "content": _format_batch(articles)},
            ]
        )
    except Exception as exc:
        print(f"[news_classify] batch of {len(articles)} failed: {exc!r}")
        return {}
    if result is None:
        print(f"[news_classify] batch of {len(articles)} returned no result")
        return {}
    return _valid_categories(result)


def _valid_categories(result: ClassificationBatch) -> dict[int, list[str]]:
    """Drops labels the model invented that aren't in the taxonomy, keeping
    everything else. An article left with no valid labels keeps an empty
    list -- same as the model saying nothing applies.

    Rejected labels are logged rather than silently discarded, because they
    are useful signal in their own right: a label the model keeps reaching
    for is a gap in the taxonomy. "Education" is what surfaced this, and the
    13 categories in Category are explicitly a v1 (see the module docstring)
    meant to be revised against real classified data."""
    categories: dict[int, list[str]] = {}
    rejected: set[str] = set()
    for item in result.items:
        kept = []
        for name in item.categories:
            if name in _CATEGORY_SET:
                kept.append(name)
            else:
                rejected.add(name)
        categories[item.index] = kept
    if rejected:
        print(f"[news_classify] dropped {len(rejected)} label(s) outside the "
              f"taxonomy: {', '.join(sorted(rejected))}")
    return categories


def classify_articles(model, articles: list[dict]) -> dict[int, list[str]]:
    """Returns {index: categories} for every article in `articles` that the
    model actually returned an entry for, where the index is into
    `articles` as given.

    Split into chunks of MAX_ARTICLES_PER_CALL rather than one call for the
    whole cycle -- see that constant for the production measurement that
    forced it. Chunking also bounds the blast radius: a failed call now
    costs one chunk's categories instead of the entire cycle's.

    Fails open on any error (model call failure, malformed response) by
    omitting that chunk's indexes -- callers treat a missing index as "no
    categories assigned" and still cache the article uncategorized, same
    reasoning as guardrails.classify_message: a classification hiccup
    shouldn't block caching the article, it should just make it harder to
    find via category filtering until a later cycle reclassifies it.

    Unlike before, a failure is now *printed*. The silent version hid a
    real outage: every batch above ~110 articles failed for three days
    straight, leaving 92.8% of the cache uncategorized and therefore
    invisible to push candidate selection, with nothing in the logs to
    show for it."""
    if not articles:
        return {}
    categories: dict[int, list[str]] = {}
    failed_chunks = 0
    total_chunks = 0
    for start in range(0, len(articles), MAX_ARTICLES_PER_CALL):
        chunk = articles[start:start + MAX_ARTICLES_PER_CALL]
        total_chunks += 1
        result = _classify_one_batch(model, chunk)
        if not result:
            failed_chunks += 1
        for local_index, cats in result.items():
            # translate the chunk-local index the model returned back to an
            # index into the caller's own list
            if 0 <= local_index < len(chunk):
                categories[start + local_index] = cats
    if failed_chunks:
        print(
            f"[news_classify] {failed_chunks} of {total_chunks} chunk(s) failed -- "
            f"{len(articles) - len(categories)} article(s) left uncategorized"
        )
    return categories
