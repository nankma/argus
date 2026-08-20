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

The taxonomy is no longer defined here. It lives in the `categories`
table (see users_db.SEED_CATEGORIES for what a fresh database starts
with, and docs/plans/taxonomy-and-admin-plan.md for the design), and is
passed in as a Taxonomy so this module keeps no database dependency. It
is explicitly revisable: the whole point of moving it was that changing
it should be a data change, not a redeploy.
"""

from dataclasses import dataclass
from functools import cached_property

from pydantic import BaseModel


@dataclass(frozen=True)
class Taxonomy:
    """The categories the classifier may use, for one run.

    Passed in rather than read from the database here, so this module has
    no database dependency and its tests can hand it a two-category
    taxonomy instead of standing up SQLite -- the same reasoning as
    build_agent taking its model as a parameter (see CLAUDE.md). Callers
    build it from users_db.get_active_categories().

    Frozen because the prompt text is derived from it: a taxonomy that
    changed underneath a batch would mean the prompt and the validation set
    disagreed about what a valid answer is.
    """

    entries: tuple[tuple[str, str], ...]   # (name, description), prompt order

    @classmethod
    def from_rows(cls, rows) -> "Taxonomy":
        return cls(entries=tuple((name, description) for name, description in rows))

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.entries)

    def __contains__(self, name: object) -> bool:
        return name in self._name_set

    @cached_property
    def _name_set(self) -> frozenset[str]:
        """Built once. `entries` is frozen, so rebuilding a set per lookup
        would be pure waste -- negligible at a chunk of 50 articles, but
        there is no reason to pay it."""
        return frozenset(self.names)

    def prompt_fragment(self) -> str:
        return "\n".join(f"- {name}: {description}" for name, description in self.entries)


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
    # request. The prompt still lists the active categories, so the model
    # is still aimed at them; this only changes what happens when it misses.
    categories: list[str]


class ClassificationBatch(BaseModel):
    items: list[ArticleCategories]


def _classify_prompt(taxonomy: Taxonomy) -> str:
    """Built per call from the taxonomy rather than being a module constant,
    so activating a category takes effect without a redeploy. The category
    list and the set used to validate the reply come from the same object,
    which is the point: a prompt offering categories the validator would
    then reject is a silent classification failure."""
    return (
        "You are a strict classifier, not an assistant. Below is a numbered "
        "list of news article titles (and summaries, where available). For "
        "EACH article, assign every category from this list that plausibly "
        "applies -- most articles need more than one:\n\n"
        f"{taxonomy.prompt_fragment()}\n\n"
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


def classify_interests(model, interests: list[str], taxonomy: Taxonomy) -> dict[str, list[str]]:
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
    up cached as [] even though AI is itself one of the categories, and an
    empty mapping matches every article, so those subscribers were being
    sent completely unfiltered news."""
    if not interests:
        return {}
    articles = [{"title": interest, "summary": None} for interest in interests]
    result = classify_articles(model, articles, taxonomy)
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


def _classify_one_batch(model, articles: list[dict], taxonomy: Taxonomy,
                        on_unknown_label=None) -> dict[int, list[str]]:
    """One structured-output call. Returns {} on any failure -- see
    classify_articles for the fail-open reasoning."""
    try:
        structured = model.with_structured_output(ClassificationBatch)
        result = structured.invoke(
            [
                {"role": "system", "content": _classify_prompt(taxonomy)},
                {"role": "user", "content": _format_batch(articles)},
            ]
        )
    except Exception as exc:
        print(f"[news_classify] batch of {len(articles)} failed: {exc!r}")
        return {}
    if result is None:
        print(f"[news_classify] batch of {len(articles)} returned no result")
        return {}
    return _valid_categories(result, taxonomy, articles, on_unknown_label)


def _valid_categories(result: ClassificationBatch, taxonomy: Taxonomy,
                      articles: list[dict], on_unknown_label=None
                      ) -> dict[int, list[str]]:
    """Drops labels the model invented that aren't in the taxonomy, keeping
    everything else. An article left with no valid labels keeps an empty
    list -- same as the model saying nothing applies.

    Rejected labels are reported rather than silently discarded, because
    they are useful signal in their own right: a label the model keeps
    reaching for is a gap in the taxonomy. "Education" is what surfaced
    this. See docs/plans/taxonomy-and-admin-plan.md, where these labels
    become the evidence an admin decides on.

    `on_unknown_label(label, article)` is how they leave this module. A
    callback rather than a database write here, and rather than a richer
    return type, for two reasons: it keeps this module free of a database
    dependency (the same reason Taxonomy is a parameter), and it carries
    the example article along, which the admin prompt needs -- a bare label
    with no example is much harder to make a decision about.

    The first article seen for a label is passed, not all of them: this
    runs per 50-article chunk many times a day, and the admin prompt shows
    two or three examples, so accumulating every one would grow a table
    nobody reads to the bottom of."""
    categories: dict[int, list[str]] = {}
    rejected: set[str] = set()
    for item in result.items:
        kept = []
        for name in item.categories:
            if name in taxonomy:
                kept.append(name)
            elif name not in rejected:
                rejected.add(name)
                if on_unknown_label is not None:
                    # index is the model's, so it can be out of range if the
                    # reply is malformed -- don't let that lose the batch
                    article = (articles[item.index]
                               if 0 <= item.index < len(articles) else {})
                    on_unknown_label(name, article)
        categories[item.index] = kept
    if rejected:
        print(f"[news_classify] dropped {len(rejected)} label(s) outside the "
              f"taxonomy: {', '.join(sorted(rejected))}")
    return categories


def classify_articles(model, articles: list[dict], taxonomy: Taxonomy,
                      on_unknown_label=None) -> dict[int, list[str]]:
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
        result = _classify_one_batch(model, chunk, taxonomy, on_unknown_label)
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
