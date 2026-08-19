"""
Can an embedding model replace TF-IDF here, and does it fit the bot VM?

Two questions, because either one alone can veto the design:

RESOURCES -- the bot VM is a VM.Standard.E2.1.Micro (Oracle Always Free):
954 MB total RAM, ~420 MB available with the bot container already running
at ~184 MB, 1 OCPU, x86_64. Disk is not the constraint (33 GB free). So
the number that decides this is peak RSS during encoding, not model size
on disk. Swap exists (1 GB) but is already 240 MB used, and swapping
during a batch encode on one core is not a real option.

Each backend runs in its OWN PROCESS. Measuring them in one process gives
nonsense: RSS is process-wide and cumulative, and `del` + gc.collect()
doesn't return freed pages to the OS, so whichever backend runs last
inherits everything before it. The first version of this script did that
and credited fastembed with +1077 MB.

QUALITY -- TF-IDF was measured failing this task in two specific ways, and
a replacement has to fix both or it isn't worth the RAM:

  1. Word-attractor clusters. TF-IDF put "EmbeddingGemma, Google's new
     efficient embedding model" into a cluster about Spirit Airlines,
     because both say "google". Semantic vectors should not.
  2. Degenerate distances. Picking the article farthest from everything
     already selected only works if "farthest" is discriminating. Under
     TF-IDF the median max-similarity across the candidate pool was
     0.0000 -- hundreds of articles tied at zero, so "farthest" was
     picking arbitrarily. Dense vectors should give every pair a real
     nonzero similarity.

Absolute cosine values are NOT comparable across backends -- each model
family has its own similarity floor (BGE in particular scores everything
high). What's comparable is the SEPARATION between the pairs that should
match and the pairs that shouldn't, which is what the report prints.

    python docs/analysis/tools/bench_embeddings.py              # all
    python docs/analysis/tools/bench_embeddings.py --backend tfidf
"""

import argparse
import json
import os
import subprocess
import sys
import time

import numpy as np

SNAPSHOT = os.path.join("docs", "analysis", "data", "cache-snapshot.tsv")

# Two pairs TF-IDF got wrong, and two it should keep getting right. A
# backend that's actually semantic separates the bottom two from the top two.
PROBES = [
    ("shares only 'google'",
     "EmbeddingGemma, Google's new efficient embedding model",
     "Google and Spirit Airlines announce expanded partnership", False),
    ("shares only 'new'",
     "Will the future of gaming be powered by upstart indie developers",
     "Why AI Output Is the New XSS", False),
    ("same story, no shared words",
     "Bitcoin ETF inflows hit a record high this quarter",
     "Cryptocurrency funds see largest weekly investment on record", True),
    ("same topic, different words",
     "Boston Dynamics unveils a new bipedal warehouse robot",
     "Humanoid machines are moving into logistics work", True),
]

BACKENDS = ["tfidf", "model2vec", "fastembed", "sentence-transformers"]


def rss_mb() -> float:
    import psutil
    return psutil.Process().memory_info().rss / 1e6


def load_texts(limit=2500) -> list[str]:
    texts = []
    with open(SNAPSHOT, encoding="utf-8", errors="replace") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 6 and p[4].strip():
                s = "" if p[5].strip() in ("null", "None") else p[5]
                texts.append((p[4] + " " + s).strip())
            if len(texts) >= limit:
                break
    return texts


def build(backend, texts):
    """Returns (encode_fn, load_seconds, disk_mb)."""
    def pkg_mb(*mods):
        import importlib.util
        total = 0
        for m in mods:
            spec = importlib.util.find_spec(m)
            if spec and spec.submodule_search_locations:
                p = list(spec.submodule_search_locations)[0]
                total += sum(os.path.getsize(os.path.join(r, f))
                             for r, _, fs in os.walk(p) for f in fs)
        return total / 1e6

    t0 = time.perf_counter()
    if backend == "tfidf":
        from sklearn.feature_extraction.text import TfidfVectorizer
        vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), min_df=2).fit(texts)
        # left sparse on purpose: densifying this corpus would allocate
        # ~900 MB, which is itself part of why it's a poor fit for a 954 MB box
        return vec.transform, time.perf_counter() - t0, 0.0

    if backend == "model2vec":
        from model2vec import StaticModel
        m = StaticModel.from_pretrained("minishlab/potion-base-8M")
        return (lambda d: np.asarray(m.encode(d)), time.perf_counter() - t0,
                pkg_mb("model2vec", "tokenizers"))

    if backend == "fastembed":
        from fastembed import TextEmbedding
        m = TextEmbedding("BAAI/bge-small-en-v1.5")
        return (lambda d: np.array(list(m.embed(d))), time.perf_counter() - t0,
                pkg_mb("fastembed", "onnxruntime", "tokenizers"))

    if backend == "sentence-transformers":
        from sentence_transformers import SentenceTransformer
        m = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2", device="cpu")
        return (lambda d: m.encode(d, batch_size=32, show_progress_bar=False),
                time.perf_counter() - t0,
                pkg_mb("torch", "transformers", "sentence_transformers", "tokenizers"))

    raise SystemExit(f"unknown backend {backend}")


