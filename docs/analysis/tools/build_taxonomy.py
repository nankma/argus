"""
Build a frozen cluster taxonomy from the news cache, and look for hot news
inside it.

The design this implements, in three steps:

  1. OFFLINE (here, on a dev box): encode the whole snapshot, cluster it,
     prune the junk clusters, label what's left. Memory is free here.
  2. PERSIST: save the surviving centroids. 35 clusters x 384 dims is
     54 KB -- a file or a DB row.
  3. RUNTIME (on the VM): encode one incoming article, take the nearest
     centroid. No LLM call, so no per-article API cost, which is the whole
     point -- the DeepSeek classifier this replaces went silently dark for
     three days and cost money the entire time it worked.

Freezing is what makes this usable as a taxonomy. Re-clustering every
cycle scored ARI 0.478 across random seeds on identical data -- far too
unstable to be categories. A taxonomy built once and frozen cannot drift,
by construction; the risk moves to whether it still absorbs *new* articles
months later, which is what the --holdout mode measures.

Clustering happens on a t-SNE layout but centroids are computed in the
ORIGINAL embedding space. That matters: t-SNE has no transform for unseen
points, so a t-SNE-space centroid could never classify a new article.

Then, within each rough cluster, a second finer pass looks for groups of
articles that are near-duplicates of each other. Several outlets covering
the same thing at once is the corroboration signal for hot news.

    python docs/analysis/tools/build_taxonomy.py --backend model2vec
    python docs/analysis/tools/build_taxonomy.py --backend fastembed
    python docs/analysis/tools/build_taxonomy.py --backend model2vec --holdout
"""

import argparse
import json
import os
import sys
import time
from collections import Counter

import numpy as np
from sklearn.cluster import HDBSCAN
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.manifold import TSNE
from sklearn.metrics.pairwise import cosine_similarity

SNAPSHOT = os.path.join("docs", "analysis", "data", "cache-snapshot.tsv")
OUT_DIR = os.path.join("docs", "analysis", "data")
RS = 42

# Fine-cluster threshold: how similar two articles must be to count as
# covering the same story. Deliberately well above the rough-cluster
# threshold -- a rough cluster is a topic, a fine cluster is one event.
FINE_THRESHOLD = 0.75
MIN_CLUSTER_SIZE = 8


def load_rows(limit=None):
    rows = []
    with open(SNAPSHOT, encoding="utf-8", errors="replace") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 6 and p[4].strip():
                s = "" if p[5].strip() in ("null", "None") else p[5]
                rows.append({
                    "src": p[0], "published": p[1], "fetched": p[2],
                    "llm_cats": [c.strip() for c in p[3].split(",") if c.strip()],
                    "title": p[4], "text": (p[4] + " " + s).strip(),
                })
            if limit and len(rows) >= limit:
                break
    rows.sort(key=lambda r: r["fetched"])
    return rows


def get_encoder(backend):
    if backend == "model2vec":
        from model2vec import StaticModel
        m = StaticModel.from_pretrained("minishlab/potion-base-8M")
        return lambda d: np.asarray(m.encode(list(d)), dtype=np.float32)
    if backend == "fastembed":
        os.environ["OMP_NUM_THREADS"] = "1"
        from fastembed import TextEmbedding
        m = TextEmbedding("BAAI/bge-small-en-v1.5", threads=1)
        return lambda d: np.array(list(m.embed(list(d), batch_size=32, parallel=0)),
                                  dtype=np.float32)
    if backend == "sentence-transformers":
        from sentence_transformers import SentenceTransformer
        m = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2", device="cpu")
        return lambda d: np.asarray(
            m.encode(list(d), batch_size=32, show_progress_bar=False), dtype=np.float32)
    raise SystemExit(f"unknown backend {backend}")


def normalize(V):
    return V / (np.linalg.norm(V, axis=1, keepdims=True) + 1e-9)


