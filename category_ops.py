"""
Data Access Layer for the article-category taxonomy -- see
docs/plans/taxonomy-and-admin-plan.md. bot.py/admin_bot.py/news_ingest.py/
news_push.py call these; none of them touch SQL.
"""

from datetime import datetime, timedelta, timezone

from app_settings import get_settings
from storage import get_storage

CATEGORY_SIGHTING_RETENTION_DAYS = get_settings().resolved("categories.sighting_retention_days", default=30)
# A placeholder, not a measured value -- see git history (pre-refactor
# users_db.py) for the reasoning; revisit once the sightings table has a
# distribution worth reading.
CATEGORY_PROPOSAL_THRESHOLD = get_settings().resolved("categories.proposal_threshold", default=5)

# Telegram caps callback_data at 64 bytes and admin_bot packs
# "cat:into:{name}:{target}" into it, so a proposed name must be short and
# must not contain the delimiter.
MAX_CATEGORY_NAME_LENGTH = 32

# Written by the code, never chosen by the model -- marks "the classifier
# looked and nothing applied", distinguishable from "the classifier never
# ran". status='system' keeps it out of get_active_categories() and
# therefore out of the prompt.
UNCLASSIFIABLE = "Other"

# The taxonomy the classifier started with. Seeded once on an empty table;
# after that the table is authoritative and this list is history, not
# configuration. Descriptions are load-bearing -- they go into the
# classifier prompt verbatim.
SEED_CATEGORIES: list[tuple[str, str]] = [
    ("AI", "AI models, research, agents, LLMs"),
    ("Software", "software products, dev tools, programming"),
    ("Hardware", "chips, semiconductors, devices, infrastructure hardware"),
    ("IT", "enterprise IT, cloud, infrastructure, enterprise software"),
    ("Startups", "funding rounds, new companies, venture capital"),
    ("Finance", "business/financial industry news, economics, corporate deals"),
    ("Stock", "stock price moves, market reactions specifically -- distinct "
              "from Finance, which covers business news generally"),
    # Policy was retired 2026-08-20 and split into the four below -- see
    # bootstrap()'s migrate_split_policy call. Kept in this list so seeding
    # an old database still creates the row the migration then retires.
    ("Policy", "regulation, government, legal, antitrust"),
    ("Security", "cybersecurity, breaches, vulnerabilities"),
    ("Research", "academic papers, science"),
    ("Consumer", "consumer gadgets, reviews, product launches for individual users"),
    ("Robotics", "robotics specifically"),
    ("Crypto", "cryptocurrency/blockchain"),
    ("Regulation", "rules and compliance imposed on an industry -- export "
                   "controls, safety standards, licensing"),
    ("Government", "government action and process -- agencies, appointments, "
                   "budgets, procurement, public programmes"),
    ("Legal", "courts, lawsuits, rulings, liability, intellectual property"),
    ("Antitrust", "competition law specifically -- monopoly, market power, "
                  "mergers under review, breakup remedies"),
]


def bootstrap() -> None:
    """Seeds the taxonomy and applies the one-time Policy-split migration.
    Called once at process startup, right after storage.init_db() --
    separate from it because storage.init_db() is schema-only and doesn't
    know what a category IS; this is where the actual taxonomy content
    lives, per docs/plans/data-layer-plan.md's layering."""
    now = datetime.now(timezone.utc).isoformat()
    get_storage().seed_categories(SEED_CATEGORIES, now, UNCLASSIFIABLE, "the classifier found nothing applicable")
    get_storage().migrate_split_policy(now)


def get_active_categories() -> list[tuple[str, str]]:
    """The (name, description) pairs the classifier should offer, in
    curated sort_order (not alphabetical -- see git history for why that
    matters for Stock/Finance's cross-referencing descriptions)."""
    rows = get_storage().get_active_categories()
    return [(name, description or "") for name, description in rows]


def resolve_category_name(name: str) -> str | None:
    """Follows `merged_into` so a name stored on an article cached before
    a merge still resolves to the surviving category. None for a name not
    in the taxonomy, or for a merge cycle (impossible unless the data is
    corrupt -- logged by the storage layer if it happens)."""
    return get_storage().resolve_category_name(name)


def normalize_category_name(name: str) -> str:
    """Makes a model-proposed label safe to round-trip through a Telegram
    callback -- normalized at the point it's recorded, not parsed
    defensively later."""
    cleaned = " ".join(name.replace(":", " ").split())
    return cleaned[:MAX_CATEGORY_NAME_LENGTH].strip()


def record_category_sighting(name: str, seen_at: datetime, link: str | None = None,
                             title: str | None = None) -> None:
    """Logs that the classifier reached for `name`, which isn't active,
    and creates the proposed row if this is the first time. A sighting is
    evidence, not a decision -- an already-decided row (rejected, retired,
    merged) stays that way."""
    name = normalize_category_name(name)
    if not name:
        return
    get_storage().record_category_sighting(name, seen_at.isoformat(), link, title)


def count_recent_sightings(now: datetime, days: int = CATEGORY_SIGHTING_RETENTION_DAYS) -> dict[str, int]:
    """{proposed category name: sightings inside the window}. Only
    'proposed' rows count -- a rejected category keeps accumulating
    sightings but must never trigger the admin prompt again."""
    cutoff = (now - timedelta(days=days)).isoformat()
    rows = get_storage().count_recent_sightings(cutoff)
    return {name: count for name, count in rows}


def prune_category_sightings(now: datetime, days: int = CATEGORY_SIGHTING_RETENTION_DAYS) -> int:
    cutoff = (now - timedelta(days=days)).isoformat()
    return get_storage().prune_category_sightings(cutoff)


def categories_ready_for_review(now: datetime, threshold: int = CATEGORY_PROPOSAL_THRESHOLD,
                               days: int = CATEGORY_SIGHTING_RETENTION_DAYS) -> list[tuple[str, int]]:
    """[(name, hits)] for proposals crossed the threshold and not yet
    raised with an admin -- alerted_at is a one-time push, not a
    reminder loop."""
    cutoff = (now - timedelta(days=days)).isoformat()
    rows = get_storage().categories_ready_for_review(cutoff, threshold)
    return [(name, hits) for name, hits in rows]


def category_examples(name: str, limit: int = 3) -> list[tuple[str, str]]:
    rows = get_storage().category_examples(name, limit)
    return [(title, link or "") for title, link in rows]


def mark_category_alerted(name: str, now: datetime, description: str | None = None) -> None:
    get_storage().mark_category_alerted(name, now.isoformat(), description)


def activate_category(name: str, by: str, now: datetime, description: str | None = None) -> bool:
    """Promotes a proposal to a real category. Also clears
    interest_categories (pre-A5 shape -- see docs/plans/taxonomy-and-admin-plan.md);
    the next push cycle re-resolves them."""
    return get_storage().activate_category(name, by, now.isoformat(), description)


def reject_category(name: str, by: str, now: datetime) -> bool:
    return get_storage().reject_category(name, by, now.isoformat())


def merge_category(name: str, into: str, by: str, now: datetime) -> bool:
    """Points `name` at `into` and rewrites any interest that referenced
    it. Refuses to merge into anything that isn't active."""
    return get_storage().merge_category(name, into, by, now.isoformat())
