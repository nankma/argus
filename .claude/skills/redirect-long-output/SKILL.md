---
name: redirect-long-output
description: Use when running any command whose output could exceed ~50 lines — docker build, image transfer, package installs, full test suites, docker logs, deploy scripts, or reading a whole file to find one thing. Also use when a session has become expensive, an agent burned far more tokens than the task warranted, or a hook reports a large tool result.
---

# Redirect long output, grep it on failure

## Why this is not a style preference

A model is stateless: every tool call re-sends the whole conversation, so
output is not paid for once but on every later call.

Measured here (3,212 turns):

| | |
|---|---|
| genuinely new input | **6,411 tokens** |
| re-read context | **1,598,178,675 tokens** |
| average context per call | **506,273 tokens** |

Cost is roughly **(turns × conversation length)**, which grows
quadratically. The same `ls` costs ~500x more at turn 3,000 than at turn 10.

And the distribution is extremely long-tailed — across 4,610 real calls:

```
p50 0.2 KB    p95 4.1 KB    p99 13.7 KB
6 calls (0.13%) over 50 KB  ->  27% of ALL output
```

A handful of enormous results dominate. Catching those six is most of the win.

## The pattern

```bash
LOG="$TMPDIR/build.log"
docker build -t img . > "$LOG" 2>&1; echo "exit=$?"
# ONLY if that was non-zero:
grep -iE "^ *(error|fatal)|error:|failed to|cannot " "$LOG" | tail -6
```

Redirect, print the exit code, and look inside **only when it is non-zero** —
then grep for the error, never `cat` or `tail` the whole thing.

Not `-q`, and not `| tail`: both **discard** the output, so a failed build
has to be re-run — minutes, and it may not reproduce. A file keeps
everything at zero context cost.

| Instead of | Do |
|---|---|
| `docker build ...` | redirect to a log, echo the exit code |
| `docker logs c` | `docker logs --tail 50 c` |
| `pytest` | `pytest -q > "$LOG" 2>&1; echo "exit=$?"` |
| `cat file.py` to find one thing | `grep -n 'pattern' file.py`, then read that range |
| `conda list` | `conda list \| grep -i <pkg>` |

## Rationalizations

| Excuse | Reality |
|--------|---------|
| "It's only 30 lines" | 30 lines × every later call. This exact excuse cost a real deploy. |
| "I need to see the error" | Then grep for the error. The other 1,970 lines are not the error. |
| "I'll trim it if it gets big" | You cannot know it is big until it is already in context. Redirect first. |
| "The command usually succeeds" | The one time it doesn't is when the log is longest. |

## Red flags

- About to run a build, install, image transfer, or full test suite with no redirect
- Reading a whole file to check one line
- Writing `| tail -30` into a script's failure path
- A subagent brief that does not mention output discipline

**All of these mean: redirect to a file first.**

## What NOT to trim

Verification output you will actually read, and investigation of a real
failure. The deploys that cost the most also found a CRLF-broken
entrypoint, a silent telemetry regression, and a live provider outage.

Trim noise. Never trim checks.

## Enforcement

`~/.claude/hooks/output_budget.py` warns above 12.5 KB in one tool result
and stops the turn above 50 KB, asking for human approval. Thresholds live
at the top of that file.
