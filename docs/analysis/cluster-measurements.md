# Cluster Measurements — the real cache, 2026-08-19

The numbers behind `news-ranking-plan.md`'s Option B. Everything here was
measured on a live snapshot of the production cache, not estimated.

**📊 Interactive report with both scatter plots:**
[cluster-report.html](cluster-report.html) — open it locally, or view the
published copy at
<https://claude.ai/code/artifact/c8b4c66f-ce7d-480f-80b6-d6bc3bc49ef7>.

The HTML is **generated, never hand-edited** — regenerate it any time with
`python docs/analysis/tools/build_cluster_report.py` (see [README](README.md)).
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
python docs/analysis/tools/fetch_cache_snapshot.py --host ubuntu@<ip> --key <key>
python docs/analysis/tools/cluster_news.py
python docs/analysis/tools/build_cluster_report.py
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

---

# Follow-up, 2026-08-19: clustering the *filtered* set, and can clusters replace the 13 categories?

Two questions raised after seeing the scatter plot, plus an outage found
while answering them.

## First: the 13 categories are not working at all

Before either question could be answered honestly, this had to be
established. Of 2,262 cached articles, **only 163 (7.2%) carry any
category**. The other 92.8% are uncategorized — and
`news_push.select_candidate_articles` excludes uncategorized articles
whenever the subscriber's topic has real categories, so most of the cache
is invisible to most subscribers.

Cause, found by grouping cached articles by the ingestion tick that wrote
them:

| Batch size | Categorized |
|---|---|
| 1 | 100% |
| 60 | 95% |
| 109 | 96% |
| **113** | **0%** |
| 139 / 147 / 156 / 171 | **0%** |
| 281 | **0%** |
| 1085 | **0%** |

All-or-nothing, with a cliff just above 110. `news_classify` made **one
call per ingestion cycle**, which was fine while a cycle produced ~100
articles and stopped being fine when `local-news-cache-plan.md` item 7
raised the RSS per-source cap from 5 to 200 and cycles began producing
100–1000+. The likely mechanism is the structured-output response
exceeding the model's output token limit — one entry per article, so
response length scales with batch size.

It ran for three days without a trace, because `classify_articles` caught
every exception and returned `{}` with no logging. **Fixed**: batches are
chunked at `MAX_ARTICLES_PER_CALL = 50`, a failure now prints, and a
failed chunk costs one chunk instead of the whole cycle.

A second, compounding failure in the same area: several cached
interest→category mappings are empty (`AI` → `[]`, `robotics` → `[]`,
`Bitcoin` → `[]`, `機器人科技` → `[]`). An empty mapping is treated as
unrestricted by design, so those subscribers match *every* article, while
subscribers whose interests did map correctly match almost none.

## Q1 — What do the filtered results cluster into?

Traced for a real subscriber (interests: semiconductors, quantum
computing, robotics):

| Interest | Mapped to | Candidates |
|---|---|---|
| semiconductors | `Hardware` | 32 of 2,262 |
| quantum computing | `Research`, `Hardware` | 42 of 2,262 |
| robotics | *(empty mapping)* | **2,262 — no filtering at all** |

Clustering the 32 `Hardware` articles gives **31 clusters: 30 singletons
and one pair**. There is nothing to cluster.

**The honest answer to Q1 is that the filtered set can't be meaningfully
clustered in its current state** — not because clustering doesn't work,
but because the filter is either returning ~1% of the cache or 100% of it.
Worth re-running once classification is repaired and the cache has
refilled with properly categorized articles.

## Q2 — Could the visual clusters replace the 13 categories?

The intuition is well-founded. Formalizing the hand-circled clumps with
HDBSCAN over the t-SNE layout finds **~80 clusters covering 89% of
articles**, and they are far more coherent than the hand-written
categories when measured back in the original TF-IDF space:

| | Median coherence | vs random baseline |
|---|---|---|
| Discovered clusters | 0.0869 | **9×** |
| The 13 categories | 0.0241 | 3× |

