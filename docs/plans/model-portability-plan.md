# Model Portability Plan (LLM Gateway / Dynamic Model Switching)

Nothing here is built yet — this doc captures the goal, what's already
possible, and the decisions to make before implementing, same pattern as
`docs/plans/deployment-plan.md` and `docs/plans/multi-channel-plan.md`.

Cross-references to "Appendix B.1" below mean
`docs/system-overview.md` Appendix B.1 (Problems hit in development and
production).

**The question that prompted it:** the architecture has no AI gateway.
Can we switch the LLM model dynamically, and do we need one?

**Short answer: yes, and no.** Yes we can switch models — LangChain
provides the mechanisms natively and the codebase's dependency-injection
design already accommodates it. No, we don't need a separate gateway
service for that; a gateway would mostly add operational features
(budget caps, caching, multi-provider key management) that don't earn
their cost at current scale.

## Status

| # | Item | Status |
|---|------|--------|
| 1 | Config-driven model selection | **Built 2026-08-16** — `agent.build_model()`, `LLM_MODEL` env var, see below |
| 2 | Per-stage model routing (cheap model for classifiers) | **Built 2026-08-16 as plumbing** — `LLM_MODEL_CLASSIFIER` env var; both default to the same model today since no second provider is configured yet, so production behavior is unchanged until one is set |
| 3 | Provider failover | Not built — deferred until availability is a real requirement |
| 4 | Dedicated AI gateway (LiteLLM / OpenRouter / Cloudflare) | **Evaluated, not recommended at current scale** |
| 5 | Re-validating guardrail reliability after any model change | **Blocking prerequisite** for 1–3 — see "The behavioral caveat". A fresh baseline was measured 2026-08-16 against the newly-wired config, see below |

## Where things stand today

Model choice is hardcoded in two ways:

- `agent.py` defines `MODEL = "deepseek-chat"`.
- `ChatDeepSeek(model=MODEL)` is constructed in three places —
  `agent.main()`, `bot.main()`, and `combined_bot.main()`.

Everything downstream is already provider-agnostic. These all take the
model as a parameter rather than constructing one:

| Consumer | Signature |
|---|---|
| Agent construction | `build_agent(model)` |
| Stage 1 router | `classify_message(model, user_message)` |
| Stage 3 output check | `is_output_on_topic(model, response_text, category)` |
| Push digest writer | `write_push_digest(model, articles, language)` |
| Push cycle | `run_push_cycle(model, send, now)` |

That injection point exists because of testability (it's what lets the
suite run against a scripted fake — see
`docs/plans/telemetry-and-testing-plan.md`), but it happens to give most of what
a gateway would provide: **the ability to substitute the model without
touching consumer code.** The gap is only that construction is pinned to
one provider class.

## Verified capabilities

Tested against the installed `langchain==1.3.14`, constructing real
objects (no API calls):

| Capability | Result |
|---|---|
| `init_chat_model("deepseek:deepseek-chat")` | Works — returns `ChatDeepSeek`, `model_name == "deepseek-chat"`. Provider becomes a **string**, so it can come from config. |
| `init_chat_model(configurable_fields=("model", "model_provider"))` | Works — returns `_ConfigurableModel`, allowing the model to be chosen **per invocation** via runtime config. |
| `Runnable.with_fallbacks([...])` | Works — returns `RunnableWithFallbacks`. Automatic failover to a secondary model. |
| `configurable_alternatives` / `configurable_fields` on `RunnableSerializable` | Present. (Not on the base `Runnable` — confirmed, so target the right class if used.) |

## Built 2026-08-16 — Levels 1 and 2, plus a fresh baseline

