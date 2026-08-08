---
name: telegram-message-formatting
description: Use when editing agent.py's SYSTEM_PROMPT (the format the LLM writes trend reports in) or bot.py's message-sending code (split_for_telegram, parse_mode, HTML escaping/fallback) — the rules this bot's Telegram output has to follow to render well instead of showing up as raw markup or an unreadable wall of text.
---

# Telegram message formatting for this bot

Synthesized from four external Telegram-bot-formatting skills/references
(see Sources below) plus this project's own testing, filtered to what
actually applies to `myfirstagent`'s shape: **LLM-generated structured
reports sent via `reply_text()`**, not hand-authored copy, not an
interactive button-driven UI.

## The core decisions already made (don't re-litigate without a reason)

- **HTML parse mode, not MarkdownV2.** Confirmed via research and this
  project's own use: MarkdownV2 needs 18 context-sensitive escaped
  characters; HTML needs 3 (`< > &`). For LLM-generated content specifically,
  fewer escaping failure modes wins — MarkdownV2's only real edge
  (spoiler/underline shorthand) is covered by HTML tags anyway.
- **`bot.py`'s `split_for_telegram()` is tag-depth-aware**, not naive
  newline-splitting — it will never cut a message in a way that leaves an
  `<b>` open in one chunk and `</b>` in the next. Don't revert to plain
  `rfind("\n", ...)` splitting once HTML is in play.
- **A plain-text fallback exists** in `handle_message()`: if Telegram
  rejects a chunk (`BadRequest`, usually unescaped `&`/`<`/`>` or a tag the
  model wasn't asked to use), it resends with tags stripped rather than
  failing silently. Keep this — an LLM's HTML won't be 100% reliable no
  matter how good the prompt is.

## Structure rules for `SYSTEM_PROMPT` (agent.py)

- **Emoji as a visual anchor, not decoration.** One emoji on the title
  line, optionally one per source-link line — never as a substitute for
  the actual label ("never rely on color or emoji alone — the label
  carries the meaning," per `telegram-bot-ui`). Don't scatter emoji
  through body text.
- **Bold is for the one thing that matters per line** — section titles,
  not every noun. Over-bolding reads as no emphasis at all.
- **Compact, not padded.** Each section's summary should be tight (this
  project uses 1-3 sentences) — `telegram-message-design`'s guideline of
  keeping summaries under ~600 characters is a good sanity ceiling even
  though our reports are more substantive than a quick acknowledgement.
- **Synthesize, don't enumerate**, when multiple sources cover the same
  story — one merged summary beats three near-duplicate bullet points.
  This is judgment the LLM has to exercise; state it explicitly in the
  prompt, don't assume it's implied by "cite your sources."
- **Links compact and clearly marked**, not one per line by default —
  this bot commonly has 2-3 sources per story, so `telegram-compose`'s
  "link on its own line with an arrow" pattern would make reports too
  tall. Use a link emoji + inline separator instead:
  `🔗 <a href="url1">Source 1</a> · <a href="url2">Source 2</a>`.
- **Escape order matters if this project ever adds its own HTML escaping
  logic** (currently the LLM is asked to escape its own output, not code
  doing it programmatically): `&` must be escaped *first*, before `<`/`>`
  — escaping `<` to `&lt;` first and `&` second would double-escape it
  into `&amp;lt;`. Not currently a live bug since no code path does this,
  but a real mistake to avoid if that changes.
- **Never invent a source URL.** Only cite links `search_news` actually
  returned — this was a real gap fixed alongside this skill (the tool's
  output didn't include `link` at all until this session; see agent.py's
  `search_news`).

## Message-sending rules for `bot.py`

- **Small delay between sequential chunks** when a report needs multiple
  messages (`split_for_telegram()` returned >1 chunk) — sending
  instantly back-to-back risks Telegram rate-limiting and reads worse
  than a steady stream. ~1 second between chunks, per `telegram-compose`.
- **Avoid `<pre>`/monospace blocks for report content** — renders badly
  on mobile per `telegram-compose`'s explicit warning. This bot doesn't
  use `<pre>` for reports; keep it that way. `<code>` for short inline
  identifiers (a model name, a ticker) is fine, just not for laid-out data.
- **Treat message editing as a future option, not a requirement.**
  `nzhulikov`'s reference notes editing-in-place ("update one message
  instead of sending clutter") as the more polished pattern for
  interactive bots — not implemented here (this bot only ever sends new
  messages), and not worth the added complexity unless a real use case
  for it shows up (e.g. a live-updating status message).

## What was deliberately *not* adopted from these sources

- `telegram-bot-ui`'s button-design guidance (verb+object labels, 1-3
  buttons per row) — this bot has exactly one interactive keyboard
  (admin_bot.py's Approve/Deny), already simple enough not to need a
  design system.
- `telegram-message-design`'s `correlationId`-per-message idempotency
  tracking — solving a duplicate-delivery problem this bot doesn't have
  (no retry/queue layer sending the same message twice).

## Sources

- https://github.com/hlibsuslov/telegram-bot-ui
- https://skillsmp.com/creators/sefito/virtual-assistant-bot/github-skills-telegram-message-design
- https://mcp.directory/skills/telegram-compose
- https://skillsmp.com/creators/nzhulikov/telegram-bot-skills/skills-telegram-bot-api-03-messages-and-formatting