They also read as genuinely specific topics — `quantum computing` (47
articles), `apple / camera / airpods` (30), `bitcoin / price / btc` (68),
`google / pixel` (50), `data center` (41), `supply chain` (18) — where the
hand-written set offers only `Hardware` or `Finance`. Two categories,
`Hardware` and `Policy`, score at **1× baseline**: articles labelled with
them are no more similar to each other than two random articles.

**But they cannot be categories, for one decisive reason: they are not
stable.** Re-running the identical pipeline on the identical data with
only the random seed changed:

| | Run A | Run B |
|---|---|---|
| Clusters | 77 | 84 |
| Noise points | 255 | 213 |
| **Adjusted Rand Index between the two groupings** | &nbsp; | **0.478** |

Less than half the grouping survives a change of seed *on the same data*.
Real data changes every four hours. A category has to be a stable
vocabulary that a stored user interest maps onto once and keeps matching;
"cluster 36" means something different on the next run, so there is
nothing durable to store a preference against.

Two further caveats: several clusters are **single-source artifacts** (one
outlet's beat, not a topic — the 68-article bitcoin cluster is one
source), and the tail includes incoherent grab-bags (a 94-article cluster
whose top terms are `null, v0, earth, dream, transformer`).

### Conclusion

**Not a replacement — a complement, at a different layer.**

- **Categories** stay the stable, coarse vocabulary that a *stored user
  interest* matches against. Their job is durability across time, and
  clusters cannot do it. What they need is to actually run (see the outage
  above) and probably a better taxonomy — `Hardware` and `Policy` earning
  1× baseline is evidence the current 13 are partly arbitrary.
- **Clusters** are valuable *within a single push cycle*, where stability
  across runs is irrelevant: grouping the current candidate set by story
  for de-duplication, for source diversity, and for structuring the digest
  itself. That is exactly what `news-ranking-plan.md`'s Option B needs, and
  the 9× coherence says the signal is real.

The measurement that would change this conclusion: if cluster assignments
proved stable across *consecutive real ingestion cycles* (not just seeds),
the durability objection weakens. Worth testing before dismissing the idea
permanently — the coherence numbers are strong enough to justify it.

---

# Follow-up, 2026-08-19: picking an embedding backend, and whether it fits the VM

Everything above used TF-IDF as a stand-in for "embeddings", because the
project had no embedding model. That stand-in is what produced the two
headline failures — word-attractor clusters, and a degenerate distance
metric where 1,382 of 2,257 candidate articles tied at exactly zero
distance from the selected set. So before designing anything on top of
clusters, the question is which backend to actually use.

Measured with `docs/analysis/tools/bench_embeddings.py` on the same 2,262
article snapshot. Each backend runs in its **own process** — RSS is
process-wide and `del` + `gc.collect()` does not return pages to the OS,
so measuring them in one process credits whichever runs last with
everything before it. The first version of that script did exactly that
and reported fastembed at +1,077 MB.

## The numbers

| backend | dim | disk | RSS peak | docs/s | separation | med max-sim | exact zeros |
|---|---|---|---|---|---|---|---|
| TF-IDF (incumbent) | 4494 | 0 MB | **9 MB** | 43281 | **−0.101** | 0.0000 | 1382/2257 |
| model2vec potion-base-8M | 256 | 8 MB | **80 MB** | 22145 | +0.155 | 0.2349 | 6/2257 |
| fastembed bge-small (ONNX) | 384 | 54 MB | 855 MB | 8 | +0.195 | 0.5853 | 0/2257 |
| sentence-transformers MiniLM-L6 | 384 | 634 MB | 416 MB | 129 | **+0.299** | 0.1752 | 11/2257 |

**"Separation"** is the gap between the lowest-scoring pair that *should*
match and the highest-scoring pair that shouldn't, across four probes.
Absolute cosine values are not comparable across backends — each model
family has its own similarity floor, and BGE scores everything high — so
the gap is the comparable quantity.

