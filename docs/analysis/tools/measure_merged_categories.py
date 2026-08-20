"""
Does human-merging the discovered clusters into broad categories help?

Two designs get compared here, because they answer the same product need
in opposite directions:

  A. DISCOVER THEN MERGE -- cluster the corpus, then a human merges the
     fragments into real categories ("trump/iran" + "trump/tariffs" +
     "abc/disney/fcc" -> Politics). This is what was asked about.

  B. NAME THEN RETRIEVE -- a human writes the category list first, and
     each category's members are whatever kNN/BM25 retrieves for its name.
     No clustering in the path at all.

The measurement that matters is the same as in measure_routing.py: COARSE
RECALL against a subscriber interest. A later fine step fixes precision;
nothing recovers an article the coarse step never returned.

The prediction going in, from measure_routing.py's three failure modes, is
that merging fixes exactly one of them:

  - fragmentation (relevant articles split across sibling clusters) --
    merging should fix this directly, since the siblings become one bucket
  - the coverage ceiling (72% of articles are in NO cluster) -- merging
    cannot fix this; combining empty-ish buckets adds no articles
  - short interest strings routing to noise -- unaffected

So merging should raise recall for topics that were split, and leave the
ceiling exactly where it was. This script checks whether that is what
actually happens, and what it costs in precision.

    python docs/analysis/tools/measure_merged_categories.py
"""

import os
import sys

import numpy as np
from sklearn.cluster import HDBSCAN
from sklearn.manifold import TSNE
from sklearn.metrics.pairwise import cosine_similarity

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_taxonomy import (SNAPSHOT, RS, MIN_CLUSTER_SIZE, get_encoder,
                            label_clusters, load_rows, normalize)

# A plausible human merge of the 36 model2vec clusters, by the keywords
# their c-TF-IDF labels contain. This stands in for the manual pass:
# a person reading the label list and grouping it.
MERGE_RULES = [
    ("Politics & Policy", ["trump", "policy", "governance", "fcc", "disney", "tariff", "iran"]),
    ("Crypto", ["bitcoin", "btc", "blockchain"]),
    ("Markets & Stocks", ["stocks", "hong kong", "chinese stocks"]),
    ("Robotics", ["robot", "unitree", "humanoid", "autonomous"]),
    ("Consumer devices", ["apple", "iphone", "airpods", "headphones", "garmin",
                          "trackers", "tvs", "tv", "comcast", "pixel"]),
    ("Energy", ["battery", "batteries", "nuclear", "ev", "climate", "warming"]),
    ("AI & ML", ["ai", "openai", "chatgpt", "codex", "gpt", "agents", "llm",
                 "hugging", "generative", "voice", "pytorch", "ocr", "mcp",
                 "coding", "artificial"]),
    ("Security", ["cyber", "security", "malware"]),
    ("Health", ["patients", "children", "boston"]),
]

INTERESTS = [
    ("robotics", ["robot", "humanoid", "unitree"]),
    ("semiconductors", ["semiconductor", "chip", "foundry", "tsmc", "wafer"]),
    ("quantum computing", ["quantum"]),
    ("Bitcoin", ["bitcoin", "btc", "crypto"]),
    ("光通訊", ["optical", "photonic", "fiber", "transceiver"]),
]


def assign_merged(name):
    for cat, kws in MERGE_RULES:
        if any(k in name for k in kws):
            return cat
    return None


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    if not os.path.exists(SNAPSHOT):
        sys.exit(f"no snapshot at {SNAPSHOT}")

    rows = load_rows()
    encode = get_encoder("model2vec")
    V = encode([r["text"] for r in rows])
    mean_vec = V.mean(axis=0)
    V = normalize(V - mean_vec)

    P = TSNE(n_components=2, perplexity=30, init="pca", metric="cosine",
             random_state=RS).fit_transform(V)
    labels = HDBSCAN(min_cluster_size=MIN_CLUSTER_SIZE, min_samples=4).fit_predict(P)
    ids = sorted(set(labels) - {-1})
    names = label_clusters([r["text"] for r in rows], labels, ids)

    coh = []
    for c in ids:
        idx = np.where(labels == c)[0]
        S = cosine_similarity(V[idx])
        coh.append(float((S.sum() - len(idx)) / (len(idx) * (len(idx) - 1))))
    cutoff = float(np.median(coh))
    keep = [k for k in range(len(ids)) if coh[k] >= cutoff]

    # --- design A: merge the kept clusters -------------------------------
    merged = {}                                   # category -> article indexes
    unmerged = []
    for k in keep:
        cat = assign_merged(names[k])
        members = np.where(labels == ids[k])[0]
        if cat is None:
            unmerged.append((names[k], len(members)))
        else:
            merged.setdefault(cat, []).extend(members.tolist())

    total_in_cat = sum(len(v) for v in merged.values())
    print(f"{len(keep)} kept clusters -> {len(merged)} merged categories")
    print(f"{total_in_cat}/{len(rows)} articles in a merged category "
          f"({total_in_cat/len(rows)*100:.0f}%)  "
          f"-- the coverage ceiling is unchanged by merging\n")
    for cat, members in sorted(merged.items(), key=lambda t: -len(t[1])):
        print(f"  {cat:22} {len(members):5} articles")
    if unmerged:
        print(f"  (unmerged clusters: {', '.join(n for n, _ in unmerged)})")

    # --- design B: name the category, retrieve its members ---------------
    cat_names = [c for c, _ in MERGE_RULES]
    cat_vecs = normalize(encode(cat_names) - mean_vec)
    sim_to_cat = cosine_similarity(V, cat_vecs)

    print("\n" + "=" * 78)
    print("COARSE RECALL for a subscriber interest")
    print("=" * 78)
    print(f"{'interest':18} {'rel':>4} {'A: merged cat':>15} {'B: named cat kNN':>18} "
          f"{'direct kNN-200':>15}")

    for interest, kws in INTERESTS:
        rel = {i for i, r in enumerate(rows) if any(k in r["text"].lower() for k in kws)}
        if not rel:
            continue
        iv = normalize(encode([interest]) - mean_vec)

        # A: route the interest to the merged category holding the most of
        # the cluster it best matches, then pull that whole category
        best_cat, best_recall = None, 0.0
        cat_centroids = {c: normalize(V[m].mean(axis=0)[None])[0] for c, m in merged.items()}
        order = sorted(cat_centroids, key=lambda c: -float(iv[0] @ cat_centroids[c]))
        picked = order[0]
        a_recall = len(rel & set(merged[picked])) / len(rel)

        # B: rank all articles by similarity to the NAMED category the
        # interest routes to, take top 200
        ci = int(np.argmax([float(iv[0] @ cat_vecs[j]) for j in range(len(cat_names))]))
        b_top = sim_to_cat[:, ci].argsort()[::-1][:200]
        b_recall = len(rel & set(b_top.tolist())) / len(rel)

        # control: direct kNN on the interest itself
        d_top = cosine_similarity(iv, V)[0].argsort()[::-1][:200]
        d_recall = len(rel & set(d_top.tolist())) / len(rel)

        print(f"{interest:18} {len(rel):4} {a_recall*100:13.0f}% "
              f"{b_recall*100:16.0f}% {d_recall*100:14.0f}%")
        print(f"{'':18} {'':4} -> {picked} / {cat_names[ci]}")


if __name__ == "__main__":
    main()
