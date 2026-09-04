"""
What would the taxonomy be missing if general US news were added?

A dry run, not a deployment. Fetches the general-news feeds under
consideration (NPR, NBC, CBS, CNN, PBS, ABC, Politico, The Hill),
classifies them against the CURRENT active taxonomy, and reports what the
classifier reaches for that doesn't exist.

The point is to answer "which categories would we need" before adding the
sources, rather than after. The same question was answered accidentally
for the tech feeds: adding Ars Technica, TechCrunch and CNBC surfaced five
gaps (Healthcare, Legal, Media, Open Source, Retail) in a single ingestion
cycle. That was useful but unplanned, and it happened in production. This
does it deliberately and offline.

Two numbers matter:

  COVERAGE -- what fraction of general-news articles the active taxonomy
  can classify at all. Deliberately not stated as a fixed number of
  categories: the taxonomy is data now and changes without a redeploy, and
  the first version of this docstring said "13" in the very commit that
  made it 16. The script prints the live count when it runs.

  A low number is expected: the taxonomy was derived from a corpus that is
  47% AI-only feeds. How low tells you whether general news is a few
  missing categories away from fitting, or a different product.

  Coverage alone is misleading, which is why the report also breaks down
  WHICH categories get used. The first run of this scored 70% coverage
  with 65% of all assignments landing on a single bucket -- a number that
  looks like success and means the opposite.

  PROPOSED LABELS -- what the classifier asks for. These are candidates for
  the taxonomy, and unlike a human brainstorm they are grounded in articles
  that actually appeared.

Runs through the Claude CLI rather than DEEPSEEK_API_KEY -- see
tools/claude_cli_model.py, including its note on what does and doesn't
transfer between models.

    python docs/analysis/tools/probe_general_news.py
    python docs/analysis/tools/probe_general_news.py --limit 15 --model haiku
"""

import argparse
import os
import sys
from collections import Counter

# repo root is three levels up from docs/analysis/tools/
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")))

import news_classify
import news_sources
import category_ops
from tools.claude_cli_model import ClaudeCLIModel

# The bundle under consideration. Deliberately the general-news half only:
# Ars Technica, TechCrunch and CNBC are already registered, and MarketWatch
# was already there.
FEEDS = [
    ("NPR", "https://feeds.npr.org/1001/rss.xml"),
    ("NBC News", "http://feeds.nbcnews.com/nbcnews/public/news"),
    ("CBS News", "https://www.cbsnews.com/latest/rss/main"),
    ("CNN", "http://rss.cnn.com/rss/cnn_topstories.rss"),
    ("PBS NewsHour", "https://www.pbs.org/newshour/feeds/rss/headlines"),
    ("ABC News", "https://feeds.abcnews.com/abcnews/topstories"),
    ("Politico", "https://rss.politico.com/politics-news.xml"),
    ("The Hill", "https://thehill.com/news/feed/"),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=12, help="articles per feed")
    ap.add_argument("--model", default="sonnet", help="claude CLI model alias")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    articles, by_source = [], {}
    print("fetching:")
    for name, url in FEEDS:
        try:
            fetched = news_sources._fetch_rss(url, name, args.limit)
        except Exception as exc:
            print(f"  {name:14} FAILED {type(exc).__name__}: {str(exc)[:60]}")
            continue
        print(f"  {name:14} {len(fetched):3} articles")
        by_source[name] = (len(articles), len(articles) + len(fetched))
        articles.extend(fetched)

    if not articles:
        sys.exit("no articles fetched")

    taxonomy = news_classify.Taxonomy.from_rows(category_ops.get_active_categories())
    print(f"\nclassifying {len(articles)} articles against the current "
          f"{len(taxonomy.names)} active categories")
    print(f"  ({', '.join(taxonomy.names)})\n")

    proposed = Counter()
    examples: dict[str, str] = {}

    def note(label, article):
        proposed[label] += 1
        examples.setdefault(label, article.get("title") or "")

    model = ClaudeCLIModel(model=args.model)
    result = news_classify.classify_articles(model, articles, taxonomy,
                                             on_unknown_label=note)

    classified = sum(1 for i in range(len(articles)) if result.get(i))
    empty = sum(1 for i in range(len(articles)) if i in result and not result[i])
    missing = len(articles) - len(result)

    print("=" * 72)
    print("COVERAGE -- can the current taxonomy classify general news at all?")
    print("=" * 72)
    print(f"  got at least one category : {classified:4}/{len(articles)} "
          f"({classified/len(articles)*100:.0f}%)")
    print(f"  nothing applied ('Other') : {empty:4}/{len(articles)} "
          f"({empty/len(articles)*100:.0f}%)")
    if missing:
        print(f"  chunk failed, unknown     : {missing:4}/{len(articles)}")

    print("\nper source:")
    for name, (lo, hi) in by_source.items():
        n = hi - lo
        hit = sum(1 for i in range(lo, hi) if result.get(i))
        print(f"  {name:14} {hit:3}/{n:3} classified ({hit/n*100:3.0f}%)")

    print("\nwhich existing categories actually get used:")
    used = Counter(c for cats in result.values() for c in cats)
    for name, count in used.most_common():
        print(f"  {name:12} {count}")
    unused = [n for n in taxonomy.names if n not in used]
    if unused:
        print(f"  (never used: {', '.join(unused)})")

    # A high coverage number means nothing if one broad category is
    # absorbing everything, so show what the dominant one actually caught.
    # "70% classified" and "70% dumped into Policy" are very different
    # findings and the headline number cannot tell them apart.
    if used:
        top = used.most_common(1)[0][0]
        only_top = sum(1 for c in result.values() if c == [top])
        print(f"\n  articles whose ONLY category is '{top}': {only_top}"
              f" of {len(articles)}")
        print(f"  a sample of what '{top}' caught:")
        shown = 0
        for i, cats in sorted(result.items()):
            if top in cats and i < len(articles) and shown < 6:
                title = (articles[i].get("title") or "")[:54]
                print(f"    {str(cats):32} {title}")
                shown += 1

    print("\n" + "=" * 72)
    print("PROPOSED -- what the classifier reached for that doesn't exist")
    print("=" * 72)
    if not proposed:
        print("  nothing -- the current taxonomy covered everything it saw")
    for label, count in proposed.most_common():
        print(f"  {label:22} x{count}")
        print(f"      e.g. {examples[label][:62]}")

    print(f"\n{model.calls} CLI call(s), ${model.total_cost_usd:.4f}")


if __name__ == "__main__":
    main()