**TF-IDF's separation is negative.** Its false positive (two articles
sharing only the word "google", 0.101) outscores a true match (same topic,
different vocabulary, 0.000). On this task TF-IDF is not merely weak, it
is anti-correlated. That is the single most useful number here: it retires
the "maybe TF-IDF is good enough" option outright.

## Does it fit the bot VM?

Measured on the live box: `VM.Standard.E2.1.Micro`, 954 MB total, **420 MB
available** with `myfirstagent-bot` already resident at 184 MB, 1 OCPU,
x86_64, 33 GB free disk, 1 GB swap of which 240 MB is already in use.

| backend | verdict |
|---|---|
| model2vec | **fits** — 80 MB, 340 MB headroom |
| sentence-transformers | **does not fit in practice** — 416 MB peak against 420 MB available is 4 MB of headroom, plus 634 MB added to the Docker image |
| fastembed | **does not fit** — over by 435 MB, and 8 docs/s makes it moot anyway |

Disk is not the constraint. Peak RSS is.

## Frozen-taxonomy quality on dense vectors

Rerun of the frozen-taxonomy experiment (build clusters on the older 60%,
assign the unseen newer 40%) with each backend:

| backend | clusters | coverage, all | coverage, pruned | median best-match vs random |
|---|---|---|---|---|
| TF-IDF | 55 | 82.5% | **43.3%** | 0.216 vs 0.006 |
| model2vec | 35 | 89.4% | **80.8%** | 0.446 vs 0.186 |
| sentence-transformers | 36 | 86.0% | **75.8%** | 0.449 vs 0.143 |

The pruning column is the important one. TF-IDF needed aggressive pruning
because half its clusters were word-attractors, and pruning cost it 39
points of coverage while contamination survived anyway. Dense vectors
produce fewer junk clusters to begin with (35–36 rather than 55, worst
coherence 1.5× baseline rather than near-random), so pruning costs 9–10
points instead of 39.

The specific failure case is fixed in both: "EmbeddingGemma, Google's new
efficient embedding model" no longer lands in a Spirit Airlines cluster.

At tight thresholds sentence-transformers pulls ahead (32.6% vs 20.2% at
sim ≥ 0.50), which is consistent with its better separation. model2vec
buys ~85% of the useful behaviour for 19% of the RAM, no torch dependency,
and 87× faster encoding (0.2 s vs 17.4 s for the full corpus).

## Reproducing

```powershell
conda activate myfirstagent
python docs/analysis/tools/bench_embeddings.py
```

## Correction: peak-RSS was measuring the wrong workload

The table above measured peak RSS while batch-encoding all 2,262 articles.
That is not the production workload. The intended split is:

1. Build the taxonomy **offline on a dev machine** — encode the corpus,
   cluster, prune. Memory is free here.
2. Store the resulting centroids. A 35-cluster taxonomy is **54 KB**
   (35 × 384 float32) — trivially a DB row or a checked-in file.
3. On the VM, encode **each incoming article** and take the nearest
   centroid. That is a 384-vector against a 35 × 384 matrix.

Step 3 is genuinely light *in work*. It is not light *in residency*, and
that distinction is what actually decides the backend: the model has to be
loaded in RAM to encode anything at all, and that fixed cost does not
shrink with batch size. Re-measured with the two costs separated:

| backend | FIXED (import + load) | MARGINAL (all encoding) | total resident | per article | separation |
|---|---|---|---|---|---|
| model2vec | **84 MB** (1.1 s) | 7 MB | **91 MB** | **0.2 ms** | +0.155 |
| fastembed bge-small | **152 MB** (0.5 s) | 20 MB | **172 MB** | 96–300 ms | +0.195 |
| sentence-transformers | **398 MB** (14.6 s) | 90 MB | **488 MB** | 11–21 ms | +0.299 |

