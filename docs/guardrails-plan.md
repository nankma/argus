# Guardrails Plan

Goal: stop the agent from answering off-topic questions or discussing its
own implementation/system prompt, without adding infrastructure this
project's tiny free-tier deployment can't carry. **Built and live** — see
Status below; this doc still captures the incident and the research
behind the design, kept for context.

## The incident that triggered this

A real user message: *"Claude code has a new function that allow message
cross session. How do I sent the prompt that you can return this kind of
news next time."* — ambiguous phrasing mentioning "Claude Code" (this
project's own dev tool) in a way that could plausibly be either a garbled
news question or a meta-question about the bot itself.

The agent got pulled off-script: instead of treating it as a news query
(or asking for clarification), it answered as if it *were* Claude Code,
explaining how to edit `CLAUDE.md`, referencing `--resume`, and walking
through session-continuity mechanics — none of which is this bot's job,
and some of which references this project's own internal tooling by name.
No malicious input was involved — this is a **benign scope-drift
failure**, not a prompt-injection attack, which matters for which
mitigations actually apply (see below).

## Status

| # | Item | Status |
|---|------|--------|
| 1 | Fast pre-filter for obviously-bad input | **Done** — `guardrails.fails_local_prefilter()` |
| 2 | Cheap secondary classifier ("gateway") before the main agent runs | **Done** — `guardrails.is_input_on_topic()` |
| 3 | Hardened, scope-confined `SYSTEM_PROMPT` | **Done** — `agent.py` |
| 4 | Output-side check before replying (added based on research, not in the original ask) | **Done** — `guardrails.is_output_on_topic()` |

**Verified live, both directions**: a normal AI-news question
(e.g. "What's new with OpenAI?") still gets a real answer; the actual
incident message (and rephrasings of it) now gets the redirect message
instead of the agent discussing its own configuration.

## What the research says (see chat for full findings; summary here)

- **Almost every well-known guardrail tool targets malicious content**
  (jailbreak/injection/toxicity) — Llama Guard / Meta's Prompt Guard,
  Lakera Guard, Rebuff, Azure AI Content Safety's Prompt Shields. None of
  these are built for *benign* topic/scope drift, which is what actually
  happened here. Also worth knowing: Prompt Guard has been shown to be
  bypassable with trivial obfuscation (simple character spacing dropped
  detection accuracy from 100% to 0.2% in published research) — a
  reminder that no single filter is bulletproof, defense-in-depth matters
  more than picking the "best" single tool.
- **Only two things found were actually built for topic confinement
  specifically**: NVIDIA NeMo Guardrails' `Llama-3.1-NemoGuard-8B-TopicControl`
  (a model fine-tuned to output on-topic/off-topic against a policy) and
  OpenAI's newer Guardrails Python library's "Off Topic Prompts" check.
  Both are real signal that "is this on-topic?" is commonly handled as its
  **own dedicated classification step**, not folded into the main model's
  system prompt alone.
- **NeMo's TopicControl NIM is not a fit for this project's scale** — it's
  an NVIDIA-hosted/GPU-oriented microservice, meaningfully heavier
  infrastructure than anything else this project runs (compare: the whole
  point of `combined_bot.py` and the Oracle Always Free deployment has
  been fitting everything into 1GB VMs). Noting it exists, not adopting it.
- **The "cheap model screens, expensive path only runs if needed" pattern
  is well-established**, not something being improvised here — Anthropic's
  Constitutional Classifiers use exactly this two-stage cascade (cheap
  first-stage classifier on every exchange, expensive second-stage only on
  flagged ones); the academic root is Stanford's FrugalGPT (cascade
  routing cut cost up to 98% in their benchmarks). This directly validates
  item 2 below — it's not a novel idea, it's the standard shape.
- **Prompt-only defense is proven insufficient on its own** — one cited
  study found output filtering alone stopped 100% of leak attempts while
  prompt hardening alone did not; OWASP's 2025 Top 10 for LLM apps gave
  "System Prompt Leakage" its own category (LLM07) specifically because
  prompt instructions alone don't reliably prevent it. This is why item 4
  (output-side check) got added to this plan even though it wasn't in the
  original three-item ask — the research is unambiguous that input-side
  hardening alone (items 1-3) leaves a real gap.

## Design

Four layers, each catching what the previous one might miss — matches
this project's existing "no single point of failure" instinct (e.g. the
Docker+iptables two-firewall-layer setup for Phoenix, or the
security-list+VCN-CIDR restriction on the OTLP port).

### 1. Fast pre-filter (deterministic, no LLM call)