`agent.build_model(env_var, default=DEFAULT_MODEL)` wraps
`langchain.chat_models.init_chat_model(os.environ.get(env_var, default))`.
`agent.main()` calls it once (`LLM_MODEL`); `bot.py`/`combined_bot.py`'s
`main()` call it twice -- `LLM_MODEL` for the agent, `LLM_MODEL_CLASSIFIER`
for `guard_model` (Stages 1/3, the router and output check). Both default
to `DEFAULT_MODEL = "deepseek:deepseek-chat"`, so this is pure plumbing
today, not a behavior change -- no second provider is configured yet (see
"Open questions" below, still open). `tools/measure_guardrails.py`
deliberately keeps its own hardcoded `ChatDeepSeek(model="deepseek-chat")`
rather than reading these env vars -- the harness needs to pin a known
model for reproducible scoring runs, independent of whatever the live app
happens to be configured with at the time.

**Fresh baseline, measured against this code path** (all layers,
`tools/measure_guardrails.py`, 2026-08-16):

| Layer | Result |
|---|---|
| 1 (`fails_local_prefilter`) | 8/8 (100%) |
| 2 (`classify_message`, the router) | 190/190 (100%), 10 trials/case |
| 4 (`is_output_on_topic`, the output check) | 158/160 (99%), 20 trials/case |

Consistent with (slightly better than) the last recorded layer-4 numbers
in `docs/plans/guardrails-plan.md` (85%/100% on `set_language`, 92% on
self-disclosure) -- confirms the config-driven wiring is behavior-neutral,
as expected since both env vars still resolve to the same model.

**A real bug found and fixed while establishing this baseline, not
hypothetical**: the first `--layer 4` run crashed with `AttributeError:
'NoneType' object has no attribute 'discusses_own_configuration'`.
`classify_message`/`is_output_on_topic` both fail open on an *exception*
from `structured.invoke(...)`, but a `None` return (no exception at all --
seen once in ~160 trials, hit specifically on this run) wasn't guarded.
Since neither call is wrapped in a try/except at the `bot.py`
`process_message` call site, this could have crashed real message
handling in production, not just the harness. Fixed in `guardrails.py` --
both functions now also return their fail-open default on a `None`
result, with regression tests in `tests/test_guardrails.py`.

**Before pointing `LLM_MODEL_CLASSIFIER` (or `LLM_MODEL`) at a different
model**, re-run `python tools/measure_guardrails.py --layer 2` and
`--layer 4` against it and compare to the table above -- this is what "The
behavioral caveat" below means in practice, made concrete rather than left
as prose.

## Level 1 — Config-driven model selection

Replace the three hardcoded constructions with a single string read from
configuration:

```python
# instead of: ChatDeepSeek(model=MODEL)
model = init_chat_model(os.environ.get("LLM_MODEL", "deepseek:deepseek-chat"))
```

**Effect:** switching provider or model becomes an environment-variable
change plus a restart — no code change, no rebuild.

**Cost:** roughly ten lines across three files. The target provider's
LangChain package must be installed (e.g. `langchain-openai`), and its API
key must be added to OCI Vault following the existing secrets pattern
(`docs/plans/security-plan.md` finding 2) — no new secrets design needed.

**Recommended.** Low cost, and it makes the "which model?" decision
reversible instead of baked in.

## Level 2 — Per-stage model routing

The most valuable item, and nearly free, because **the architecture
already separates the calls.**

The three-stage pipeline (`docs/system-overview.md` §B2) makes three
distinct kinds of LLM call, and each already receives its model as a
separate argument:

| Stage | Work | Model requirement |
|---|---|---|
| Stage 1 — Classify | Short structured classification | Small/fast is sufficient; must support structured output |
| Stage 2 — Act | Synthesis, tool use, prose | Benefits most from a stronger model |
| Stage 3 — Verify | Short structured classification | Small/fast is sufficient; must support structured output |

Today all three receive the *same* instance, but nothing requires that.
Passing a cheaper model to the two classifier stages and reserving a
stronger one for synthesis is a **wiring change in `main()`, not a
refactor** — the seam is already in place.