Against the VM's 420 MB available: model2vec leaves 329 MB, fastembed
leaves 248 MB, sentence-transformers overruns by 68 MB — and would overrun
on the fixed cost alone if anything else on the box moved.

**The earlier fastembed number was wrong.** It was recorded at 855 MB peak
and 8 docs/s, and both figures were artifacts of `TextEmbedding.embed()`
forking parallel workers by default. Pinned to `threads=1, parallel=0`, its
fixed cost is 152 MB — a factor of 5.6 lower, and the difference between
"disqualified" and "viable". Worth flagging as a measurement lesson: a
default that spawns workers will misreport both memory and throughput on a
single-core target.

fastembed is nonetheless the **slowest** per article of the three, ONNX and
int8 notwithstanding — 96–300 ms against sentence-transformers' 11–21 ms,
because single-threaded int8 gets no benefit from the BLAS paths torch
uses. That is tolerable for a background ingestion job (200 articles ≈ 19 s
here, more on 1 OCPU) and irrelevant to user-facing latency, but it rules
fastembed out of anything synchronous.

**Two viable candidates**, then: model2vec (cheapest, fastest, weakest
separation) and fastembed (2× the memory, ~1000× the latency, better
separation). sentence-transformers stays the offline quality reference and
cannot be deployed to this VM.

---

# Rough taxonomy from embeddings, both candidates, 2026-08-19

Built with `docs/analysis/tools/build_taxonomy.py` on the same 2,262
article snapshot. The pipeline is the three-step design: encode and
cluster offline, keep the centroids, classify new articles against them at
runtime with no LLM call.

Cluster labels come from **c-TF-IDF** — pool each cluster's documents into
one pseudo-document, then TF-IDF across clusters, so terms frequent *in* a
cluster but rare *across* clusters win. Plain centroid-nearest-terms gave
labels like "ai, new, hn" because it just surfaces whatever is globally
common.

## The finding that unblocked fastembed: anisotropy

Run as-is, fastembed produced **2 clusters and 2 noise points from 2,262
articles** — one undifferentiated blob. The cause is visible in one number:
its **random-pair cosine baseline is 0.502**. Two unrelated articles are
already 50% similar, so a density-based clusterer has no contrast to grip.
model2vec's baseline is 0.120.

This is the well-known anisotropy of BERT-family embeddings — they occupy a
narrow cone rather than filling the sphere. Subtracting the corpus mean
before normalizing fixes it completely: baseline 0.502 → 0.001, and 2 raw
clusters → 75.

**Centering is not optional for either backend, and it must use the build
set's mean, not the whole corpus's.** Getting that wrong is subtle and was
a real bug here: centering the build subset by a mean computed over a
superset leaves a residual offset that re-introduces the anisotropy, and it
collapsed the model2vec holdout run from 40 clusters to 2. It also matters
practically — at build time only the build set exists. The mean is stored
with the centroids and must be applied to every article classified later,
or runtime vectors land in a different space than the taxonomy.

## The derived categories

Both produce plausible topic sets, but with a consistent difference in
grain. Selected, by size:

| model2vec (36 kept) | fastembed (38 kept) |
|---|---|
| bitcoin, btc, price | bitcoin, price |
| chatgpt, chatgpt work, codex | openai, safety, frontier |
| cyber, cybersecurity | codex, openai, gpt |
| unitree, robot, humanoid | unitree, humanoid, robot |
| hugging face, inference providers | hugging face, face inference |
| apple, iphone, airpods | **gpu, kernels, cuda** |
| stocks, chinese, hong kong | **transformers, ocr, transformers js** |
| battery, batteries, nuclear, ev | **trl, batching, continuous batching** |
| trump, canada, tariffs | **lerobot v0, agents** |
| blockchain, sec | **embedding, sentence, supervised** |
| climate, planes, warming | **leaderboard, evaluation, asr** |
| headphones, garmin, trackers | trump, canada, tariffs |

