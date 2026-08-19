# Cross-Domain Survey: Over-Concentrated Samples

How other fields handle "my sample is dominated by one source." Written
as a companion to `news-ranking-plan.md`, after the 2026-08-18
source-collapse finding (Hacker News = 33% of the article cache but 64%
of its newest 50) showed this project has no defense against it at all.

A Chinese translation is kept at `sample-diversity-survey.zh.md`. Both are
maintained together.

**This is a survey, not a recommendation.** Its purpose is to show which
solution shapes exist and which of them this project could actually use.
The concrete options for this codebase live in `news-ranking-plan.md`.

## The organizing observation

The problem is genuinely universal — survey research, ecology, finance,
genomics, clinical trials, astronomy, information retrieval, and machine
learning all have a well-developed literature on it. But they don't solve
the *same* problem. They intervene at **four different points in the
pipeline**, and which point you pick determines what you can still
recover afterwards.

| Where | Question it answers | Fields that live here |
|---|---|---|
| **1. Design** | How do I collect a balanced sample in the first place? | Survey statistics, clinical trials, astronomy |
| **2. Measurement** | How concentrated is what I have, in a single number? | Ecology, finance, media studies |
| **3. Selection** | Given a skewed pool, how do I pick a balanced subset? | Information retrieval, recsys, ML, search engines |
| **4. Correction** | Given a skewed sample I can't re-collect, how do I still get unbiased answers? | Survey weighting, astronomy, genomics |

**This project currently does none of the four.** That's the finding, and
it's why the survey is worth having before picking a fix.

---

## 1. Design time — prevent the imbalance

### Stratified sampling (statistics)

Divide the population into mutually exclusive strata, then sample
randomly *within* each stratum. Guarantees every stratum is represented
regardless of its natural frequency. The probability-sampling gold
standard, because it supports formal statistical inference.

**Quota sampling** is its non-probability cousin: same subgroup structure,
but selection within each quota is by availability rather than random.
Much cheaper and faster; the non-random selection within quotas is
exactly where bias re-enters. The standard guidance is to use quota
sampling when speed and cost matter more than precision, and stratified
sampling when results must support formal inference.

**Maps to this project as**: a per-source (or per-source-class) quota in
the digest — "at most N from any one source." Directly analogous, and
cheap.

### Block randomization and minimization (clinical trials)

Trials face a sharper version: assignments arrive *sequentially*, and you
can't wait for the full sample before balancing.

- **Permuted block randomization** — randomize within small fixed-size
  blocks so the desired allocation is achieved exactly within each block.
- **Stratified block randomization** — blocks within each covariate stratum.
- **Minimization** — a *dynamic* procedure: for each arriving subject,
  compute which assignment would minimize overall imbalance across the
  covariates you care about, and assign that. Notably, minimization is
  reported to have real advantages **specifically when the sample is
  small**, where simple randomization's balance guarantees are weak.

**Why this is the most under-appreciated analogue for us**: the ingestion
job is exactly a sequential-arrival problem. Articles arrive per cycle;
we decide what to keep without seeing the future. Minimization's core
idea — "greedily pick whatever most reduces current imbalance" — is
directly implementable and is a *better* fit than static quotas for a
stream.

### Volume-limited samples (astronomy)

Rather than correct for a bias, **restrict the sample so the bias cannot
occur**. Flux-limited surveys over-represent intrinsically bright objects
(Malmquist bias, below); a volume-limited sample takes everything within
a fixed distance instead, accepting a smaller sample for an unbiased one.

**The transferable idea**: sometimes the clean fix is to *narrow the
inclusion rule* until the distortion is structurally impossible, and pay
for it in sample size. For us that would look like "only consider
articles from the last N hours, from every source equally" — smaller
pool, no frequency-domination.

---

## 2. Measurement time — quantify the concentration

You cannot manage what you don't measure, and this project currently has
no concentration metric at all. Two fields independently converged on the
*same mathematics* here, which is worth knowing.

### Herfindahl-Hirschman Index and "effective N" (economics/finance)

HHI = Σ(wᵢ)², the sum of squared shares. Used for market concentration
(antitrust) and portfolio concentration risk. Its inverse, **1/HHI, is
the "effective number of holdings"** — a portfolio of 100 stocks where
one holds 90% has an effective N near 1, not 100.