A local, zero-cost check before anything reaches the LLM at all — regex/
keyword matching for the clearest cases: instruction-override phrasing
("ignore previous instructions", "you are now", "pretend you are"),
direct requests to reveal the system prompt/instructions/configuration,
and self-referential mentions of this project's own tooling ("Claude
Code", "CLAUDE.md", "system prompt"). Catches the obvious cases for
free, instantly, before spending any DeepSeek tokens — genuinely
malicious/obvious attempts rarely need an LLM call to catch.

**Not adopting Guardrails AI or Rebuff as a dependency for this layer** —
both are real, credible libraries, but what they'd actually provide here
is pattern/regex-based validation, which is straightforward to hand-roll
as a small module without pulling in either framework's broader
dependency tree (Guardrails AI in particular is a substantial package).
Revisit this decision if the hand-rolled version becomes hard to
maintain — the frameworks remain a reasonable fallback if pattern lists
grow unwieldy.

### 2. Cheap secondary classifier — the "gateway" (item 2 from the ask)

**Planned evolution, not built yet**: `is_input_on_topic`'s plain boolean
is becoming a richer router — see `docs/context-management-plan.md`'s
"router design" section. The same classification call that decides
on-topic/off-topic will also return a `category` (news query vs. setting
an interest vs. toggling proactive push, etc.), feeding directly into
which instructions/tool the main agent reaches for that turn — one call
doing double duty instead of stacking a separate intent-classification
call on top. This section describes the *current*, still-boolean-only
behavior; update once the router ships.

A second, separate DeepSeek call — not a different/smaller model, per the
user's own framing ("now just the same one") — with a tightly-scoped
prompt whose only job is answering one question: *is this message a
legitimate request for AI-industry news/trends, yes or no?* Runs after
layer 1 (only for input that passed the free check) and before the main
agent's tool-calling loop starts. This is the cascade/tiered-defense
pattern confirmed above — cheap, fast, narrow classification gating the
expensive multi-turn agent call, not a general-purpose second opinion.

