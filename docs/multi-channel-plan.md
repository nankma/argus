# Multi-Channel Plan: Adding LINE Alongside Telegram

Nothing here is built yet — this doc exists to capture the goal, technical
approach, and open questions before implementation starts, same pattern as
`docs/bot-features-plan.md` and `docs/deployment-plan.md`.

**The ask:** since the bot already runs on a cloud VM rather than a laptop,
add LINE as a second client alongside Telegram, so users can talk to it from
either app.

## Status

| # | Item | Status |
|---|------|--------|
| 1 | Public HTTPS webhook infrastructure | Not started — see below, biggest open item |
| 2 | Platform-aware identity in `users_db.py` | Not started — schema change touching most of the module |
| 3 | LINE channel adapter (`line_bot.py`) | Not started |
| 4 | Platform-conditional reply formatting | Not started |
| 5 | Push notifications across platforms | Not started |
| 6 | Admin approval flow extension | Not started |

## Why this is a bigger lift than "add a second bot token"

Telegram's design in this project was deliberately polling-based specifically
to avoid a public endpoint — `bot.py`'s own docs say "no public HTTPS
endpoint/TLS needed... same shape locally and in a future Kubernetes
Deployment." LINE's Messaging API has no polling equivalent: it is
webhook-only. LINE's servers POST events to a URL you register, over HTTPS,
with a real (non-self-signed) TLS certificate. This is new infrastructure
this project has specifically avoided until now, not a drop-in second
adapter.

Separately, `users_db.py`'s `subscribers` table is keyed on Telegram's
integer `chat_id` everywhere — interests, push, language, access control.
LINE user IDs are opaque strings (e.g. `U4af4980629...`), which don't fit an
`INTEGER PRIMARY KEY` column at all. This is a schema change, not a new
column.

The good news: `agent.py`, `guardrails.py`, and `news_push.py` already don't
know anything about Telegram specifically — `bot.py` is the only
Telegram-specific glue calling into a platform-agnostic core
(`build_agent`/`run_agent`, `classify_message`, `is_output_on_topic`,
`fetch_new_articles`/`write_push_digest`). That separation already paid off
once this session (see `combined_bot.py`'s docstring: "no changes were
needed to `agent.py`'s core logic to add this second entry point"). Adding
LINE is a new adapter plus the two items below, not a rewrite of the core.

## 1. Public HTTPS webhook infrastructure

LINE requires:
- A publicly reachable HTTPS URL, registered in the LINE Developers console
  as the channel's webhook endpoint.
- A real CA-issued TLS certificate — self-signed won't work, and **Let's
  Encrypt needs a real domain name, not a bare IP.** Open question: does the
  project have a domain to use? Not assumed here — needs to be confirmed
  before this item can be built at all.
- A fast response (LINE expects an ack within a few seconds); heavy work
  (the actual agent call) should happen after acknowledging, not block the
  webhook response.

**Recommended shape**, matching this project's established "keep it on the
one small VM, avoid new paid infra" pattern (see `docs/deployment-plan.md`'s
E2.1.Micro reasoning): a lightweight reverse proxy (Caddy — auto-provisions
and renews Let's Encrypt certs with a few lines of config, far less
operational overhead than manually running `certbot`) in front of a small
internal HTTP server the bot process runs (e.g. `aiohttp.web`, since
`combined_bot.py` already runs its own asyncio event loop that an
`aiohttp` server can share). Rejected alternative: an OCI Load
Balancer for TLS termination — adds real cost/complexity for a feature this
project can get for free with Caddy on the existing VM.

Same two-firewall-layer gotcha documented in `docs/deployment-plan.md`
("Setup notes") will apply again: both the OCI Security List (cloud-level)
and the VM's local `iptables` (host-level) need a rule opening the HTTPS
port, or the webhook silently can't be reached — this bit the Phoenix OTLP
setup once already this session; expect to hit it again here and check both
layers immediately rather than assuming one is sufficient.

**Signature verification is the actual security boundary here**, replacing
Telegram's implicit trust model (only the bot-token holder can long-poll
Telegram's servers for updates; nobody can push fake updates to you). With a
public webhook, *anyone* can POST to the endpoint, so every request must be
verified via the `X-Line-Signature` header — base64(HMAC-SHA256(channel
secret, raw request body)) — computed and compared before touching the
payload at all. Reject anything that doesn't match. This is not optional
hardening; it's the equivalent of Telegram's guardrail layer 1 for a
completely different threat (spoofed requests, not prompt injection) and
needs the same "verify before processing" discipline.

New secrets (LINE channel secret, LINE channel access token) get the same
treatment as every other credential in this project: OCI Vault +
`docker-entrypoint.sh`'s existing `*_SECRET_OCID` pattern, never baked into
the image or passed as plain env vars in production. No new secrets-handling
design needed — the existing pattern already generalizes.

## 2. Platform-aware identity in `users_db.py`

Every function in `users_db.py` (`get_status`, `request_access`, `decide`,
`get_interests`/`set_interests`, `get_push_enabled`/`set_push_enabled`,
`get_language`/`set_language`, `list_push_enabled_subscribers`, etc.) is
keyed on a bare `chat_id`. Adding a second platform means every one of these
needs to know *which* platform's `chat_id` it's talking about, since the
same raw ID space could theoretically collide across platforms (unlikely in
practice given LINE's ID format, but the schema shouldn't rely on that).

