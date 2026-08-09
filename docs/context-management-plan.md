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

## Verified: the middleware API this design uses

Confirmed directly against the installed `langchain==1.3.14`, not assumed:

```python
from langchain.agents.middleware import dynamic_prompt
from langchain.agents.middleware.types import ModelRequest

@dynamic_prompt
def my_prompt(request: ModelRequest) -> str:
    # request.state (messages, etc.) and request.runtime.context are both
    # available here -- runtime.context is how per-call data like chat_id
    # gets threaded in (via create_agent's context_schema / passed at
    # invoke time), not a global.
    return "the full system prompt string for this call"

agent = create_agent(model, tools=TOOLS, middleware=[my_prompt])
```

`dynamic_prompt` replaces the whole system message per call (not
"append") — so layers 1-3 get composed into one string inside a single
`dynamic_prompt` function, computed fresh each call, rather than three
separate middleware functions each contributing a fragment. `AgentMiddleware`
also exposes `wrap_model_call`/`before_model`/`after_model` hooks directly
for anything `dynamic_prompt` doesn't cover (it's a thin convenience
wrapper around `wrap_model_call` specifically for this use case).

## The router design: one classifier call feeds both the guardrail and layer 2

This is the piece that ties this doc together with `docs/guardrails-plan.md`
and the newly-requested natural-language subscription management
(subscribe/unsubscribe to topics, start/stop periodic push — all via plain
conversation, not just `/interests`, since voice input is an eventual goal
and voice doesn't have slash commands).

**The insight**: `guardrails.py`'s layer-2 check (`is_input_on_topic`) and
"what kind of request is this" are the same underlying question, asked
separately today. One classification call can answer both, replacing two
(or more) sequential LLM calls with one:

```python
class MessageClassification(BaseModel):
    on_topic: bool
    category: Literal[
        "news_query", "set_interest", "remove_interest",
        "start_push", "stop_push", "off_topic",
    ]
```

Built with structured output (a Pydantic model via `.with_structured_output()`,
not free-text "yes"/"no" parsing — more robust, and the natural way to
return more than one field). This becomes the **first LLM call** per turn.

**The second LLM call is the existing `create_agent` tool-calling loop —
now doing more than search+format.** `category` from the first call feeds
directly into the `dynamic_prompt` middleware's layer 2: which instructions
and which tool the agent should reach for this turn.

- `category == "news_query"` → layer 2 = search_news usage notes +
  Telegram formatting rules (today's behavior, unchanged).
- `category in ("set_interest", "remove_interest")` → layer 2 = "the user
  wants to update their interests; use the `update_interests` tool, then
  confirm conversationally what changed." Needs a **new tool** exposed to
  the agent (`update_interests(action: "add"|"remove", topic: str)` calling
  `users_db`'s existing `set_interests()`/`get_interests()`).
- `category in ("start_push", "stop_push")` → layer 2 = "the user wants to
  toggle periodic push; use the `set_push_enabled` tool." Needs a **new
  tool** + a **new `users_db` column** (`push_enabled`) — this turn only
  covers recognizing the intent and flipping the flag; the actual
  scheduled sending is out of scope for now (`docs/bot-features-plan.md`
  item 5, still deferred — explicitly agreed when scoping this work).
- `category == "off_topic"` → same as today: skip the second call
  entirely, send the redirect message.

**Why this is designed for extensibility, not just this feature**: adding
a future capability (translation, per-user source selection) means adding
one more `category` value, one more tool, and one more `dynamic_prompt`
branch — not touching the classifier's core shape or the agent's
construction. `guardrails.py`'s `is_input_on_topic`/`is_output_on_topic`
evolve into this richer classifier rather than staying a separate,
narrower check — `docs/guardrails-plan.md` needs a corresponding update
once this is built, since its four-layer design currently assumes a plain
boolean gate, not a router.

**Tools stay a single fixed list, not swapped per category.** `create_agent`
takes `tools=` once at construction time; making the actual tool list
different per request would mean rebuilding the agent per call, which is
wasteful. Simpler and sufficient: keep one growing `TOOLS` list
(`search_news`, `save_note`, `update_interests`, `set_push_enabled`, ...)
always available, and let the `dynamic_prompt`-injected layer 2 text guide
*which* tool the model reaches for this turn — LLMs are generally reliable
at picking the right tool from a fixed set given clear per-turn framing,
so dynamic tool-list swapping isn't needed to get the conditional-behavior
goal.

## Planned refactor: dispatch settings routes out of the agent

**Status: not built.** Identified 2026-08-09 while documenting the request
pipeline for `docs/system-overview.md` §B2.

**The observation.** The router (built, live) classifies every message
into a category, and the agent's layer-2 instructions and tool set are
selected from that. Functionally this works — but *every* category still
runs through the same tool-calling agent loop, differentiated only by
prompt content. That's the wrong shape for half of them.

The categories split cleanly into two kinds of work:

| Route | Categories | Work required |
|---|---|---|
| **A — Research** | `news_query` | Genuinely open-ended: search multiple sources, synthesize across them, cite links. Needs tool use and multiple model steps — an agent loop is the right tool. |
| **B — Settings** | `set_interest`, `remove_interest`, `start_push`, `stop_push`, `set_language` | A bounded state change against one subscriber's record. The router has *already* determined the intent; there is nothing left to reason about multi-step. |

**The problem.** Route B pays for an agent loop it doesn't need. Changing
a push interval is one deterministic write, but currently costs a full
agent invocation — model call, tool-selection reasoning, and a second call
to produce the confirmation. That's latency and token cost spent on a
decision the router already made.

**Proposed change.** Dispatch Route B directly: the router's category maps
to a handler that performs the state change and returns a confirmation,
without entering the tool-calling loop. Only `news_query` enters the
agent.

**Expected benefits:**

- Lower latency and cost on the highest-volume non-news operations
- The routing boundary becomes explicit in code rather than implicit in
  which prompt fragment got selected
- Route B becomes deterministic and therefore fully unit-testable — no
  model call means no non-determinism to measure (see the reliability
  discussion in `docs/guardrails-plan.md`)

**Open design points:**

- Does Route B still need the layer-4 output check? If the confirmation
  text is generated from a template rather than by a model, there is no
  model output to verify, and that call could be dropped too — a further
  saving. If it stays model-generated for tone/language, the check stays.
- Route B must still honor the per-user reply-language preference. A
  templated confirmation would need translation handled explicitly rather
  than falling out of the prompt, which is an argument for keeping one
  small model call on that path.
- Argument extraction (e.g. *which* topic to add) currently happens inside
  the agent via tool-call arguments. Dispatching out of the agent means
  the router must return that too — expanding its structured output from
  `{on_topic, category}` to include an optional argument payload.

That last point is the real design question: it makes the router do more,
and the router is a single point of failure for every message. Worth
measuring whether a richer structured output degrades its classification
accuracy before committing — the same discipline applied in
`docs/system-overview.md` §D1, where a plausible prompt change measured
dramatically worse.

## Open questions

- ~~Exact LangChain middleware API surface~~ — **verified**, see above.
- ~~How layer 2 decides what's "relevant"~~ — **mostly resolved**: the
  router's `category` output drives it directly for anything with a
  distinct intent (interests, push); content that's relevant regardless
  of category (Telegram formatting) just stays unconditional. No separate
  relevance-classifier call needed beyond the one router call itself.
- Exact tool signatures for `update_interests`/`set_push_enabled`, and the
  `users_db` schema addition (`push_enabled` column) — not finalized, a
  real design step at implementation time, not this doc.
- How `docs/guardrails-plan.md` needs to change once `is_input_on_topic`
  becomes this router — it currently documents a boolean layer 2; needs a
  follow-up pass once this is built, not before.
- Whether to formally adopt LangGraph's `Store` API later if per-user
  memory grows more complex, or keep extending `users_db.py` directly —
  leaning toward the latter unless a concrete need for `Store`-specific
  features (e.g. built-in semantic search over memories) shows up.
- Net call count per turn once this ships: **one router call (replacing
  guardrails' old input check) + the agent's own 1-2 calls + the existing
  output check (layer 4 in `docs/guardrails-plan.md`)** — likely a wash or
  slight improvement over today's count, not an addition, since the router
  absorbs what used to be a separate guardrail call. Worth confirming with
  real latency numbers once built, not assumed.
