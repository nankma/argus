"""
Does the rough taxonomy work as a RETRIEVAL BUCKET?

The design this tests is not "classify each article into a final label".
It's two-stage retrieval:

    user asks about AAOI
      -> map the interest to a rough cluster  (e.g. "telecom / optical")
      -> pull EVERY article in that cluster
      -> narrow with a fine step, or hand a small set to the LLM

Under that design the taxonomy never needs an "AAOI" cluster. It needs
two things, and they are different from classification accuracy:

  ROUTING AGREEMENT -- the interest string and the articles about it must
  land on the SAME cluster. The interest is 4 characters ("AAOI"), the
  article is a paragraph; short-vs-long is a known weak spot for embedding
  similarity, so this cannot be assumed.

  COARSE RECALL -- of the articles genuinely relevant to the interest, what
  fraction is inside the cluster we pulled? A later fine step can fix
  precision; nothing downstream can recover an article the coarse pull
  never returned. Recall is therefore the metric that decides the design.

The ceiling on recall is coverage: an article HDBSCAN left as noise, or
that the coherence prune dropped, is in no bucket at all and is
unreachable by any routing.

    python docs/analysis/tools/test_routing.py --backend model2vec
"""

import argparse
import os
import sys

import numpy as np
from sklearn.cluster import HDBSCAN
from sklearn.manifold import TSNE
from sklearn.metrics.pairwise import cosine_similarity

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_taxonomy import (SNAPSHOT, RS, MIN_CLUSTER_SIZE, get_encoder,
                            label_clusters, load_rows, normalize)

# Real subscriber interests from the live DB, with keyword ground truth.
# Keywords are a proxy for relevance, not a gold label -- they under-count
# (an article about Applied Optoelectronics that never says "optical") and
# over-count (any passing mention). Good enough to compare backends and to
# tell "routes somewhere sensible" from "routes nowhere".
INTERESTS = [
    ("robotics", ["robot", "humanoid", "unitree"]),
    ("semiconductors", ["semiconductor", "chip", "foundry", "tsmc", "wafer"]),
    ("quantum computing", ["quantum"]),
    ("Bitcoin", ["bitcoin", "btc", "crypto"]),
    ("光通訊", ["optical", "photonic", "fiber", "transceiver"]),
    ("AAOI", ["aaoi", "applied optoelectronics", "optical", "transceiver"]),
    ("AI", ["ai ", "artificial intelligence", "llm", "model"]),
]

# Probe headlines for interests with too little real coverage to judge.
# Written to be realistic rather than convenient -- an AAOI story is a
# supplier-earnings story, which is exactly the kind of article that could
# plausibly route to finance, hardware, or nothing.
PROBES = {
    "AAOI": [
        "Applied Optoelectronics shares jump on strong 800G transceiver demand",
        "AAOI raises guidance as hyperscaler orders for optical modules accelerate",
    ],
    "光通訊": [
        "Coherent and Lumentum expand optical transceiver capacity for AI data centres",
    ],
    "semiconductors": [
        "TSMC's 2nm yields improve ahead of volume production next quarter",
    ],
}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", default="model2vec",
                    choices=["model2vec", "fastembed", "sentence-transformers"])
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    if not os.path.exists(SNAPSHOT):
        sys.exit(f"no snapshot at {SNAPSHOT}")

    rows = load_rows()
    encode = get_encoder(args.backend)
    V = encode([r["text"] for r in rows])
    mean_vec = V.mean(axis=0)          # centering: see build_taxonomy
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

    C = normalize(np.vstack([V[labels == ids[k]].mean(axis=0) for k in keep]))
    kept_names = [names[k] for k in keep]
    # article index -> position in `keep`, or -1 if in no kept bucket
    bucket = np.full(len(rows), -1)
    for pos, k in enumerate(keep):
        bucket[labels == ids[k]] = pos

    print(f"backend {args.backend}: {len(ids)} clusters, {len(keep)} kept, "
          f"{(bucket >= 0).sum()}/{len(rows)} articles in a bucket "
          f"({(bucket >= 0).mean()*100:.0f}%)\n")

    def route(texts):
        v = normalize(encode(texts) - mean_vec)
        s = cosine_similarity(v, C)
        return s.argmax(axis=1), s.max(axis=1)

    print("=" * 78)
    print("ROUTING: where does the INTEREST go, and where do its ARTICLES go?")
    print("=" * 78)
    for interest, kws in INTERESTS:
        rel = [i for i, r in enumerate(rows)
               if any(k in r["text"].lower() for k in kws)]
        i_b, i_s = route([interest])
        i_b, i_s = int(i_b[0]), float(i_s[0])

        line = f"\n{interest!r}  ({len(rel)} keyword-relevant articles)"
        print(line)
        print(f"  interest routes to -> [{kept_names[i_b]}]  sim {i_s:.3f}")

        if not rel:
            print("  no real articles to check against")
        else:
            in_bucket = [i for i in rel if bucket[i] == i_b]
            unreachable = [i for i in rel if bucket[i] == -1]
            # where do the relevant articles actually live?
            spread = {}
            for i in rel:
                if bucket[i] >= 0:
                    spread[kept_names[bucket[i]]] = spread.get(kept_names[bucket[i]], 0) + 1
            top = sorted(spread.items(), key=lambda t: -t[1])[:3]
            print(f"  COARSE RECALL: {len(in_bucket)}/{len(rel)} "
                  f"({len(in_bucket)/len(rel)*100:.0f}%) of relevant articles are in "
                  f"that one bucket")
            print(f"    unreachable (in no bucket at all): {len(unreachable)}/{len(rel)} "
                  f"({len(unreachable)/len(rel)*100:.0f}%)")
            if top:
                print(f"    they actually spread across: "
                      + "; ".join(f"{n} ({c})" for n, c in top))

        # Control: skip buckets entirely and rank every article by similarity
        # to the interest. If this beats routing, the clusters are not earning
        # their place in the retrieval path.
        if rel:
            v = normalize(encode([interest]) - mean_vec)
            ranked = cosine_similarity(v, V)[0].argsort()[::-1]
            relset = set(rel)
            for n in (50, 200):
                hits = sum(1 for i in ranked[:n] if i in relset)
                print(f"    [direct kNN, no clusters] top-{n}: {hits}/{len(rel)} "
                      f"({hits/len(rel)*100:.0f}% recall, {hits/n*100:.0f}% precision)")

        for probe in PROBES.get(interest, []):
            p_b, p_s = route([probe])
            agree = "AGREES with the interest" if int(p_b[0]) == i_b else "DISAGREES"
            print(f"  probe -> [{kept_names[int(p_b[0])]}] sim {float(p_s[0]):.3f}  [{agree}]")
            print(f"      {probe[:70]}")


if __name__ == "__main__":
    main()