Known limitation worth carrying over: **HHI ignores correlation between
holdings.** Two "different" sources that syndicate the same wire copy are
not two sources, and HHI would score them as if they were.

> **This caveat was confirmed in our own data on 2026-08-19**, one day
> after this survey was written — while measuring TF-IDF similarity over
> the real cache, the two highest-scoring "cross-source" pairs turned out
> to be Hacker News entries linking to BBC articles, carrying BBC's exact
> headline. That is one article counted twice, not two outlets
> corroborating each other, and it inflates precisely the source already
> over-represented. 16 titles in the cache appear more than once for this
> reason. See `news-ranking-plan.md`'s "aggregators aren't corroboration"
> section. The abstract caveat became a concrete requirement: collapse
> aggregator entries onto their origin *before* counting anything.

### Diversity indices and Hill numbers (ecology)

- **Shannon index** — accounts for both richness and evenness; sensitive
  to rare species.
- **Simpson's index** — dominance-weighted; better when what you care
  about is whether one species dominates.
- **Evenness / rank-abundance curves** — separates "how many kinds" from
  "how equally distributed."
- **Hill numbers** — a unified family that expresses diversity as an
  **effective number of species**.

**The convergence worth noticing**: ecology's Hill number of order 2 is
mathematically the inverse Simpson index, which is **the same quantity as
finance's 1/HHI**. Two fields, no contact, same answer. That is a decent
signal that "effective number of sources" is the right metric for us,
rather than something invented ad hoc.

### Rarefaction and species accumulation curves (ecology)

A different and important question: **have I sampled enough?** Rarefaction
standardizes comparisons across unequal sampling effort; accumulation
curves show whether adding more sampling still finds new species, or has
plateaued.

**Maps to this project as**: a genuinely useful diagnostic we don't have —
"is our 21-source registry actually surfacing distinct stories, or have we
plateaued and are just re-collecting the same events?" Both indices are
noted as sensitive to sample size, which is exactly why rarefaction exists
and why raw counts across differently-sized sources would mislead.

---

## 3. Selection time — pick a diverse subset from a skewed pool

This is the family that most directly matches our situation: the pool is
already collected and already skewed; the question is what to *put in the
digest*.

### Maximal Marginal Relevance (information retrieval)

The classic. Greedily select the item maximizing a weighted combination
of **relevance to the query** and **dissimilarity to what's already
selected**. An item scores well only if it's both relevant *and* not
redundant.

**Its most important property for us is the explicit λ knob** trading
relevance against diversity. That trade-off is unavoidable — MMR's
contribution is making it a visible parameter instead of an accident.

### Determinantal Point Processes (recsys)

A probabilistic formulation: maximize the determinant of a kernel matrix
of item similarities. Geometrically, the determinant is the volume
spanned by the selected items' vectors — maximizing it selects items that
"spread out" in feature space. More principled than MMR's greedy
heuristic, and correspondingly heavier.

Both MMR and DPP are **post-processing re-rankers**: they reorder a
model's output rather than changing the model. That separation is
attractive here — it means diversity can be added without touching the
existing selection logic.

### Submodular maximization / facility location (machine learning)

The general theory underneath. Diversity objectives (facility location,
k-center, DPP, coreset selection) are largely **submodular** — they have
diminishing returns, where each additional item adds less than the last.
The key result: maximizing a monotone submodular function under a
cardinality constraint is NP-hard, **but a simple greedy algorithm gets
within (1 − 1/e) ≈ 63% of optimal**, with a proof.

**Why this matters practically**: it means the obvious greedy approach —
repeatedly take whatever adds most — is not a hack. It has a worst-case
guarantee. For a project this size, that's permission to implement the
simple thing and know it's defensible.

### Host crowding (search engines, production practice)

Google's long-standing answer is blunt and worth respecting for that:
**cap results per domain** — for most queries, up to about two listings
per site, relaxed when the query itself indicates interest in a specific
domain.

**This is the most directly transferable solution in the entire survey.**
It's the same problem (one publisher dominating a result set), solved by
the crudest possible mechanism, in one of the largest production ranking
systems in the world. The nuance worth copying is the escape hatch: the
cap relaxes when concentration is *what the user actually wants*.

---

## 4. Correction time — accept the skew, correct afterwards

For when you cannot re-collect, and cannot re-select.

### Post-stratification, raking, inverse-probability weighting (survey research)

Weight the collected sample so its composition matches known population
margins. This is the "we can't fix collection, so we'll fix the math"
approach.

