# Context Management Plan

Goal: replace the current monolithic `SYSTEM_PROMPT` (one long string, sent
in full on every single LLM call regardless of relevance) with a layered,
conditionally-assembled prompt — tighter core identity, situational
content only included when actually needed, per-user memory injected
consistently instead of the current ad hoc string-prepending. Nothing here
is built yet — this doc captures the design and the research behind it
before implementation starts, same pattern as the other `docs/*-plan.md`
files.

## The problem with today's `SYSTEM_PROMPT`

One string, defined once in `agent.py`, passed to `create_agent(...,
system_prompt=SYSTEM_PROMPT)` at agent-construction time, resent in full
on *every* `ChatDeepSeek` call the agent makes — confirmed directly from a
real Phoenix trace this session: a single user turn produced two separate
`ChatDeepSeek` LLM spans (one before the `search_news` tool call, one
after), each carrying the complete system prompt again. It currently
mixes three things that don't belong at the same level:

1. Core identity ("you are a technology industry analyst," scope
   confinement, anti-role-play rules) — this should basically never change
   and should always be present.
2. Telegram HTML formatting rules (`<b>`, `<a href>`, escaping, structure
   template) — only relevant *because* the output happens to go to
   Telegram. Irrelevant token weight on every call regardless of what the
   turn actually needs.
3. Tool-usage notes (when to use `save_note`) and the interests-injection
   instruction — situational, not identity.

Per-user data (interests, set via `/interests`) is currently injected by
string-prepending a `[User's stated interests: ...]` note onto the raw
user message in `bot.py` — works, but is an ad hoc mechanism distinct from
how everything else reaches the model, and doesn't generalize cleanly to
more per-user preferences (e.g. "prefers deep analysis over brief
summaries," format customization) without more of the same string-hacking.

## The proposed four layers

Priority order, highest to lowest — higher layers should win in conflicts
with lower ones:

1. **System prompt** — tight. Identity + non-negotiable behavioral rules
   only (scope confinement, anti-self-disclosure, anti-role-play). Always
   present, on every call.
2. **Subsystem/workflow/state prompt** — situational. Telegram formatting
   rules, tool-usage notes, "the user seems to be trying to set a
   preference in natural language," translation instructions if/when that
   feature exists. **Only included when relevant to the current turn** —
   not sent at all otherwise.
3. **Long-term user memory prompt** — per-user persistent data (interests,
   depth-of-analysis preference, custom formatting, once those exist).
   Injected consistently for every call from that user, not per-feature
   string hacks.
4. **Current user message** — the actual turn's input. Lowest priority —
   nothing in the user's own message should be able to override rules
   from layers 1-3.

## What the research says

- **This is a real, named pattern, not a novel idea.** Two related
  framings converge on the same shape: OpenAI's Model Spec defines an
  explicit conflict-resolution order (Root > System > Developer > User >
  Tool/Data, higher wins) — directly matches this doc's 1→4 priority
  ordering. The broader industry term for *what content goes in the
  prompt, from where, assembled how* is **"context engineering"** (the
  2025 successor term to "prompt engineering") — Anthropic's own
  engineering blog frames it as "curating and maintaining the optimal set
  of tokens during inference," and LangChain's formalization breaks it
  into **write / select / compress / isolate** strategies. This doc's
  layer 1 is "write" (durable, authored); layers 2-3 are "select" (pull in
  only what's relevant).
- **A concrete existence proof, not just theory**: Claude Code's own
  system prompt is reportedly not one monolithic document but "110+
  separate instruction strings conditionally assembled based on context"
  (per third-party analysis) — i.e., a flagship production agent already
  works the way this doc proposes, not a hypothetical.
- **Where per-user long-term memory should live in the prompt — system-
  adjacent, not folded into the user's message.** Letta/MemGPT's
  three-tier memory model puts "core memory" (key facts/preferences about
  the user) as content that's "embedded inside system instructions and
  always remains in-context," distinct from recall/archival memory
  fetched only on demand. This matches layer 3's design (a distinct,
  consistently-injected segment ranked above the live user message) — and
  means the current string-prepending-onto-the-user-message approach is
  the wrong location, not just an inelegant mechanism. Flagged as
  **convention, not a formal spec** — no equivalent of the Model Spec
  mandates this placement, but it's what practitioners converged on.
- **Token budget: retrieve/include conditionally, don't always-send
  everything.** One cited benchmark: full-context approaches ran
  23,000-26,000 tokens/query vs. 600-1,500 tokens with retrieval-based
  memory inclusion — over 90% reduction with minimal quality loss. This is
  the direct justification for layer 2 being conditional rather than
  always-on like today's monolithic prompt.
- **Caveat worth taking seriously**: reliably enforcing that lower layers
  *can't* override higher ones is still an active adversarial-robustness
  research problem (prompt injection via tool output/RAG content), not
  something the layering alone guarantees. This is exactly why
  `docs/guardrails-plan.md`'s four-layer input/output classification stays
  in place as a separate, independent defense — this doc's layering is
  about *organizing* the prompt well, not a replacement for the guardrails
  that actually *police* what gets through.

## Do we need LangGraph's state machine (hand-built graph)?

**No.** `create_agent` is already built on LangGraph under the hood
(confirmed directly — Phoenix traces show a top-level `LangGraph` span
wrapping every agent call) — we're already using it, just through a
convenience wrapper that hides the graph structure.

LangChain's **middleware** system is the documented, purpose-built
mechanism for exactly this: a `wrap_model_call`-style hook can inspect the
current request/state and rewrite the system message **before each model
call**, and middleware passes straight into the existing call shape —
`create_agent(model=..., middleware=[...], tools=[...])`. No graph
rewrite, no dropping `create_agent`'s loop management (which is the whole
reason this project migrated to LangChain in the first place, per
`CLAUDE.md`).