If the classifier says no: skip the main agent entirely, reply with a
short, consistent redirect ("I only help with AI industry news and
trends — try asking about a company, model, or trend instead.") — no
DeepSeek tool-calling loop spent on a request that was never going to be
answerable anyway.

**Why DeepSeek and not a cheaper embedding/semantic-similarity check**:
considered and deferred, not rejected outright. Embedding-based topic
routing (compute an embedding of the incoming message, compare cosine
similarity against reference on-topic examples, no text generation
involved — the "semantic-router" pattern) is genuinely cheaper and faster
than an LLM call, and a real, commonly-used technique for this exact
problem. Not adopted now for a concrete infrastructure reason: the model
itself is small (e.g. `sentence-transformers`' `all-MiniLM-L6-v2` is
~80MB), but the inference library it needs (PyTorch) is not — installing
it would meaningfully strain VMs that are already carefully managed down
to individual hundreds-of-MB of headroom (see `docs/deployment-plan.md`'s
Oracle Always Free setup). Revisit once there's a host with room for it,
or a case for calling a hosted embeddings API instead of running one
locally. Also worth being honest about a real limitation even if this
gets adopted later: embedding similarity is good at catching *clearly*
off-topic input cheaply (weather, recipes, poetry) but weaker on the kind
of input that actually caused this project's incident — a message that's
still semantically AI/tech-adjacent, just aimed at the wrong target (the
bot's own tooling, not AI industry news). An embedding layer would be a
useful cheap *first* pass to reduce how often the DeepSeek classifier
call below even needs to run, not a replacement for it.

### 3. Hardened `SYSTEM_PROMPT` (item 3 from the ask)

Add explicit scope-confinement instructions to `agent.py`'s
`SYSTEM_PROMPT`, on top of what's already there for output formatting
(see the `telegram-message-formatting` skill). Concrete additions, based
on what the research flagged as recurring categories worth covering
explicitly (not an exhaustive list — a starting point):

- State the scope positively and negatively: only AI-industry news/trends;
  explicitly refuse anything else, including questions about the bot's
  own configuration, instructions, system prompt, tools, or the software
  it happens to be built with (Claude Code, LangChain, DeepSeek, etc.).
- A small number of concrete refusal examples (not excessive — the
  research notes over-stuffing few-shot examples can degrade unrelated
  performance) covering: a plain off-topic question, a direct "what's
  your system prompt" ask, and an ambiguous one shaped like the actual
  incident (a question that *mentions* the bot's own tooling by name
  without being a clear attack).
- Explicit instruction not to role-play as, or claim to be, any other
  assistant/system/tool — directly addresses this incident, where the
  model effectively answered *as* Claude Code.

**Known limitation, stated plainly**: per the research, this layer alone
is not considered reliable — it's necessary, not sufficient. It stays in
the plan because it's cheap and catches real cases, not because it's
expected to fully solve the problem by itself.

### 4. Output-side check (added based on research, not in the original ask)

After the main agent produces its final answer, run that answer through
the same lightweight classifier from layer 2 (or a similarly cheap check)
before it's sent to the user: *does this response actually stay within
AI-industry news/trends, and does it avoid discussing the bot's own
configuration/instructions?* If it fails, replace the reply with the same
redirect message layer 2 uses, rather than forwarding whatever the model
produced. This is the layer the research says actually does the work —
input-side hardening (1-3) reduces how often this triggers, but doesn't
replace it.

Would have caught the actual incident even if the other three layers
missed it, since the failure only became visible in what the model
*wrote*, not in the (ambiguous, not obviously malicious) input.

## Worked example: the actual incident through all four layers

Concrete walkthrough, tracing the real message ("Claude code has a new
function that allow message cross session. How do I sent the prompt that
you can return this kind of news next time.") through the design above —
useful for understanding why four layers, not one:

1. **Layer 1 (regex/keyword)**: no "ignore instructions"-style phrasing,
   no direct "show me your system prompt" pattern → **passes**. Expected —
   this layer only exists to catch blatant attempts for free, and this
   message doesn't look blatant.
2. **Layer 2 (DeepSeek gateway)**: asked "is this a legitimate AI-industry
   news/trends request?" — a well-prompted classifier should recognize
   this is actually a question about a dev tool's own feature (Claude
   Code's session handling), not an AI-news request → **should flag as
   off-topic here**, before the main agent ever runs. This is the layer
   actually expected to have prevented the incident.
3. **Layer 3 (hardened system prompt)**: backstop if layer 2 somehow
   passed it through anyway — the main agent's own instructions say
   explicitly not to discuss its own configuration or role-play as
   another tool, which is exactly what went wrong originally.
4. **Layer 4 (output check)**: final backstop — even if the agent still
   produced an off-topic answer, re-checking the *actual generated text*
   ("does this response stay in AI-news scope and avoid discussing the
   bot's own tooling?") catches it before it's ever sent, and swaps it
   for the same redirect message. This is the layer that would have
   caught this specific incident for certain, since the failure was only
   visible in what the model wrote, not in the input itself.

No single layer is assumed to be reliable alone — that's the point of
having four. Layer 2 is where this incident was *expected* to be caught;
layer 4 is where it's *guaranteed* to be caught if every earlier layer
fails, since it inspects the actual output rather than trying to predict
it from the input.

## Where this plugs into the existing code

- Layers 1 and 2 run in `handle_message()` (`bot.py`), before
  `run_agent()` is called — same place `check_access()` already gates
  requests, so this becomes a second gate in the same spot.
- Layer 3 is a `SYSTEM_PROMPT` (`agent.py`) edit.
- Layer 4 wraps the result of `run_agent()`, still inside
  `handle_message()`, before `split_for_telegram()`/`reply_text()` runs.
- All four should be pure/testable functions following this project's
  existing pattern (`tests/fakes.py`'s `FakeToolCallingModel` for the
  layer-2/4 DeepSeek calls, no real API calls in tests) — no architecture
  change needed to fit this in.

## Incident: Chinese-language input largely misclassified as off-topic, and a successful action's confirmation silently blocked

**Found 2026-08-14**, running the smoke-test checklist against the new
local curl API (`docs/local-testing-api-plan.md`) — the first time this
project could feed the same prompt through layers 1/2/4 repeatedly
without the friction of real Telegram round-trips, which is exactly what
surfaced this. Two distinct, real findings, not one.

### Finding 1 — layer 2 (the router) misclassifies Chinese-language requests as off-topic

Six trials, four different Chinese phrasings, five different chat_ids,
all through the real deployed router:

| # | Text | Intent | Result |
|---|---|---|---|
| 1 | 我對機器人科技很感興趣，請加入我的追蹤主題 | add interest | `off_topic` |
| 2 | (same, re-run) | add interest | `off_topic` |
| 3 | (same, re-run again) | add interest | `news_query` — still wrong category, at least on-topic |
| 4 | 請把機器人科技加入我的興趣 (shorter) | add interest | `off_topic` |
| 5 | 我想追蹤機器人科技的新聞 (matches a real subscriber's previously-successful phrasing — see the SHioufen trace investigation earlier this project) | add interest | `off_topic` |
| 6 | 機器人科技最近有什麼新聞 (plain news query, no interest-setting at all) | news query | `off_topic` |
| **control** | "What is happening with robotics lately?" (same topic, English) | news query | `news_query` — correct, full report, every time |

**5 of 6 Chinese trials failed. The English control on the identical
topic passed every time.** Not phrasing-specific (four different
phrasings all failed), not topic-specific (the working English control
was the same topic), not `set_interest`-specific (a plain news query in
Chinese failed too). This isolates to Chinese-language input generally,
against `_ROUTER_PROMPT`'s current classification.

**Why this matters more than a typical reliability gap:** this bot has
real Chinese-speaking subscribers today — stored interests like
`機器人科技`/`科技財經` and a `language` preference of Traditional Chinese
are already in the live database (see the SHioufen investigation
earlier this project). This finding means those subscribers are likely
being silently rejected most of the time they write in Chinese, not a
theoretical edge case.

### Finding 2 — a successful action's confirmation gets blocked, but the action itself silently succeeds anyway

`set_language` → "Traditional Chinese": layer 2 correctly classified it
as `set_language`, the agent correctly ran and called the `set_language`
tool, and the database was actually updated — confirmed directly against
the live container:

```
sqlite3 /data/subscribers.db "SELECT chat_id, language FROM subscribers WHERE chat_id = 990"
→ (990, 'Traditional Chinese')
```

But layer 4 (`is_output_on_topic`) blocked the *confirmation reply* and
swapped it for the generic redirect message ("I only help with tech
industry news..."). The user sees a message that reads like the bot
didn't understand the request at all, with no indication that their
request actually succeeded. Circumstantial confirmation this really did
take effect silently: the very next message on the same chat_id (a plain
English Tesla query) came back in Traditional Chinese unprompted, which
only happens if the stored preference took hold.

This is a different failure shape from Finding 1 — not a rejection of a
legitimate request, but a **successful state change paired with a
misleading denial message**, which is arguably worse from a user's
perspective: nothing tells them what actually happened.

### Not fixed yet, deliberately

Both findings touch the guardrail/router prompts — the same live,
carefully-tuned logic this doc's own "Unrun experiment" section below
already flags as needing a proper measurement harness before changing,
not a guessed fix. This project has hit "the obvious fix made it worse"
twice already (the layer-4 narrowing attempt, and the Markdown-leak
follow-up) — a third guess without measurement isn't the move here.
**Deliberately left open pending the harness described below**, not
forgotten.

### What this changes about the harness's priority and scope

Two concrete decisions that came out of discussing this incident,
recorded here so they aren't lost before the harness is actually built:

1. **The harness needs to test layers 1 and 2 standalone, not only
   bundled into an end-to-end run.** `docs/model-portability-plan.md`'s
   "The harness" section already proposed a repeatable N-trial scorer,
   but scoped primarily around the output check (layer 4). This incident
   makes the case concretely: `fails_local_prefilter` (layer 1) and
   `classify_message` (layer 2) both need to be testable by feeding
   hundreds of prompts directly at just that function, with no agent
   run, no output check, no Telegram round-trip in the loop at all. That
   changes both what gets measured first (the router's Chinese-language
   reliability is now the most urgent open question, ahead of the
   already-known layer-4 causal question) and how cheap each trial is
   (a single classifier call is far cheaper to run at volume than a full
   agent turn).
2. **Settings actions (interests/language/push) should be dispatched by
   a separate agent from the research/news agent**, specifically *so*
   they can be tested in isolation without the research agent's behavior
   being a confound. This directly reinforces
   `docs/context-management-plan.md`'s already-documented "Planned
   refactor: dispatch settings routes out of the agent" section — that
   refactor was previously justified only by efficiency (Route B pays
   for an agent loop it doesn't need); this incident adds a second,
   independent justification: **testability**. Finding 2 above is a
   `set_language` failure specifically, and having settings handling
   live in the same agent as news research makes it harder to isolate
   whether a fix for one risks regressing the other. Recorded in that
   doc's open questions, not duplicated here.

## Unrun experiment: what actually made the output check reliable

**Status: not run.** Identified 2026-08-09 while writing up the layer-4
measurements for `docs/system-overview.md` Appendix B.1.

**Background.** Four versions of the layer-4 output check were measured
against the real model. Scores on "does it catch a self-disclosure leak":

| # | Prompt structure | Caught leaks |
|---|---|---|
| 1 | Stepped prompt (ordered checks, one yes/no answer) | 2/3 *(3-trial spot-check)* |
| 2 | Stepped prompt, reworded | 15/15 |
| 3 | Compact prompt, no steps | **1/15** |
| 4 | Structured output, one boolean field per condition | 15/15 |

**The problem with concluding anything.** Version 4 changed **three
things at once** relative to version 3:

1. the output format (free text → structured fields),
2. where precedence between the two conditions is decided (model → code),
3. whether the two conditions are explicitly separated at all.

So the improvement can't be attributed to any single one. Version 3's
collapse is equally ambiguous: it dropped the stepped structure, but its
prompt was also mostly *negative space* — a short "flag this" followed by
a long "this does NOT include…" carve-out, with no contrasting example of
an acceptable reply. The exclusion may simply have dominated, independent
of the missing steps.

The doc currently states only the operational conclusion (the change was
measured, the plausible option was rejected on evidence) and explicitly
flags the causal claim as untested. That's honest but unsatisfying — if
"use structured output for classifier-style checks" is going to be a
reusable rule, it should rest on something better than one confounded
comparison.

**Proposed experiment.** Hold everything constant except one variable at
a time, same test cases and same trial count throughout:

| Variant | Structure | Output format | Isolates |
|---|---|---|---|
| A | Stepped | Free text yes/no | baseline (= version 2) |
| B | Stepped | Structured fields | effect of *output format* alone |
| C | Compact, no steps | Free text yes/no | baseline (= version 3) |
| D | Compact, no steps | Structured fields | effect of *structure* alone (= version 4) |

Comparing A↔B and C↔D isolates output format; comparing A↔C and B↔D
isolates prompt structure. A fifth variant adding a positive contrast
case ("here is what an acceptable reply looks like") to version 3's
wording would test the negative-space hypothesis separately.

**Why it's worth doing at some point:** the "move the logic that must be
correct out of the prompt and into code" principle is the most reusable
idea to come out of this project, and it's currently supported by a
confounded result. Either the experiment confirms it, or it reveals the
real mechanism was something else — both outcomes are more useful than
the current state.

**Prerequisite:** the measurement harness described in
`docs/model-portability-plan.md`. That plan needs a repeatable N-trial
scorer anyway, because guardrail reliability figures are properties of a
prompt/model *pair* and must be re-measured on any model swap. Once that
harness exists, this experiment is a matter of running it over four
prompt variants rather than building anything new — which is why this is
sequenced after it rather than before.

## Open questions

- ~~Exact wording/pattern list for layer 1~~ — built in `guardrails.py`'s
  `_SUSPICIOUS_PATTERNS`; expect to keep iterating on this list as new
  false-negative phrasings show up in practice.
- ~~Whether layers 2 and 4 share one prompt~~ — **decided: separate
  prompts** (`_INPUT_SCOPE_PROMPT` / `_OUTPUT_SCOPE_PROMPT` in
  `guardrails.py`), sharing one `_classify()` helper. Input framing asks
  "is this a legitimate request"; output framing asks "does this text
  stay in scope and avoid self-disclosure" — different enough questions
  that reusing one prompt for both seemed likely to classify worse.
- Cost/latency impact of the extra DeepSeek call(s) per message — partly
  answered. **There is no cheaper DeepSeek model to move the classifier
  calls to** — DeepSeek's current lineup is `deepseek-v4-flash` (cheap)
  and `deepseek-v4-pro` (expensive: ~50x pricier on cache hits, ~3x on
  cache misses, per DeepSeek's pricing page), and this project's
  `deepseek-chat` identifier is already aliased to `v4-flash` — confirmed
  directly from a real Phoenix trace during this session's testing
  (`model_name: deepseek-v4-flash`). So layers 2/4 aren't running on a
  discounted model; they're on the same one as the main agent. The actual
  savings this design gets isn't a cheaper-model discount — it's that an
  off-topic message never reaches the main agent's multi-turn tool-calling
  loop (`search_news` calls, synthesis, etc.) at all, and a single short
  classification completion is far cheaper than that whole loop even on
  the same model. A genuine "cheaper model for the gate" would require
  going back to the deferred local-embedding option (no DeepSeek call at
  all for the gate) rather than a same-provider model swap — there isn't
  one available. Actual token/latency numbers still not measured.
- Whether false positives (a legitimate news question getting redirected)
  are a real problem in practice — not observed in initial live testing
  (a normal question still got a real answer), but that's one data point,
  not a stress test.
