"""
Two push-selection signals, measured on the filtered candidate set.

Not classification. The taxonomy is the LLM's job and stays that way; this
is about WHICH of the articles that survive filtering are worth sending:

  HOT TOPIC -- inside one category, a cluster holding several articles
  means several outlets covered the same thing at once. Cluster size alone
  is not enough, because one outlet publishing a series looks identical to
  a real story; the usable signal is size AND distinct sources.

  NOVELTY -- one or two articles far from everything already selected, so
  a digest isn't five versions of the same story. Only meaningful if
  "far" is discriminating: under TF-IDF the median max-similarity across a
  candidate pool was 0.0000, hundreds tied at zero, making "farthest" a
  coin flip. Dense vectors are what make it a real ordering.

The earlier run of this question used a cache that was 93% unclassified
(a three-day outage nobody noticed), so the filtered sets were tiny and
the answer was "not enough data". Classification has been fixed since.

    python docs/analysis/tools/measure_hotspots.py
    python docs/analysis/tools/measure_hotspots.py --category AI --threshold 0.7
"""

import argparse
import os
import sys
from collections import Counter

import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")))

SNAPSHOT = os.path.join("docs", "analysis", "data", "cache-snapshot-0821.tsv")


def load(path):
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 6 and p[4].strip():
                s = "" if p[5].strip() in ("null", "None") else p[5]
                rows.append({
                    "src": p[0], "published": p[1], "fetched": p[2],
                    "cats": [c.strip() for c in p[3].split(",") if c.strip()],
                    "title": p[4], "text": (p[4] + " " + s).strip(),
                })
    return rows


def single_linkage(V, threshold):
    """Connected components above `threshold`. Single-linkage on purpose:
    a story is a chain of near-duplicates (A rewrites B, C rewrites A), not
    a tight ball, so requiring every pair to be similar would split real
    coverage into fragments."""
    n = len(V)
    if n < 2:
        return []
    S = cosine_similarity(V)
    np.fill_diagonal(S, 0.0)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for a, b in zip(*np.where(np.triu(S >= threshold, 1))):
        ra, rb = find(int(a)), find(int(b))
        if ra != rb:
            parent[ra] = rb
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return sorted((g for g in groups.values() if len(g) > 1), key=len, reverse=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", default=SNAPSHOT)
    ap.add_argument("--category", default=None, help="only this one")
    ap.add_argument("--threshold", type=float, default=0.75,
                    help="cosine above which two articles are the same story")
    ap.add_argument("--select", type=int, default=5, help="digest size for the novelty test")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    if not os.path.exists(args.snapshot):
        sys.exit(f"no snapshot at {args.snapshot} -- run tools/fetch_cache_snapshot.py")

    rows = load(args.snapshot)
    classified = [r for r in rows if r["cats"] and r["cats"] != ["Other"]]
    print(f"{len(rows)} articles, {len(classified)} with real categories "
          f"({len(classified)/len(rows)*100:.0f}%)")

    from model2vec import StaticModel
    model = StaticModel.from_pretrained("minishlab/potion-base-8M")
    V = np.asarray(model.encode([r["text"] for r in classified]), dtype=np.float32)
    V = V - V.mean(axis=0)                      # centering; see build_taxonomy
    V = V / (np.linalg.norm(V, axis=1, keepdims=True) + 1e-9)

    cats = Counter(c for r in classified for c in r["cats"])
    targets = [args.category] if args.category else [c for c, _ in cats.most_common(8)]

    print(f"\n{'=' * 74}\nHOT TOPICS inside each category (cosine >= {args.threshold})\n{'=' * 74}")
    print(f"{'category':16} {'n':>5} {'groups':>7} {'largest':>8}  biggest story")
    for cat in targets:
        idx = [i for i, r in enumerate(classified) if cat in r["cats"]]
        if len(idx) < 5:
            print(f"{cat:16} {len(idx):5}  (too few to cluster)")
            continue
        groups = single_linkage(V[idx], args.threshold)
        # size alone rewards one outlet publishing a series; distinct
        # sources is what separates that from real corroboration
        best = max(groups, key=lambda g: (len({classified[idx[i]]['src'] for i in g}), len(g)),
                   default=None)
        if best is None:
            print(f"{cat:16} {len(idx):5} {0:7} {0:8}")
            continue
        srcs = {classified[idx[i]]["src"] for i in best}
        print(f"{cat:16} {len(idx):5} {len(groups):7} {len(best):8}  "
              f"{len(best)} articles / {len(srcs)} sources")
        for i in best[:2]:
            r = classified[idx[i]]
            print(f"{'':40}   [{r['src'][:14]:14}] {r['title'][:46]}")

    print(f"\n{'=' * 74}\nNOVELTY -- is 'far from everything selected' discriminating?\n{'=' * 74}")
    for cat in targets[:4]:
        idx = [i for i, r in enumerate(classified) if cat in r["cats"]]
        if len(idx) < args.select + 5:
            continue
        # pretend the top N by recency were chosen, then look at the rest
        order = sorted(idx, key=lambda i: classified[i]["fetched"], reverse=True)
        sel, pool = order[:args.select], order[args.select:]
        maxsim = cosine_similarity(V[pool], V[sel]).max(axis=1)
        far = pool[int(np.argmin(maxsim))]
        print(f"\n{cat}: pool {len(pool)}, median max-sim to the selection "
              f"{np.median(maxsim):.3f}, exact zeros {int((maxsim <= 1e-6).sum())}")
        print(f"  farthest (max-sim {maxsim.min():.3f}): "
              f"[{classified[far]['src'][:14]}] {classified[far]['title'][:52]}")
        nearest = pool[int(np.argmax(maxsim))]
        print(f"  nearest  (max-sim {maxsim.max():.3f}): "
              f"[{classified[nearest]['src'][:14]}] {classified[nearest]['title'][:52]}")


if __name__ == "__main__":
    main()
