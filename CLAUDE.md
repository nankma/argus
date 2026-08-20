# CLAUDE.md

This file provides guidance to Claude Code when working in this repo.
Kept deliberately short — it's loaded into every session regardless of
task, so detail that's only sometimes relevant lives in `docs/` or a
subagent's own definition instead. See **Where to look** below before
assuming something's missing here.

## Overview

A technology-industry news-trend agent on **LangChain** + DeepSeek
(`langchain-deepseek`'s `ChatDeepSeek`). `search_news` pulls from a
pluggable source registry (`news_sources.py`) and writes a trend report
citing sources; `save_note` persists arbitrary notes. Ships as a
Telegram bot (`bot.py`/`admin_bot.py`/`combined_bot.py`) with per-user
interests, reply-language, and scheduled push digests — not one shared
scope, since different subscribers care about different topics.

## Landmines — small, real, worth knowing before you touch nearby code

- **`MODEL` must stay `"deepseek-chat"`.** `"deepseek-reasoner"`
  (DeepSeek-R1) doesn't support tool calling, which `search_news`/
  `save_note` depend on. Don't switch without changing the tools setup.
- **Replies are Telegram HTML, not Markdown.** `SYSTEM_PROMPT` asks for
  `<b>`/`<a href="">`; `bot.py` sends with `parse_mode=ParseMode.HTML`.
  See the `telegram-message-formatting` skill before touching either.
- **Don't add `arize-phoenix` back as a dependency.** The full package
  imports `pandas` at `import phoenix` time, which Windows Smart App
  Control blocks outright on this machine. `arize-phoenix-otel` (what's
  actually used) has no pandas dependency and covers everything needed.
- **Tests never make real API calls.** `PHOENIX_ENABLED` unset is a
  no-op (every test/CI run). `build_agent(model)`/`run_agent(...,
  callbacks=None)` take the model and telemetry as parameters
  specifically so tests can substitute fakes — see `tests/fakes.py`'s
  `FakeToolCallingModel`, not LangChain's built-in fakes (they don't
  override `bind_tools()` and raise `NotImplementedError`).

## Where to look

| Need | Where |
|---|---|
| Full doc index, categorized | `docs/README.md` |
| Full architecture, request pipeline, design principles | `docs/system-overview.md` |
| First-time environment setup, running any piece locally | `docs/reference/setup.md` |
| Current VM topology, security model, deploy process shape | `docs/current/infrastructure.md` (real IPs/keys: `local-infra/infrastructure.yaml`, gitignored) |
| A specific feature's design/history/status | the matching `docs/plans/*.md` — see `docs/README.md` for the full index |
| News source registry (what's live, how to add one) | `docs/current/ai-news-sources.md` |
| Diagnosing a live issue (Phoenix traces, hot-patching) | `docs/reference/observability-and-debugging.md` |

## Testing

```powershell
conda activate myfirstagent
pytest
```

No `DEEPSEEK_API_KEY` needed — runs against fakes/mocks only. Requires
`pytest.ini`'s `pythonpath = .` to import project modules at all.

Write a change's own unit tests as part of the change, and run plain
`pytest` yourself while iterating. **After finishing any code change**
(not doc-only edits), dispatch in order — `code-reviewer` (coding
standards), then `qa-engineer` (matches its documented design, coverage,
smoke suite, guardrail harness), then `deploy-engineer`. Each reports
back pass or a diagnosis; don't advance past a failure.

## Landing a change: PR only, `main` is protected

`git push origin main` is **rejected** — branch protection requires the
`test` check, and it applies to admins too. Every change goes:

```powershell
git switch -c <branch>          # never commit straight onto main
git push -u origin <branch>
gh pr create --fill
gh pr checks --watch            # wait for CI; do not merge on a local green
gh pr merge --squash --delete-branch
```

A green local `pytest` is **not** evidence about CI. On 2026-08-19 two
pushes landed on `main` with CI red because local was green: three
analysis scripts were named `test_*.py`, pytest collected them, and they
imported `numpy`/`sklearn` — present on the dev machine from unrelated
work, deliberately absent from `environment.yml`. Same command, opposite
result. CI's environment is the one that resembles the image.

(`pytest.ini` pins `testpaths = tests` now, so that specific trap is
closed, but the general point stands: the dev machine's environment
drifts and CI's doesn't.)

If CI is genuinely wrong and the change must land, turn protection off
explicitly rather than working around it — and say so:
`gh api -X DELETE repos/nankma/argus/branches/main/protection`.
