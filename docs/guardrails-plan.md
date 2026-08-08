# Guardrails Plan

Goal: stop the agent from answering off-topic questions or discussing its
own implementation/system prompt, without adding infrastructure this
project's tiny free-tier deployment can't carry. Nothing here is built
yet — this doc captures the incident, the research behind the design, and
the plan before implementation starts, same pattern as the other `docs/*
-plan.md` files.

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
| 1 | Fast pre-filter for obviously-bad input | Planned, not built |
| 2 | Cheap secondary classifier ("gateway") before the main agent runs | Planned, not built |
| 3 | Hardened, scope-confined `SYSTEM_PROMPT` | Planned, not built |
| 4 | Output-side check before replying (added based on research, not in the original ask) | Planned, not built |

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

## Open questions

- Exact wording/pattern list for layer 1 — needs to be built and iterated
  on, not fully speculated here.
- Whether layers 2 and 4 share one prompt/function or need separate ones
  (input framing vs. output framing are different enough that separate
  prompts may classify better — not decided).
- Cost/latency impact of the extra DeepSeek call(s) per message — not
  measured yet; worth checking after building, especially since layer 2
  runs on *every* message, not just suspicious ones.
- Whether false positives (a legitimate news question getting redirected)
  are a real problem in practice — only observable once this is live and
  used for a while.
