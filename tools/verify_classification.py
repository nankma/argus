"""
Verifies news_classify.classify_articles against a real model.

Exists because of a real production outage: classification was a single
LLM call per ingestion cycle, every batch above ~110 articles silently
failed all-or-nothing, and 92.8% of the cache sat uncategorized for three
days with nothing in the logs. The fix chunks each cycle into calls of
MAX_ARTICLES_PER_CALL, which introduced the one thing worth verifying --
translating each chunk-local index the model returns back into an index
into the caller's list. Get that wrong and every article past the first
chunk gets somebody else's categories, which no unit test with a fake
model would catch if the fake returns indexes the same way the real model
does.

So this checks two things a fake can't:

  1. A real model, given the real prompt, returns indexes we can actually
     map back -- across several chunks.
  2. Canary articles planted at known positions in later chunks come back
     with THEIR OWN categories, not the ones belonging to the article at
     the same offset within the chunk. This is the off-by-a-chunk bug, and
     it's detectable without a human reading the output.

Runs through the Claude CLI rather than DEEPSEEK_API_KEY -- see
tools/claude_cli_model.py, including its note on what that does and
doesn't transfer to DeepSeek (batch-size limits do NOT transfer).

    python tools/verify_classification.py --count 120
    python tools/verify_classification.py --count 120 --model haiku
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news_classify
from tools.claude_cli_model import ClaudeCLIModel

SNAPSHOT = os.path.join("docs", "analysis", "data", "cache-snapshot.tsv")

# Planted at known indexes in later chunks. Each is unambiguous enough that
# the expected category isn't a judgment call -- if the mapping is off by a
# chunk, these land on the wrong index and the check fails loudly.
CANARIES = [
    ("Bitcoin ETF inflows hit record as crypto market rallies past $80,000",
     "Crypto"),
    ("Boston Dynamics unveils new bipedal warehouse robot with improved gait",
     "Robotics"),
    ("Critical zero-day in OpenSSL lets attackers bypass certificate validation",
     "Security"),
]


def load_articles(count: int) -> list[dict]:
    if not os.path.exists(SNAPSHOT):
        sys.exit(f"no snapshot at {SNAPSHOT} -- run tools/fetch_cache_snapshot.py first")
    articles = []
    with open(SNAPSHOT, encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 6 and parts[4].strip():
                summary = parts[5].strip()
                articles.append({
                    "title": parts[4],
                    "summary": None if summary in ("", "null", "None") else summary,
                })
            if len(articles) >= count:
                break
    return articles


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--count", type=int, default=120,
                    help="articles to classify (default 120 = 3 chunks of 50)")
    ap.add_argument("--model", default="sonnet", help="claude CLI model alias")
    ap.add_argument("--budget", type=float, default=5.0,
                    help="per-call spend cap in USD")
    args = ap.parse_args()

    articles = load_articles(args.count)
    if len(articles) < args.count:
        print(f"note: snapshot only has {len(articles)} usable articles")

    # Plant canaries deep enough that at least one lands in each later chunk.
    step = news_classify.MAX_ARTICLES_PER_CALL
    canary_at = {}
    for n, (title, expected) in enumerate(CANARIES, start=1):
        pos = min(n * step + 7, len(articles) - 1)
        if pos < len(articles):
            articles[pos] = {"title": title, "summary": None}
            canary_at[pos] = expected

    n_chunks = -(-len(articles) // step)
    print(f"classifying {len(articles)} articles in {n_chunks} chunk(s) of {step} "
          f"via claude CLI ({args.model})")
    print(f"canaries planted at indexes: {sorted(canary_at)}\n")

    model = ClaudeCLIModel(model=args.model, max_budget_usd=args.budget)
    result = news_classify.classify_articles(model, articles)

    # --- coverage -------------------------------------------------------
    missing = [i for i in range(len(articles)) if i not in result]
    empty = [i for i, c in result.items() if not c]
    print(f"returned categories for {len(result)}/{len(articles)} articles")
    print(f"  missing entirely : {len(missing)}")
    print(f"  present but empty: {len(empty)}")
    if missing:
        print(f"  first missing indexes: {missing[:12]}")

    # --- per-chunk coverage, which is where the old bug showed ----------
    print("\nper-chunk coverage (the outage was all-or-nothing per call):")
    for c in range(n_chunks):
        lo, hi = c * step, min((c + 1) * step, len(articles))
        got = sum(1 for i in range(lo, hi) if i in result)
        flag = "  <-- EMPTY CHUNK" if got == 0 else ""
        print(f"  chunk {c + 1} (idx {lo:3}-{hi - 1:3}): {got:3}/{hi - lo}{flag}")

    # --- index mapping --------------------------------------------------
    print("\ncanary check (catches an off-by-a-chunk index mapping):")
    failures = 0
    for pos in sorted(canary_at):
        expected = canary_at[pos]
        got = result.get(pos)
        if got is None:
            verdict, failed = "NOT CLASSIFIED", True
        elif expected in got:
            verdict, failed = "ok", False
        else:
            verdict, failed = f"MISMATCH (wanted {expected})", True
        failures += failed
        print(f"  idx {pos:3} -> {got}  [{verdict}]")
        print(f"          {articles[pos]['title'][:66]}")

    # --- eyeball a couple of ordinary ones ------------------------------
    print("\nspot-check, ordinary articles from the last chunk:")
    tail = [i for i in sorted(result) if i >= (n_chunks - 1) * step][:3]
    for i in tail:
        print(f"  idx {i:3} {result[i]}")
        print(f"          {articles[i]['title'][:66]}")

    print(f"\n{model.calls} CLI call(s), ${model.total_cost_usd:.4f}")

    ok = not failures and not missing
    print("\nRESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