**fastembed resolves finer technical distinctions** — CUDA kernels,
continuous batching, ASR evaluation, late-interaction embeddings are all
separate topics for it, where model2vec keeps them inside broader
"ai"/"coding" clusters. For a technology-news product that grain is worth
something.

Both keep a legitimate "google, spirit airlines" cluster. That is a real
story (Google bought Spirit's data at a bankruptcy auction), not the
word-attractor failure TF-IDF had — the earlier problem was *EmbeddingGemma*
landing in it, and neither dense backend does that.

**Coverage is the honest weak point: only 25–30% of articles fall into any
kept cluster.** HDBSCAN leaves 670–873 as noise and the coherence prune
removes about half the rest. A taxonomy that classifies a quarter of the
corpus is not yet a replacement for the 13 LLM categories; it is a
high-precision signal over part of the feed.

## Absorbing unseen articles (build on older 60%, classify newer 40%)

| | model2vec | fastembed |
|---|---|---|
| raw clusters → kept | 40 → 20 | 40 → 20 |
| build-set coverage | 28% | 23% |
| absorbed at sim ≥ 0.30 | 40.9% | **58.8%** |
| absorbed at sim ≥ 0.40, margin ≥ 0.02 | 20.9% | **33.0%** |
| median best-match | 0.265 | **0.336** |
| vs a random cluster | −0.010 | −0.003 |

fastembed absorbs unseen articles substantially better — 58.8% against
40.9% at the loose threshold, and a higher median match throughout. Both
score ~0 against a random cluster, so the matches are real.

## Hot news via fine clusters

Second pass inside each rough cluster, single-linkage at cosine ≥ 0.75 —
a rough cluster is a *topic*, a fine cluster should be one *event*.

This works, and the standout is the same story for both backends:

| story | model2vec | fastembed |
|---|---|---|
| **Unitree humanoid-robot IPO** | 11 articles, 3 sources | **15 articles, 4 sources** |
| Strategy / Bitcoin purchases | 4 articles, 1 source | 5 articles, 1 source |
| Hugging Face inference providers | 2 articles, 1 source | 5 articles, 1 source |
| Apple camera AirPods leak | **5 articles, 4 sources** | not surfaced |
| Claude text watermarking | not surfaced | **3 articles, 3 sources** |
| ChatGPT for Teens | 4 articles, 3 sources | — |

The Unitree IPO is exactly the intended signal: 15 articles across gnews,
BBC, Guardian and Nikkei, all covering one event, on a day it was genuinely
the biggest story in the feed. Cluster size plus **source count** is the
usable metric — size alone rewards a single outlet publishing a series, as
with the four `openai_blog` "ChatGPT Work" posts, which are not hot news.

fastembed found the larger cross-source groups; model2vec found more groups
overall (13 vs 7 on the holdout build) but skewed single-source.

## Where this leaves the two candidates

| | model2vec | fastembed |
|---|---|---|
| resident memory | **91 MB** | 172 MB |
| per article | **0.2 ms** | 96–300 ms |
| category grain | broad | **finer, more technical** |
| absorption of unseen | 40.9% | **58.8%** |
| cross-source hot news | weaker | **better** |

Both fit the VM. fastembed is better at the job on every quality axis
measured; model2vec is roughly 500x cheaper per article and half the
memory. Neither is disqualified, and the choice is a product call about
whether the finer grain justifies the cost.

## Reproducing

```powershell
conda activate myfirstagent
python docs/analysis/tools/build_taxonomy.py --backend model2vec --center --save
python docs/analysis/tools/build_taxonomy.py --backend fastembed --center --save
python docs/analysis/tools/build_taxonomy.py --backend fastembed --center --holdout
```

`--save` writes the centroids, the stored mean, and the coherence cutoff to
`docs/analysis/data/`. That file is what a runtime classifier would load;
it is ~50-100 KB.