def label_clusters(texts, labels, ids, top_n=4):
    """c-TF-IDF: pool each cluster's documents into one pseudo-document and
    run TF-IDF across clusters. Terms that are frequent *in* a cluster and
    rare *across* clusters are the distinctive ones -- plain centroid terms
    just return whatever is globally common ("ai", "new")."""
    pooled = [" ".join(texts[i] for i in np.where(labels == c)[0]) for c in ids]
    tv = TfidfVectorizer(stop_words="english", ngram_range=(1, 2),
                         max_features=40000, min_df=1)
    X = tv.fit_transform(pooled)
    terms = np.array(tv.get_feature_names_out())
    out = []
    for r in range(X.shape[0]):
        row = np.asarray(X[r].todense()).ravel()
        out.append(", ".join(terms[i] for i in row.argsort()[::-1][:top_n]))
    return out


def fine_clusters(V, threshold):
    """Single-linkage connected components above `threshold`. Returns a list
    of member-index lists, largest first, singletons dropped."""
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
    ap.add_argument("--backend", default="model2vec",
                    choices=["model2vec", "fastembed", "sentence-transformers"])
    ap.add_argument("--holdout", action="store_true",
                    help="build on the older 60%%, measure absorption of the newer 40%%")
    ap.add_argument("--fine-threshold", type=float, default=FINE_THRESHOLD)
    ap.add_argument("--center", action="store_true",
                    help="subtract the corpus mean before normalizing. BERT-family "
                         "embeddings are anisotropic -- they occupy a narrow cone, so "
                         "every pair looks similar and density clustering has no "
                         "contrast to work with. bge-small's random-pair baseline is "
                         "0.502 raw, against model2vec's 0.120. Removing the shared "
                         "component is the standard remedy.")
    ap.add_argument("--save", action="store_true", help="write the centroids JSON")
    args = ap.parse_args()

    # Article titles carry emoji. Piped stdout on Windows defaults to cp1252,
    # which raises UnicodeEncodeError partway through printing them.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    if not os.path.exists(SNAPSHOT):
        sys.exit(f"no snapshot at {SNAPSHOT} -- run tools/fetch_cache_snapshot.py")

    rows = load_rows()
    encode = get_encoder(args.backend)
    t0 = time.perf_counter()
    V = encode([r["text"] for r in rows])
    print(f"backend {args.backend}: encoded {len(rows)} articles in "
          f"{time.perf_counter() - t0:.1f}s, dim {V.shape[1]}")

    build_idx = np.arange(int(len(rows) * 0.6)) if args.holdout else np.arange(len(rows))

    if args.center:
        # The mean comes from the BUILD SET only, not the whole corpus. Two
        # reasons, one practical and one that was an actual bug: at build time
        # only the build set exists, so a corpus mean isn't available to a real
        # deployment; and centering a subset by a mean computed over a superset
        # leaves that subset with a residual offset, which re-introduces exactly
        # the anisotropy centering is meant to remove. Doing it the wrong way
        # collapsed the model2vec holdout run from 75 clusters to 2.
        #
        # This mean is stored with the centroids and must be applied to every
        # article classified later, or runtime vectors land in a different space
        # than the taxonomy was built in.
        mean_vec = V[build_idx].mean(axis=0)
        V = V - mean_vec
    else:
        mean_vec = np.zeros(V.shape[1], dtype=np.float32)
    V = normalize(V)
    Vb = V[build_idx]

    P = TSNE(n_components=2, perplexity=30, init="pca", metric="cosine",
             random_state=RS).fit_transform(Vb)
    labels = HDBSCAN(min_cluster_size=MIN_CLUSTER_SIZE, min_samples=4).fit_predict(P)
    ids = sorted(set(labels) - {-1})
    if not ids:
        sys.exit("no clusters found")

    rng = np.random.default_rng(RS)
    baseline = float(np.mean(cosine_similarity(
        Vb[rng.integers(len(Vb), size=500)], Vb[rng.integers(len(Vb), size=500)])))

    names = label_clusters([rows[i]["text"] for i in build_idx], labels, ids)
    clusters = []
    for k, c in enumerate(ids):
        idx = np.where(labels == c)[0]
        S = cosine_similarity(Vb[idx])
        coh = float((S.sum() - len(idx)) / (len(idx) * (len(idx) - 1)))
        clusters.append({"name": names[k], "n": len(idx), "coh": coh,
                         "members": idx, "centroid": Vb[idx].mean(axis=0)})

    cutoff = float(np.median([c["coh"] for c in clusters]))
    kept = [c for c in clusters if c["coh"] >= cutoff]
    print(f"{len(clusters)} raw clusters, {(labels == -1).sum()} noise points; "
          f"random-pair baseline {baseline:.3f}")
    print(f"pruning at median coherence {cutoff:.3f} -> {len(kept)} kept\n")

    # ---- the derived categories ----------------------------------------
    print("=" * 78)
    print(f"DERIVED CATEGORIES  ({args.backend})")
    print("=" * 78)
    print(f"{'n':>5} {'coh':>7} {'xbase':>6}  distinctive terms")
    for c in sorted(kept, key=lambda c: -c["n"]):
        print(f"{c['n']:5} {c['coh']:7.3f} {c['coh']/baseline:5.1f}x  {c['name']}")
    covered = sum(c["n"] for c in kept)
    print(f"\n{covered}/{len(build_idx)} articles ({covered/len(build_idx)*100:.0f}%) "
          f"fall in a kept cluster")

    # ---- absorption of unseen articles ---------------------------------
    if args.holdout:
        C = normalize(np.vstack([c["centroid"] for c in kept]))
        held = V[len(build_idx):]
        sim = cosine_similarity(held, C)
        order = np.argsort(sim, axis=1)
        top = sim[np.arange(len(held)), order[:, -1]]
        gap = top - sim[np.arange(len(held)), order[:, -2]]
        print(f"\nabsorbing {len(held)} unseen newer articles:")
        for th, mg in ((0.30, 0.0), (0.40, 0.02), (0.50, 0.05)):
            ok = (top >= th) & (gap >= mg)
            print(f"  sim>={th:.2f} margin>={mg:.2f}: {ok.sum():4}/{len(held)} "
                  f"({ok.mean()*100:5.1f}%)")
        rnd = sim[np.arange(len(held)), rng.integers(len(kept), size=len(held))]
        print(f"  median best-match {np.median(top):.3f} vs random cluster "
              f"{np.median(rnd):.3f}")

    # ---- fine clusters inside each category = hot news candidates -------
    print("\n" + "=" * 78)
    print(f"HOT-NEWS CANDIDATES  (fine clusters at cosine >= {args.fine_threshold})")
    print("=" * 78)
    total_groups = 0
    for c in sorted(kept, key=lambda c: -c["n"]):
        groups = fine_clusters(Vb[c["members"]], args.fine_threshold)
        total_groups += len(groups)
        if not groups:
            continue
        print(f"\n[{c['name']}]  n={c['n']} -> {len(groups)} group(s), "
              f"largest {len(groups[0])}")
        for g in groups[:2]:
            gi = c["members"][g]
            srcs = Counter(rows[build_idx[i]]["src"] for i in gi)
            print(f"    {len(g)} articles from {len(srcs)} source(s): "
                  f"{dict(srcs.most_common(3))}")
            for i in gi[:3]:
                r = rows[build_idx[i]]
                print(f"      [{r['src'][:12]:12}] {r['title'][:58]}")
    print(f"\n{total_groups} multi-article group(s) across {len(kept)} categories")

    if args.save:
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, f"taxonomy-{args.backend}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "backend": args.backend, "dim": int(V.shape[1]),
                "built_from": len(build_idx), "coherence_cutoff": cutoff,
                "centered": bool(args.center), "mean": mean_vec.tolist(),
                "clusters": [{"name": c["name"], "n": int(c["n"]),
                              "coherence": c["coh"],
                              "centroid": normalize(c["centroid"][None])[0].tolist()}
                             for c in kept],
            }, f)
        print(f"\nwrote {path} ({os.path.getsize(path)/1024:.0f} KB)")


if __name__ == "__main__":
    main()
