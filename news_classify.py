"""
Batched article classification for the local news cache -- see
docs/local-news-cache-plan.md's "Classification mechanism" section.

One structured-output LLM call per ingestion cycle classifies every
newly-fetched article in that cycle at once, rather than one call per
article -- at the volumes this project's source registry produces, a
per-article call would mean hundreds of LLM calls/day for a task a single
batched call handles just as well. Same DeepSeek instance already used
elsewhere (see docs/local-news-cache-plan.md's resolved "classification
model" question) -- docs/model-portability-plan.md's cheaper per-stage
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


class ArticleCategories(BaseModel):
    index: int
    categories: list[Category]


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
    cycle -- interest text is stable vocabulary, not fresh content."""
    if not interests:
        return {}
    articles = [{"title": interest, "summary": None} for interest in interests]
    result = classify_articles(model, articles)
    return {interest: result.get(i, []) for i, interest in enumerate(interests)}


def classify_articles(model, articles: list[dict]) -> dict[int, list[str]]:
    """Returns {index: categories} for every article in `articles` that
    the model actually returned an entry for. Fails open on any error
    (model call failure, malformed response) by returning {} -- callers
    treat a missing index as "no categories assigned" and still cache the
    article uncategorized, same fail-open reasoning as
    guardrails.classify_message: a classification hiccup shouldn't block
    caching the article, it should just make it harder to find via
    category filtering until the next cycle re-fetches and reclassifies
    it (or a human notices and investigates)."""
    if not articles:
        return {}
    try:
        structured = model.with_structured_output(ClassificationBatch)
        result = structured.invoke(
            [
                {"role": "system", "content": _CLASSIFY_PROMPT},
                {"role": "user", "content": _format_batch(articles)},
            ]
        )
    except Exception:
        return {}
    return {item.index: list(item.categories) for item in result.items}
