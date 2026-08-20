"""
Rough categories as a COMPLETE PARTITION, which is how they're meant to work.

The earlier routing tests measured the wrong thing. They asked "does one
bucket contain every article relevant to an interest", which is the metric
for single-stage retrieval. The actual design is a funnel:

  stage 1  rough categories -- a complete partition of the feed, like a
           newspaper's section list. EVERY article gets one (or several).
           An "Other" bucket is allowed but must stay small.
  stage 2  filter the narrowed set by the subscriber's interest, cheaply
  stage 3  embeddings find hot spots (cluster size) and novelty (vectors
           far from everything already picked)

Stage 1 does not need to be precise. It needs to be COMPLETE -- an article
it drops is invisible to stage 2 no matter how good stage 2 is.

That reframes the 28% coverage figure from the earlier runs. 28% was an
artifact of using HDBSCAN's own labels as the assignment: HDBSCAN marks
72% of points as noise, which is a statement about density, not about
which centroid an article is nearest to. The centroids exist. Every
article can be assigned to its nearest one, and nothing stops it.

This script measures the difference, and also the multi-label variant
(assign to every category above a threshold, since one article genuinely
belongs to several -- a ChatGPT story is AI and Software and Industry).

    python docs/analysis/tools/measure_full_partition.py
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

# Corrected from the earlier crude pass: an FCC/Disney/ABC story is the
# entertainment industry, not politics. Regulators appearing in a story
# doesn't make it political -- the subject is who owns a broadcaster.
MERGE_RULES = [
    ("Entertainment & Media", ["abc", "disney", "fcc", "tvs", "tv", "comcast", "streaming"]),
    ("Politics & Policy", ["trump", "policy", "governance", "tariff", "iran", "regulation"]),
    ("Crypto", ["bitcoin", "btc", "blockchain"]),
    ("Markets & Stocks", ["stocks", "hong kong", "chinese stocks", "treasury", "bond"]),
    ("Robotics", ["robot", "unitree", "humanoid", "autonomous"]),
    ("Consumer devices", ["apple", "iphone", "airpods", "headphones", "garmin",
                          "trackers", "pixel", "fitness"]),
    ("Energy & Climate", ["battery", "batteries", "nuclear", "ev", "climate", "warming"]),
    ("Security", ["cyber", "security", "malware", "watermark"]),
    ("Health & Science", ["patients", "children", "boston", "quantum", "planetary"]),
    ("AI & ML", ["ai", "openai", "chatgpt", "codex", "gpt", "agents", "llm",
                 "hugging", "generative", "voice", "pytorch", "ocr", "mcp",
                 "coding", "artificial", "embedding", "transformers", "cuda", "gpu"]),
]

INTERESTS = [
    ("robotics", ["robot", "humanoid", "unitree"]),
    ("semiconductors", ["semiconductor", "chip", "foundry", "tsmc", "wafer"]),
    ("quantum computing", ["quantum"]),
    ("Bitcoin", ["bitcoin", "btc", "crypto"]),
    ("光通訊", ["optical", "photonic", "fiber", "transceiver"]),
    ("AI", ["ai ", "artificial intelligence", "llm"]),
]


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
    keep = [k for k in range(len(ids)) if coh[k] >= float(np.median(coh))]

    # category -> centroid, built from the clusters a human merged into it
    cat_members = {}
    for k in keep:
        for cat, kws in MERGE_RULES:
            if any(kw in names[k] for kw in kws):
                cat_members.setdefault(cat, []).extend(np.where(labels == ids[k])[0].tolist())
                break
    cats = sorted(cat_members)
    C = normalize(np.vstack([V[cat_members[c]].mean(axis=0) for c in cats]))
    sim = cosine_similarity(V, C)

    print(f"{len(keep)} kept clusters -> {len(cats)} human-merged categories\n")

    # --- three assignment policies ---------------------------------------
    hd = np.full(len(rows), -1)          # HDBSCAN labels as assignment
    for ci, c in enumerate(cats):
        hd[cat_members[c]] = ci
    nearest = sim.argmax(axis=1)         # every article -> nearest centroid
    best = sim.max(axis=1)

    print("=" * 74)
    print("STAGE 1 COVERAGE -- what fraction of the feed gets a category at all")
    print("=" * 74)
    print(f"  HDBSCAN labels as assignment : {(hd >= 0).sum():5}/{len(rows)} "
          f"({(hd >= 0).mean()*100:5.1f}%)")
    print(f"  nearest centroid, no floor   : {len(rows):5}/{len(rows)} (100.0%)")
    for floor in (0.05, 0.10, 0.20):
        n = int((best >= floor).sum())
        print(f"  nearest centroid, sim >= {floor:.2f} : {n:5}/{len(rows)} "
              f"({n/len(rows)*100:5.1f}%)   'Other' = {len(rows)-n} "
              f"({(len(rows)-n)/len(rows)*100:.1f}%)")

    print("\ncategory sizes under nearest-centroid assignment (sim >= 0.05):")
    ok = best >= 0.05
    for ci, c in enumerate(cats):
        n = int(((nearest == ci) & ok).sum())
        print(f"  {c:24} {n:5}  {n/len(rows)*100:5.1f}%")
    print(f"  {'Other':24} {int((~ok).sum()):5}  {(~ok).mean()*100:5.1f}%")

    # --- does stage 1 keep the relevant articles for stage 2? ------------
    print("\n" + "=" * 74)
    print("STAGE-1 RECALL -- are an interest's articles inside the category")
    print("it routes to? (stage 2 does precision; stage 1 must not lose them)")
    print("=" * 74)
    print(f"{'interest':18} {'rel':>4} {'HDBSCAN-only':>13} {'nearest':>9} "
          f"{'multi-label':>12}   routed to")
    for interest, kws in INTERESTS:
        rel = {i for i, r in enumerate(rows) if any(k in r["text"].lower() for k in kws)}
        if not rel:
            continue
        iv = normalize(encode([interest]) - mean_vec)
        route = int(np.argmax(cosine_similarity(iv, C)[0]))

        a = len(rel & set(np.where(hd == route)[0].tolist())) / len(rel)
        b = len(rel & set(np.where(nearest == route)[0].tolist())) / len(rel)
        # multi-label: an article belongs to every category within 0.05 of
        # its best match, since one story really is AI and Software and Industry
        multi = sim >= (sim.max(axis=1, keepdims=True) - 0.05)
        c_ = len(rel & set(np.where(multi[:, route])[0].tolist())) / len(rel)
        print(f"{interest:18} {len(rel):4} {a*100:12.0f}% {b*100:8.0f}% "
              f"{c_*100:11.0f}%   {cats[route]}")

    multi = sim >= (sim.max(axis=1, keepdims=True) - 0.05)
    print(f"\nmulti-label assigns {multi.sum()/len(rows):.2f} categories per article "
          f"on average")


if __name__ == "__main__":
    main()