**Why it matters:** the classifiers run on *every* message, including
rejected ones. They're the highest-volume calls in the system and the
least demanding. This is where cost optimization actually lands.

**Prerequisite:** any model used for Stages 1 or 3 must support
structured output, since both depend on it (and per Appendix B.1 of the overview, structured output is what made
the Stage 3 check reliable in the first place).

## Level 3 — Provider failover

`.with_fallbacks([secondary_model])` gives automatic retry against
another provider when the primary fails or rate-limits.

**Deferred, with two caveats to resolve first:**

1. The fallback model must support **both tool calling and structured
   output**, or Stages 1–3 and the agent loop will fail on the fallback
   path. Not all models do; this needs verifying per candidate, not
   assuming.
2. Failover needs testing against `create_agent`, since the agent wraps
   the model — the fallback behavior through that wrapper is unverified.

Worth building when provider availability becomes a real operational
concern. It isn't yet — there's been no observed DeepSeek outage
affecting the service.

## Level 4 — A dedicated AI gateway

Evaluated and **not recommended at current scale.**

| Option | What it adds beyond Levels 1–3 | Cost here |
|---|---|---|
| **LiteLLM (self-hosted)** | Budget caps, caching, unified multi-provider routing, per-key quotas | Another process competing for the same **1 GB** — directly against principle P5. The single-process topology exists precisely to avoid loading a second runtime (overview, Appendix B.1) |
| **OpenRouter / Portkey (hosted)** | Same, without local memory cost; one key for many providers | Adds a network hop to every call, a third-party dependency in the critical path, and (for some) per-token markup |
| **Cloudflare AI Gateway** | Caching, analytics, rate limiting; generous free tier | Still an extra hop; overlaps with Logfire, which already provides full trace fidelity |

The distinctive benefits of a gateway are **budget enforcement, response
caching, and managing many providers at once**. None are pressing:
spending is bounded by approval-gated access, the workload is
poorly-suited to caching (news changes constantly), and there's one
provider.

**Revisit when:** open signup makes hard budget caps necessary, or more
than two providers are in play simultaneously.

## The behavioral caveat (blocking prerequisite)

Switching models is mechanically easy. It is **not behaviorally free.**

The guardrail reliability figures recorded in `docs/system-overview.md`
Appendix B.1 of the overview — the structured-output check scoring 15/15, and the narrow-prompt
variant scoring 1/15 — were measured **against DeepSeek specifically**.
They are properties of a prompt/model pair, not of the prompt alone.

Therefore, before any model change reaches production:

1. Re-run the N-trial measurement for Stage 1 (classification accuracy)
   and Stage 3 (self-disclosure detection, false-positive rate) against
   the new model.
2. Re-run the 13-case post-deploy checklist, which covers output
   formatting — the Markdown-vs-HTML compliance behavior in Appendix B.1 is also
   model-specific.

This is the same discipline Appendix B.1 argues for, applied to a config change
rather than a prompt change. **A model swap is a behavioral change and
must be measured like one.** Cheap models in particular are likelier to
be weaker at instruction-following, which is exactly what the guardrails
depend on — so Level 2's cost saving must be validated, not assumed.

## Suggested order

1. **Level 1** — config-driven selection. Small, reversible, unblocks
   everything else.
2. **Measurement harness** — a repeatable script for the guardrail
   reliability measurements, so validating a candidate model is a command
   rather than a manual exercise. This should exist *before* Level 2, not
   after. See "The harness" below.
3. **Level 2** — per-stage routing, with the cheap model validated by
   that harness.
4. **Level 3** — failover, when availability warrants it.
5. **Level 4** — only on the triggers above.

## The harness

Both Level 2 and any future model change depend on the same missing
tool: a repeatable way to score a prompt/model pair.

**What it needs to do:** take a set of labelled test cases (input →
expected verdict), run each N times against a named model, and report
per-case and aggregate scores. That's the same procedure already used
manually to produce the figures in Appendix B.1 of the overview — the
harness just makes it a command instead
of an afternoon.

