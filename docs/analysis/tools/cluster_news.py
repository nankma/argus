"""
Step 2: cluster a cache snapshot by story, with TF-IDF cosine and BM25, and
report how many clusters exist and how big they are.

Answers the question "does corroboration-based importance scoring have
enough signal to work with?" -- see ../news-ranking-plan.md's Option B.

BM25 is implemented inline rather than pulling in rank_bm25: it's a short
formula, and this project's environment already has scikit-learn but not
that package. Clustering is connected components (single-linkage) over a
similarity threshold, which is the standard approach in the near-duplicate
detection literature -- if A~B and B~C then A, B and C are one story.

    python analysis/tools/cluster_news.py
    python analysis/tools/cluster_news.py --detail          # show cluster contents
    python analysis/tools/cluster_news.py --json out.json   # machine-readable

Reads analysis/data/cache-snapshot.tsv by default (see fetch_cache_snapshot.py).
Makes no network calls and no LLM calls -- pure local computation, free to
re-run.
"""

import argparse
import json
import sys
from collections import Counter, defaultdict

import numpy as np
from sklearn.feature_extraction.text import CountVectorizer, TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# Real article titles contain emoji; Windows' default cp1252 console can't
# encode them. Same fix tools/measure_guardrails.py uses for its dataset.
sys.stdout.reconfigure(encoding="utf-8")

TFIDF_THRESHOLDS = [0.30, 0.40, 0.50, 0.60, 0.70]
BM25_THRESHOLDS = [0.10, 0.15, 0.20, 0.30, 0.40]


def load(path):
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 3 and p[2].strip():
                summary = p[3].strip() if len(p) > 3 else ""
                rows.append({
                    "src": p[0].strip(),
                    "dt": p[1].strip(),
                    "title": p[2].strip(),
                    "summary": "" if summary in ("null", "None") else summary,
                })
    return rows


def connected_components(sim, threshold):
    """Single-linkage clustering via union-find."""
    n = len(sim)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j in np.argwhere(sim >= threshold):
        if i < j:
            ri, rj = find(int(i)), find(int(j))
            if ri != rj:
                parent[rj] = ri

    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return list(groups.values())


def bm25_similarity(docs, k1=1.5, b=0.75):
    """Document-document BM25: each document scored as a query against every
    other, symmetrized, then normalized to [0,1] so its thresholds are at
    least on a comparable *scale* to cosine (they are NOT interchangeable
    values -- see the report)."""
    counts = CountVectorizer(stop_words="english", min_df=1).fit_transform(docs)
    X = np.asarray(counts.todense(), dtype=np.float32)
    n_docs = X.shape[0]
    doc_len = X.sum(axis=1)
    avg_len = doc_len.mean()
    doc_freq = (X > 0).sum(axis=0)
    idf = np.log((n_docs - doc_freq + 0.5) / (doc_freq + 0.5) + 1.0)

    denom = X + k1 * (1 - b + b * (doc_len / avg_len))[:, None]
    weighted = ((X * (k1 + 1)) / np.maximum(denom, 1e-9)) * idf[None, :]
    present = (X > 0).astype(np.float32)

    sim = present @ weighted.T
    sim = (sim + sim.T) / 2.0
    np.fill_diagonal(sim, 0.0)
    return sim / sim.max() if sim.max() > 0 else sim


def summarize(rows, sim, thresholds, label):
    results = []
    print(f"\n=== {label} ===")
    print(f"  {'thresh':<8}{'clusters':>9}{'singleton':>11}{'multi':>7}"
          f"{'cross-src':>11}{'largest':>9}")
    for th in thresholds:
        clusters = connected_components(sim, th)
        sizes = Counter(len(c) for c in clusters)
        multi = [c for c in clusters if len(c) > 1]
        cross = sum(1 for c in multi if len({rows[i]["src"] for i in c}) > 1)
        largest = max(sizes) if sizes else 0
        results.append({
            "method": label, "threshold": th,
            "clusters_total": len(clusters),
            "singletons": sizes.get(1, 0),
            "multi_clusters": len(multi),
            "cross_source_clusters": cross,
            "largest_cluster": largest,
            "size_histogram": {str(k): v for k, v in sorted(sizes.items())},
            "articles_in_multi": sum(len(c) for c in multi),
        })
        print(f"  {th:<8.2f}{len(clusters):>9}{sizes.get(1,0):>11}{len(multi):>7}"
              f"{cross:>11}{largest:>9}")
    return results


def show_detail(rows, sim, threshold):
    print(f"\n=== multi-article clusters at threshold {threshold} ===")
    multi = sorted((c for c in connected_components(sim, threshold) if len(c) > 1),
                   key=len, reverse=True)
    print(f"{len(multi)} clusters\n")
    for c in multi:
        srcs = sorted({rows[i]["src"] for i in c})
        kind = "CROSS-SOURCE" if len(srcs) > 1 else "same-source "
        print(f"  [{kind}] n={len(c)}  {srcs}")
        for i in c[:4]:
            print(f"      - [{rows[i]['src']}] {rows[i]['title'][:70]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default="analysis/data/cache-snapshot.tsv")
    ap.add_argument("--detail", action="store_true",
                    help="print the contents of every multi-article cluster")
    ap.add_argument("--detail-threshold", type=float, default=0.40)
    ap.add_argument("--json", help="also write results to this JSON file")
    args = ap.parse_args()

    rows = load(args.input)
    if not rows:
        print(f"no articles in {args.input}", file=sys.stderr)
        sys.exit(1)
    print(f"articles: {len(rows)}   (with summary text: "
          f"{sum(1 for r in rows if r['summary'])})")

    docs = [(r["title"] + " " + r["summary"]).strip() for r in rows]

    tfidf = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), min_df=1)
    X = tfidf.fit_transform(docs)
    print(f"tf-idf vocabulary: {len(tfidf.vocabulary_)}")
    sim_tfidf = cosine_similarity(X)
    np.fill_diagonal(sim_tfidf, 0.0)
    sim_bm25 = bm25_similarity(docs)

    res_tfidf = summarize(rows, sim_tfidf, TFIDF_THRESHOLDS, "TF-IDF cosine")
    res_bm25 = summarize(rows, sim_bm25, BM25_THRESHOLDS, "BM25 (normalized)")

    # Exact duplicates are invisible to news_cache's link-hash dedup when the
    # same story arrives under two different URLs (e.g. an aggregator link).
    dupes = {k: v for k, v in Counter((r["src"], r["title"]) for r in rows).items() if v > 1}
    print(f"\nexact-duplicate entries (same source + same title): "
          f"{len(dupes)} groups, {sum(v - 1 for v in dupes.values())} redundant copies")

    if args.detail:
        show_detail(rows, sim_tfidf, args.detail_threshold)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"n_articles": len(rows), "tfidf": res_tfidf, "bm25": res_bm25,
                       "exact_duplicate_groups": len(dupes)}, f, indent=2)
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
