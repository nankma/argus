"""
Three ways to decide what "one story" means, on the same data.

Single-linkage joins A and C whenever some B sits between them, so its
threshold governs the distance between NEIGHBOURS, not the size of the
result. Measured on the AI category, that lets a group grow to 180 of 531
articles at cosine 0.40, containing a pair whose actual similarity is
-0.306 -- an enterprise-AI-strategy piece and a story about ChatGPT
swearing, linked through a five-article chain where every individual step
clears the bar.

Two alternatives constrain the cluster itself rather than the links:

  COMPLETE linkage bounds the DIAMETER: every pair inside a cluster must
  clear the threshold. Nothing can chain.

  CENTRE + RADIUS bounds the distance to a CENTRE: pick the densest
  article, absorb everything within R of it, repeat on what's left. This
  is the textbook picture of a cluster -- a middle with everything else
  gathered around it -- and R is the parameter, not the neighbour gap.

The two are not the same: a cluster of radius R can have a diameter up to
2R, so centre+radius is the looser of the two, which matters here because
genuine multi-outlet coverage of one event is less similar than one
outlet's own reruns.

    python docs/analysis/tools/compare_linkage.py --category AI
"""

import argparse
import os
import sys
from collections import defaultdict

import numpy as np
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import squareform
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
                cats = [c.strip() for c in p[3].split(",") if c.strip()]
                if cats and cats != ["Other"]:
                    s = "" if p[5].strip() in ("null", "None") else p[5]
                    rows.append({"src": p[0], "cats": cats, "title": p[4],
                                 "text": (p[4] + " " + s).strip()})
    return rows


def single_linkage(S, th):
    n = len(S)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for a, b in zip(*np.where(np.triu(S >= th, 1))):
        ra, rb = find(int(a)), find(int(b))
        if ra != rb:
            parent[ra] = rb
    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return [g for g in groups.values() if len(g) > 1]


def complete_linkage(S, th):
    """Cuts the complete-linkage tree so every pair inside a cluster is at
    least `th` similar. Bounds the diameter, so chaining is impossible."""
    D = np.clip(1.0 - S, 0, None)
    np.fill_diagonal(D, 0.0)
    Z = linkage(squareform(D, checks=False), method="complete")
    labels = fcluster(Z, t=1.0 - th, criterion="distance")
    groups = defaultdict(list)
    for i, lab in enumerate(labels):
        groups[lab].append(i)
    return [g for g in groups.values() if len(g) > 1]


def centre_radius(S, r):
    """Leader clustering, densest-first. Repeatedly take the article with
    the most neighbours within R, make it the centre, absorb everything
    within R of THAT article, and continue on what's left.

    Densest-first rather than arbitrary-first on purpose: seeding from a
    random article makes the result depend on input order, and a centre
    that happens to sit at the edge of a real story pulls in its
    neighbourhood while leaving the story's own middle for a later pass."""
    n = len(S)
    unassigned = set(range(n))
    groups = []
    while unassigned:
        idx = np.fromiter(unassigned, dtype=int)
        neighbours = (S[np.ix_(idx, idx)] >= r).sum(axis=1)
        centre = int(idx[int(np.argmax(neighbours))])
        members = [int(i) for i in idx if S[centre, i] >= r or i == centre]
        groups.append(members)
        unassigned -= set(members)
    return [g for g in groups if len(g) > 1]


def report(name, groups, rows, idx, S):
    if not groups:
        print(f"  {name:16} {0:7}")
        return
    biggest = max(groups, key=len)
    sub = S[np.ix_(biggest, biggest)]
    srcs = len({rows[idx[i]]["src"] for i in biggest})
    worst = sub[np.triu_indices(len(biggest), 1)].min() if len(biggest) > 1 else 1.0
    print(f"  {name:16} {len(groups):7} {len(biggest):8} {srcs:8} "
          f"{worst:+13.3f}  {sum(len(g) for g in groups):>9}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", default=SNAPSHOT)
    ap.add_argument("--category", default="AI")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    rows = load(args.snapshot)
    idx = [i for i, r in enumerate(rows) if args.category in r["cats"]]
    print(f"{args.category}: {len(idx)} classified articles")

    from model2vec import StaticModel
    model = StaticModel.from_pretrained("minishlab/potion-base-8M")
    V = np.asarray(model.encode([r["text"] for r in rows]), dtype=np.float32)
    V = V - V.mean(axis=0)
    V = V / (np.linalg.norm(V, axis=1, keepdims=True) + 1e-9)
    S = cosine_similarity(V[idx])
    np.fill_diagonal(S, 0.0)

    print("\n'worst pair' is the LEAST similar pair inside the biggest group --")
    print("the number that exposes chaining. A method that bounds the cluster")
    print("cannot produce a worst pair below its own threshold.\n")
    for th in (0.75, 0.60, 0.50, 0.40, 0.30):
        print(f"threshold {th:.2f}")
        print(f"  {'method':16} {'groups':>7} {'largest':>8} {'sources':>8} "
              f"{'worst pair':>13} {'clustered':>9}")
        report("single", single_linkage(S, th), rows, idx, S)
        report("complete", complete_linkage(S, th), rows, idx, S)
        report("centre+radius", centre_radius(S, th), rows, idx, S)
        print()


if __name__ == "__main__":
    main()