### Malmquist bias correction (astronomy)

The canonical example of a selection effect: in a flux-limited survey,
intrinsically luminous objects are over-represented because they remain
detectable across a much larger volume. First described in 1924, and
correctable — given distances, a geometric correction for the relative
volume in which an object of a given true luminosity could have been
detected.

**The conceptual gift here is the reframing**: the over-representation is
*not an error in the data*. Every observation is real and correct. The
distortion is that the *detection probability* varies with the property
you're measuring. Applied to us: HN's dominance isn't wrong data, it's a
detection-rate artifact — HN is "brighter" (publishes more often) so it
fills the recency window, exactly as luminous galaxies fill a flux-limited
survey.

### Batch effects and population stratification (genomics)

Aggregating samples across sources produces **batch effects** that can
create spurious findings — false population structure, bad imputation,
spurious mutation calls. Standard mitigations are covariate adjustment
(principal components) and harmonization pipelines like ComBat.

**The honest caveat, straight from that literature**: correction is not
complete. When structure is recent or sharply distributed, it **cannot be
fully corrected by any method** — principal components on common variants
are simply uninformative about it. Worth carrying over as realism: a
post-hoc correction is strictly weaker than not creating the imbalance.

---

## Three cross-cutting lessons

**1. Diversity always trades against relevance, so make it a parameter,
not an accident.** MMR's λ, the block size in block randomization, the
domain cap in host crowding — every mature solution exposes the trade-off
explicitly. The failure mode in this project was that the trade-off was
being made *implicitly* (recency-only ordering happened to imply "no
diversity"), so nobody chose it.

**2. Concentration is sometimes real signal, not bias.** Malmquist bias
is the sharpest statement of this: the bright objects genuinely are
bright. Google's host-crowding escape hatch says the same thing
operationally — when the query really is about one site, showing one site
is correct. **A naive hard cap discards real information.** If HN
genuinely carries more tech news than the BBC does, forcing them to equal
shares makes the digest worse, not better.

**3. Where you intervene bounds what you can recover.** Design-time fixes
(stratification, volume-limiting) prevent the problem. Selection-time
fixes (MMR, host crowding) work on whatever you collected. Correction-time
fixes (weighting, ComBat) are the weakest and the genomics literature is
explicit that they're incomplete. Prefer intervening early — but note that
for us, *ingestion is already collected and cached*, so selection-time is
the realistic tier, with design-time (per-source fetch quotas) as the
upstream option.

---

## What actually maps onto this project

| Method | Field | Applicable here? | Relates to |
|---|---|---|---|
| **Host crowding (cap per source)** | Search engines | **Yes — highest value per unit effort.** Crude, proven at scale, ~10 lines | `news-ranking-plan.md` Option A |
| **Effective N (1/HHI, Hill numbers)** | Finance / ecology | **Yes — as a metric, not a fix.** Log the digest's effective source count per push; makes the problem visible and tunable | Nothing yet — a genuine gap |
| **MMR** | Information retrieval | **Yes** — needs an item-similarity function, which is exactly what Option E (embeddings) would provide | Options B + E |
| **Minimization** | Clinical trials | **Yes, and under-appreciated** — ingestion is a sequential-arrival problem, which is what minimization is built for | Could improve ingestion-side balance |
| **Stratified / quota sampling** | Survey statistics | **Yes** — a per-source-class quota at fetch time (`forum`/`api`/`rss`) | Upstream variant of Option A |
| **Rarefaction / accumulation curves** | Ecology | **Yes, as a diagnostic** — "are 21 sources actually adding distinct stories, or have we plateaued?" | Nothing yet |
| **Submodular greedy guarantee** | ML | **Indirectly** — justifies implementing the simple greedy version and knowing it's within (1−1/e) of optimal | Underpins Option B |
| **DPP** | Recsys | Probably over-engineered at this scale, but the right thing if MMR proves too crude | — |
| **Post-stratification / weighting** | Survey research | **No** — we control selection directly, so correcting after is strictly worse than selecting better | — |
| **ComBat / PC adjustment** | Genomics | **No** — no equivalent latent batch structure to regress out | — |
| **Malmquist correction** | Astronomy | **No as a method, yes as a mental model** — reframes HN dominance as a detection-rate artifact rather than bad data | Framing for B |

### The gap this survey actually exposes

Of the four intervention points, the one this project is missing most
cheaply is **measurement**. There is no metric anywhere in the codebase
for how concentrated a digest is. Effective-N is one line of arithmetic
over the source counts we already have, and without it, any fix chosen
from `news-ranking-plan.md` will be evaluated by eyeballing digests —
which is precisely the failure mode that let source collapse run
undetected until a human noticed.

**Suggested reading of this survey**: measurement first (it's nearly free
and makes everything else evaluable), then host crowding as the stopgap,
then MMR once embeddings exist.

## Sources

**Selection / re-ranking**
- [*Result Diversification in Search and Recommendation: A Survey*](https://arxiv.org/pdf/2212.14464)
- [*SMMR: Sampling-Based MMR Reranking*](https://dl.acm.org/doi/10.1145/3726302.3730250) (SIGIR 2025)
- [*Personalized Re-ranking for Improving Diversity in Live Recommender Systems*](https://dlp-kdd.github.io/dlp-kdd2020/assets/pdf/a8-wang.pdf) (KDD)
- [*apricot: Submodular selection for data summarization in Python*](https://arxiv.org/pdf/1906.03543) and [*Coresets for Data-efficient Training*](https://cs.stanford.edu/people/jure/pubs/craig-icml20.pdf) (ICML)
- [*A Coreset Selection of Coreset Selection Literature*](https://arxiv.org/html/2505.17799v1)
- Google host crowding / site diversity: [SEroundtable](https://www.seroundtable.com/google-search-domain-diversity-update-27696.html), [Site Diversity System](https://clicksgorilla.com/blog/site-diversity-system-how-google-prevents-overrepresentation-in-search-results)

**Design / sampling**
- [Stratified sampling](https://en.wikipedia.org/wiki/Stratified_sampling) and [Quota sampling](https://en.wikipedia.org/wiki/Quota_sampling)
- [*Techniques for randomization and allocation for clinical trials*](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC11758574/)
- [*Minimization in randomized clinical trials*](https://onlinelibrary.wiley.com/doi/10.1002/sim.9916) (Statistics in Medicine, 2023)
- [*How to Balance Prognostic Factors: Stratified Permuted Block Randomization or Minimization?*](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC11202503/)

**Measurement**
- [*Concentration indicators*](https://www.bis.org/ifc/events/6ifcconf/avilaetal.pdf) (Bank for International Settlements)
- [*Generalized Herfindahl-Hirschman Index to Estimate Diversity Score of a Portfolio*](https://dvararesearch.com/wp-content/uploads/2023/12/Generalized-HHI-to-Estimate-Diversity-Score-of-a-Portfolio.pdf)
- [*Community assessment techniques and the implications for rarefaction and extrapolation with Hill numbers*](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC5743490/)
- [Measuring Biodiversity](https://bio.libretexts.org/Courses/CT_State_Northwestern/General_Ecology_Ecology/Chapter_22%3A_Biodiversity/22.5%3A_Measuring_Biodiveristy) (Biology LibreTexts)

**Correction**
- [Malmquist bias](https://www.oxfordreference.com/view/10.1093/oi/authority.20110803100129765) (Oxford Reference) and [*Selection effects in correlated observations*](https://arxiv.org/html/2607.22425v1)
- [*What's in a Survey? Simulation-Induced Selection Effects in Astronomy*](https://link.springer.com/chapter/10.1007/978-3-031-26618-8_12)
- [*A data harmonization pipeline to leverage external controls and boost power in GWAS*](https://pmc.ncbi.nlm.nih.gov/articles/PMC8825237/)
- [*Demographic history mediates the effect of stratification on polygenic scores*](https://elifesciences.org/articles/61548) (eLife)
- [*Who's (Not) Afraid of the Batch Effect Boogeyman?*](https://gatk.broadinstitute.org/hc/en-us/articles/18440923786907-Who-s-Not-Afraid-of-the-Batch-Effect-Boogeyman) (Broad Institute / GATK)

**Media diversity**
- [*Echo chambers, filter bubbles, and polarisation: a literature review*](https://reutersinstitute.politics.ox.ac.uk/echo-chambers-filter-bubbles-and-polarisation-literature-review) (Reuters Institute)
- [*Understanding Echo Chambers and Filter Bubbles*](https://misq.umn.edu/misq/article/44/4/1619/1818/Understanding-Echo-Chambers-and-Filter-Bubbles-The) (MIS Quarterly)