**Design direction**: add a `platform` column (`TEXT`, e.g. `"telegram"` /
`"line"`) to `subscribers`, and widen `chat_id` from `INTEGER` to `TEXT`
(SQLite's dynamic typing makes this painless — existing Telegram integer
IDs still compare/store fine as text) with the primary key becoming the pair
`(platform, chat_id)`. Every `users_db.py` function signature gains a
`platform` parameter, and every caller (`bot.py`, `admin_bot.py`,
`agent.py`'s tools via `ToolRuntime.context`, `news_push.py`) needs to
thread it through — mechanical but touches nearly the whole module and
everything that calls it. Worth doing as its own isolated commit/PR before
any LINE-specific code lands, so the migration is easy to verify
independently (existing Telegram-only tests should all still pass with
`platform="telegram"` threaded through, proving the refactor didn't change
behavior before any new platform is added).

Existing production data: per this session's established stance on this
DB ("this is a demo, not worth preserving what we'll grow out of" — see
the earlier Oracle Autonomous Database discussion), a clean migration
that resets `subscribers` is acceptable rather than writing a careful
in-place `ALTER`/backfill. Confirm this is still the right call before
migrating, since real approved users and their interests currently live
there.

## 3. LINE channel adapter (`line_bot.py`)

Mirrors `bot.py`'s role: the only LINE-specific file, translating between
LINE's webhook event shape and the same platform-agnostic core `bot.py`
already calls. Per-message flow would be the same four-layer guardrail
pipeline already built (`fails_local_prefilter` → `classify_message` →
`run_agent` → `is_output_on_topic`) — none of that changes; only the
inbound (webhook event → text) and outbound (agent reply → LINE API call)
edges are new.

**Reply mechanism differs from Telegram**: LINE webhook events carry a
`replyToken`, valid once, for a short window — the initial reply must use
`replyMessage`. Anything after that window (notably: periodic push
notifications, which by definition aren't replying to a fresh inbound
event) must use LINE's `pushMessage` API against the user's ID instead.
`news_push.py`'s already-generic `send(chat_id, text)` callback shape
fits this reasonably well, but the channel adapter layer needs to pick the
right one of these two per situation.

**Open question, needs verification in LINE's own docs/console before
committing to a design**: LINE's free tier has historically capped monthly
push messages (as distinct from replies, which are typically unlimited)
— unlike Telegram's bot API, which has no comparable message-volume cap.
If push notifications are meaningfully rate- or count-limited on LINE's
free tier, that changes whether `news_push.py`'s per-subscriber,
potentially-multiple-times-a-day design is viable there without hitting a
quota. Confirm current limits before promising LINE users the same push
experience Telegram users get.

## 4. Platform-conditional reply formatting

`agent.py`'s `HTML_FORMATTING_RULES`/`TREND_REPORT_STRUCTURE` explicitly
instruct Telegram HTML (`<b>`, `<a href="">`, the 📰 report structure) —
this is baked in as a Telegram-only assumption, and LINE's default text
message type doesn't render HTML at all (literal `<b>` tags would show up
as visible text, the same class of bug this project already hit and fixed
once for Markdown-vs-Telegram-HTML). LINE does support richer message
types (Flex Messages, a JSON-based layout format) but that's a materially
different output shape than an HTML string, not just a different tag
vocabulary.

**Design direction**: thread a `channel` (or similar) value through
`agent.py`'s existing `context` mechanism (the same plumbing `chat_id` and
`category` already use via `_compose_prompt`), and branch the formatting
instructions on it — a `_LINE_FORMATTING_RULES` constant alongside the
existing `HTML_FORMATTING_RULES`, selected per-channel the same way
`_LAYER2_BY_CATEGORY` is selected per-category today. Keep the actual
report *structure* (title, subtitle, synthesis, sources) shared between
both — only the markup/tag vocabulary differs.

## 5. Push notifications across platforms

`news_push.run_push_cycle`'s `send: callable` parameter is already
generic in shape, which was a deliberate design choice (kept the module
testable without a live Bot/Application — see its docstring). Extending
it to be platform-aware means `list_push_enabled_subscribers()` also
needs to return `platform` (once item 2 lands), and the caller
(`bot.register_push_job`'s `_push_job`, or wherever this ends up living
once there's more than one channel registering jobs) dispatches to the
right platform's send implementation per subscriber.

## 6. Admin approval flow extension

Recommend keeping admin notifications on the *existing* Telegram admin
bot regardless of which platform a request came from — no need to
duplicate the Approve/Deny UX on LINE too. `notify_admin`'s message needs
to say which platform the request is from, `admin_bot.py`'s
`handle_decision` needs to update the right `(platform, chat_id)` row
(once item 2 lands), and — the part actually easy to get wrong — the
outcome notification back to the requester must go out through *their*
platform's send mechanism, not always Telegram. A LINE user approved via
the Telegram admin bot needs a LINE push message telling them so, not a
Telegram message to a chat_id that doesn't exist for them.

## Suggested build order

1. Item 2 (platform-aware `users_db.py`) alone, with `platform="telegram"`
   threaded through everywhere and the full existing test suite passing
   unchanged in behavior — proves the refactor is safe before any LINE
   code exists to depend on it.
2. Item 1 (webhook infra: domain, Caddy, both firewall layers, signature
   verification) as its own milestone, testable independently with a
   trivial echo handler before any agent logic is wired to it.
3. Items 3-6 (the actual LINE adapter, formatting, push, admin flow) once
   1 and 2 are both solid.

Confirm the domain-name question (item 1) and the LINE push-quota question
(item 3) before starting — both could change the shape of this plan
materially, and neither can be resolved from this side without checking
LINE's/the user's own accounts.