def run_one(backend) -> dict:
    from sklearn.metrics.pairwise import cosine_similarity

    texts = load_texts()
    base = rss_mb()
    encode, load_s, disk = build(backend, texts)
    after_load = rss_mb()

    t0 = time.perf_counter()
    V = encode(texts)
    encode_s = time.perf_counter() - t0
    peak = rss_mb()

    probes = []
    for label, a, b, should_match in PROBES:
        pv = encode([a, b])
        probes.append((label, float(cosine_similarity(pv[0:1], pv[1:2])[0, 0]),
                       should_match))

    maxsim = cosine_similarity(V[5:], V[:5]).max(axis=1)
    matched = [s for _, s, m in probes if m]
    unmatched = [s for _, s, m in probes if not m]

    return {
        "backend": backend, "dim": int(V.shape[1]), "disk_mb": disk,
        "rss_load_mb": after_load - base, "rss_peak_mb": peak - base,
        "load_s": load_s, "encode_s": encode_s, "docs": len(texts),
        "probes": probes,
        # what actually matters: does it put the real matches above the
        # word-overlap coincidences, and by how much
        "separation": min(matched) - max(unmatched),
        "median_maxsim": float(np.median(maxsim)),
        "zeros": int((maxsim <= 1e-6).sum()), "pool": len(maxsim),
    }


AVAILABLE_MB = 420        # measured on the bot VM with the container running


def print_report(results):
    print(f"\n{'backend':24} {'dim':>5} {'disk':>7} {'RSS peak':>9} "
          f"{'docs/s':>7} {'separation':>11} {'med max-sim':>12} {'zeros':>10}")
    print("-" * 94)
    for r in results:
        print(f"{r['backend']:24} {r['dim']:5} {r['disk_mb']:6.0f}M "
              f"{r['rss_peak_mb']:8.0f}M {r['docs']/r['encode_s']:7.0f} "
              f"{r['separation']:+11.3f} {r['median_maxsim']:12.4f} "
              f"{r['zeros']:5}/{r['pool']}")

    print(f"\nprobe detail (want the two 'same' rows above the two 'shares only' rows;\n"
          f"absolute values are not comparable across backends, the gap is):")
    for r in results:
        print(f"  {r['backend']}")
        for label, score, should in r["probes"]:
            print(f"    {score:+.3f}  {'MATCH   ' if should else 'unrelated'}  {label}")

    print(f"\nfit against the bot VM (~{AVAILABLE_MB} MB available, 1 OCPU):")
    for r in results:
        head = AVAILABLE_MB - r["rss_peak_mb"]
        verdict = f"FITS, {head:.0f} MB headroom" if head > 80 else (
            f"TOO TIGHT, {head:.0f} MB headroom" if head > 0 else
            f"DOES NOT FIT, over by {-head:.0f} MB")
        print(f"  {r['backend']:24} {r['rss_peak_mb']:6.0f} MB  {verdict}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", choices=BACKENDS)
    ap.add_argument("--json", action="store_true", help="internal: emit one result as JSON")
    args = ap.parse_args()

    if not os.path.exists(SNAPSHOT):
        sys.exit(f"no snapshot at {SNAPSHOT} -- run tools/fetch_cache_snapshot.py")

    if args.backend:
        r = run_one(args.backend)
        if args.json:
            print("###JSON###" + json.dumps(r))
        else:
            print_report([r])
        return

    results = []
    for b in BACKENDS:
        print(f"measuring {b} in a fresh process ...", file=sys.stderr)
        proc = subprocess.run([sys.executable, __file__, "--backend", b, "--json"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
        line = next((l for l in proc.stdout.splitlines() if l.startswith("###JSON###")), None)
        if line is None:
            print(f"  {b} failed -- skipped ({proc.stderr.strip().splitlines()[-1:]})",
                  file=sys.stderr)
            continue
        results.append(json.loads(line[len("###JSON###"):]))
    print_report(results)


if __name__ == "__main__":
    main()