Hand-building a raw graph would only be justified if different turns
needed genuinely different *execution paths* — different tool sets,
different node sequencing, branching logic — not just different *prompt
text* wrapped around the same linear tool-calling loop. Since this bot's
shape stays "one agent, one tool-calling loop, varying instructions,"
middleware is the right-sized tool. This is a firm recommendation, not an
"it depends" — the docs and API are purpose-built for this exact case.

**Long-term memory storage**: LangGraph does have a dedicated `BaseStore`/
`InMemoryStore` API distinct from thread-scoped state, designed for
exactly this "persistent per-user facts, shared across sessions" need —
worth knowing it exists as the "official" LangGraph-native answer. Not
recommending adoption now: `users_db.py` already does this job (working,
tested, deployed, and already holds `interests`) — introducing
LangGraph's `Store` abstraction on top would be a second persistence
mechanism to keep in sync for no functional gain at this project's scale.
Layer 3's middleware can just call `users_db.get_interests()` directly.

## Sketch of how this maps onto the existing code

Not a full implementation plan — exact LangChain middleware API details
(decorator name, exact `ModelRequest`/state shape) need verifying against
the actually-installed LangChain version before writing real code, same
practice as checking `ParseMode`/`BadRequest` import paths earlier this
project rather than assuming from memory.

- **Layer 1**: `agent.py`'s `SYSTEM_PROMPT` shrinks to identity + scope
  confinement + anti-role-play only — the part that's already effectively
  layer 3 in `docs/guardrails-plan.md`'s terms. Telegram formatting and
  tool-usage notes move out.
- **Layer 2**: one or more middleware functions registered on
  `create_agent(..., middleware=[...])`. Telegram formatting rules become
  a middleware that always fires (since every response goes to Telegram
  regardless of topic) — arguably this one is unconditional in practice,
  which is fine, conditionality is a spectrum, not a hard rule for every
  layer-2 fragment. Tool-usage/workflow notes become middleware that
  fires only when relevant — "relevant" needs a decision: reuse the same
  lightweight-classifier-call pattern already established in
  `guardrails.py`, or simpler rule-based heuristics (e.g. presence of
  certain keywords, or just always include tool-usage notes too since
  they're small). Not decided — a real design choice for implementation
  time, not this doc.
- **Layer 3**: a middleware that calls `users_db.get_interests()` (and
  whatever other per-user preferences exist later) and formats them into
  a consistent prompt segment, replacing the current ad hoc
  string-prepend in `bot.py`'s `handle_message()`.
- **Layer 4**: unchanged — the user's raw message, already lowest in the
  message list's actual order.

## Open questions

- Exact LangChain middleware API surface for this installed version — not
  verified yet, needed before implementation.
- How layer 2 decides what's "relevant" for a given turn — reuse a
  classifier call (cost: another DeepSeek call, same tradeoff already
  discussed in `docs/guardrails-plan.md`) vs. cheap heuristics vs. just
  not bothering to conditionalize every fragment (some layer-2 content,
  like Telegram formatting, is arguably always relevant anyway).
- Whether to formally adopt LangGraph's `Store` API later if per-user
  memory grows more complex, or keep extending `users_db.py` directly —
  leaning toward the latter unless a concrete need for `Store`-specific
  features (e.g. built-in semantic search over memories) shows up.
- Migration order relative to `docs/guardrails-plan.md`'s own open
  questions (cost/latency of the extra classifier calls, false-positive
  tuning from the "What's trending?" incident) — these two efforts share
  the same "extra LLM call per turn" cost concern and should probably be
  reasoned about together, not in isolation.
