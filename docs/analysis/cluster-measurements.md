# Cluster Measurements — the real cache, 2026-08-19

The numbers behind `news-ranking-plan.md`'s Option B. Everything here was
measured on a live snapshot of the production cache, not estimated.

**📊 Interactive report with both scatter plots:**
[cluster-report.html](cluster-report.html) — open it locally, or view the
published copy at
<https://claude.ai/code/artifact/c8b4c66f-ce7d-480f-80b6-d6bc3bc49ef7>.

The HTML is **generated, never hand-edited** — regenerate it any time with
`python analysis/tools/build_cluster_report.py` (see [README](README.md)).
It is gitignored, like `showcase.html`, because it is a 700 KB build
artifact derived from a snapshot that goes stale within days. This file
holds the findings that outlive it.

---

## Snapshot

| | |
|---|---|
| Articles | 2,082 |
| Sources | 21 registered, 20 present in the window |
| Window | 48 h (the cache TTL) |
| With summary text | 1,469 of 2,082 |
| Snapshot taken | 2026-08-19 |

## Story clustering — how many clusters, and how big

Connected components (single-linkage) over a similarity threshold, which is
the standard approach in the near-duplicate literature: if A~B and B~C then
A, B and C are one story.

### TF-IDF cosine

| Threshold | Clusters | Singletons | Multi-article | Cross-source | Largest |
|---|---|---|---|---|---|
| 0.30 | 1,985 | 1,913 | 72 | 15 | 5 |
| **0.40** | **2,028** | **1,988** | **40** | **7** | **5** |
| 0.50 | 2,044 | 2,015 | 29 | 6 | 5 |
| 0.60 | 2,053 | 2,032 | 21 | 3 | 5 |
| 0.70 | 2,065 | 2,049 | 16 | 0 | 3 |

### BM25 (normalized)

| Threshold | Clusters | Singletons | Multi-article | Cross-source | Largest |
|---|---|---|---|---|---|
| 0.10 | 117 | 103 | 14 | 7 | **1,951** ⚠ |
| 0.15 | 1,060 | 865 | 195 | 106 | **651** ⚠ |
| 0.20 | 1,714 | 1,535 | 179 | 70 | 17 |
| 0.30 | 1,976 | 1,910 | 66 | 22 | 8 |
| 0.40 | 2,033 | 1,994 | 39 | 9 | 5 |

⚠ = degenerate. The clustering has collapsed, not converged.

### Size distribution at the working point (TF-IDF 0.40)

| Cluster size | Count |
|---|---|
| 1 (singleton) | 1,988 |
| 2 | 32 |
| 3 | 3 |
| 4 | 4 |
| 5 | 1 |

**2,082 articles → 2,028 clusters is a reduction of 54, or 2.6%.** Only 94
articles join a cluster at all. Clustering barely consolidates anything —
that is the finding, not a caveat to it.

## Findings

### 1. The corroboration signal is real but very thin

Seven cross-source clusters in a 48-hour window. Enough to identify the
handful of stories the whole industry covered at once, and nothing more.
**Corroboration can promote the top few items; it cannot rank the other
~2,000**, which all score identically as uncorroborated. Option B needs a
second signal underneath it.

### 2. BM25 collapses catastrophically below ~0.20

At threshold 0.10, **1,951 of 2,082 articles land in one cluster**. This is
single-linkage chaining — A resembles B, B resembles C, so all three merge,
repeated until nearly the whole cache is one story. Not a BM25 defect; it is
what connected-components clustering does with a permissive threshold on a
dense similarity matrix. TF-IDF cosine happens to be sparse enough that it
never gets there. **If BM25 is used, the threshold has to be validated
against a real snapshot, not assumed.**

### 3. There is no dense cluster structure to find

Compressing the article vectors to 50 dimensions retains only **6.6% of the
variance**. 2,082 articles from 20 outlets over 48 hours are genuinely about
~2,000 different things. This is why the scatter plot is a diffuse cloud
rather than tidy blobs, and why k-means (K=12, tried separately) put 59% of
everything into a single catch-all cluster.

### 4. The dominant source overlaps everything — which explains the MMR result

Hacker News (360 articles, the most of any source) does not occupy its own
region of content space:

| | |
|---|---|
| HN centroid → all-other-sources centroid | 0.175 |
| HN's own spread | 0.205 |

**The separation is smaller than the spread** — the two distributions sit on
top of one another. This is the picture behind the separately-measured
negative result that MMR on content similarity left source concentration
unchanged (effective sources 3.1 → 3.1). Content-based diversity works by
pushing apart points that sit close together, and HN's articles are already
spread across the whole space. It dominates by publishing *frequently*, not
*repetitively*, and no similarity function can see the difference.

### 5. Four kinds of thing end up in a "cluster" — only one is wanted

Inspecting all 40 multi-article clusters at TF-IDF 0.40:

- **Genuine cross-source corroboration** — Apple's camera-equipped AirPods
  across Engadget, TechCrunch and HN; Google buying Spirit Airlines' data
  across GNews and HN; Sonos lock-screen controls across Engadget and
  TechRadar. *This is the signal.*
- **Aggregator echoes** — an HN entry linking to a BBC article carries BBC's
  exact headline. One article counted twice, inflating the source that
  already dominates.
- **Same-source series** — OpenAI's blog posting "Sales workflows with
  ChatGPT Work", "Data science workflows with ChatGPT Work", and so on.
  Lexically near-identical, editorially four different posts. A false
  positive.
- **Literal duplicates** — 14 groups, 19 redundant copies, mostly recurring
  RSS programme slots ("Tech Now", "Tech Life", "Business Daily") that reuse
  one title for every episode. Invisible to `news_cache`'s link-hash dedup
  because the links differ.

**Any corroboration count must collapse aggregator echoes onto their origin
first**, or it will systematically over-credit the most over-represented
source.

## Reproducing this

See [README.md](README.md). Three commands, and only the first needs the VM:

```
python analysis/tools/fetch_cache_snapshot.py --host ubuntu@<ip> --key <key>
python analysis/tools/cluster_news.py
python analysis/tools/build_cluster_report.py
```

All computation is local — scikit-learn only, no API calls, no LLM calls,
nothing to pay for. The full pairwise similarity matrix over 2,082 articles
takes about a second and peaks around 36 MB.

## Caveats

- **One snapshot, one 48-hour window.** Everything here could look different
  during a major news event, when corroboration would spike. Worth re-running
  on a busy day before treating these numbers as typical.
- **t-SNE layout is not metric.** Distances between distant points in the
  scatter carry no meaning; only local neighbourhoods do. The centroid and
  spread figures in finding 4 are computed in that projected space and are
  best read as *comparative* (separation vs spread), not absolute.
- **Titles and short summaries only.** The cache does not hold full article
  text, so clustering sees roughly a headline plus up to 300 characters.
  Full text would likely find more corroboration.