**Why it's the real prerequisite.** The guardrail figures (15/15, and the
1/15 that got a change rejected) are properties of a prompt *and* a model
together, not of the prompt alone. Any model swap therefore invalidates
them until re-measured. Without a harness, that re-measurement is
expensive enough that it will get skipped — which converts Level 1's
"switching is easy" into a way to silently break a safety control.

**Second use.** `docs/plans/guardrails-plan.md` records an unrun experiment to
determine *why* structured output outperformed the text prompts — the
current result is confounded across three simultaneous changes. That
experiment is four prompt variants scored the same way, so it needs
exactly this harness and nothing else. Worth noting because it makes the
harness pay for itself twice.

**Third use — built, 2026-08-14, `tools/measure_guardrails.py`.** An
ad-hoc 6-trial manual test (via the new local curl API,
`docs/reference/local-testing-api-plan.md`) seemed to show the router rejecting
Chinese-language requests as off-topic in 5 of 6 trials against a
100%-passing English control. Building the harness to properly measure
it found the opposite: `classify_message` scored **140/140 (100%)**
across 14 cases run directly, no HTTP, no tunnel. The original 6-trial
result turned out to be an SSH tunnel corrupting requests in transit,
not a router problem at all — see
`docs/plans/guardrails-plan.md`'s incident write-up for the full comparison
across four different call paths that isolated this. **The harness is
what caught its own trigger case as a false positive** — the concrete
proof that ad-hoc manual trials (what every guardrail measurement in
this project was, before this) aren't good enough to trust on their own,
independent of whatever the specific finding turns out to be. Layers 1
(`fails_local_prefilter`), 2 (`classify_message`), and now 4
(`is_output_on_topic`, extended 2026-08-14, motivated by the one finding
from this incident that *did* survive re-verification — see below) are
all scoreable standalone — hundreds of prompts fed directly at just that
function, no agent run, no Telegram round-trip anywhere in the loop. The
harness has since caught and fixed a real layer-4 issue this way (the
`set_language` reasoning-before-conclusion fix, `docs/plans/guardrails-plan.md`)
and also caught its own false positive a second time (the retracted
"Chinese-language crypto" incident, same doc) — proof this extension
pulled its weight, not just a planned-but-unused capability.

**Fourth implication.** Discussing this incident surfaced a related
design point, recorded in `docs/plans/context-management-plan.md`'s "Planned
refactor: dispatch settings routes out of the agent" section: settings
actions (interests/language/push) dispatched by a separate agent from
news research would make it possible to test and fix one without risking
a regression in the other — a second, independent justification for that
refactor beyond the original efficiency argument. Grounded in the one
finding from this incident that held up: a `set_language` confirmation
being wrongly blocked by layer 4 while the underlying state change
silently succeeded, reproduced 1/5 with zero tunnel involvement.

**Cost note:** the harness makes real API calls by definition, so it
shouldn't run on every commit. A manually-triggered job, run when the
model or a guardrail prompt changes, is the right cadence. Layers 1/2
standalone runs are far cheaper per trial than a full agent turn (one
classifier call vs. a whole pipeline), so "hundreds of prompts" is
realistic for those two specifically in a way it wouldn't be for
layer 4 or a full end-to-end run.

## Open questions

- Which second provider? Choice should be driven by structured-output and
  tool-calling support, not headline price — a cheaper model that can't
  do structured output is unusable for Stages 1 and 3.
- Should the classifier model be configurable *separately* from the agent
  model (two env vars), or should routing be a single named "profile"
  (e.g. `LLM_PROFILE=economy|balanced`)? The profile approach is less
  flexible but harder to misconfigure into an unsafe combination.
- Does the measurement harness belong in CI? It costs real API calls, so
  probably not on every commit — likely a manually-triggered job.
