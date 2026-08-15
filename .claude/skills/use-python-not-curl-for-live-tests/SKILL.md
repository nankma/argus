---
name: use-python-not-curl-for-live-tests
description: Use when manually testing a live/deployed endpoint (test_api.py, curl through an SSH tunnel, or similar) with non-English or non-ASCII text, or when such a test shows an unexpected/suspicious result that a direct or offline call doesn't reproduce.
---

# Use a Python script, not curl, for live tests involving non-ASCII text

**Rule:** when manually testing a live endpoint (`test_api.py`'s
`/test_message`, or anything similar) with non-English text — Chinese,
emoji, any multi-byte UTF-8 — send the request with a small Python
script (`urllib.request`/`requests`, explicit `.encode("utf-8")`), not a
raw `curl -d '{"text": "..."}'` invoked through the shell. Plain ASCII
payloads are fine either way; this only matters once the payload has
non-ASCII bytes in it.

**Why:** `curl` with non-ASCII text embedded in a `-d` argument, invoked
through this project's shell tooling, has silently mangled the payload
**twice** in this project's history — both times producing a false
signal that looked exactly like a real router/classifier bug:

1. An SSH tunnel that had accumulated stale/dual-stack state corrupted
   `curl` requests through it ~25% of the time (docs/local-testing-api-
   plan.md's "Resolved issue" section) — first misread as "the router
   misclassifies Chinese input."
2. `curl -d '{"text": "我對區塊鏈很感興趣", ...}'` run through this
   session's Bash tool mangled the multi-byte payload before it left the
   local machine, even through a freshly-verified, non-stale tunnel
   (docs/plans/guardrails-plan.md's retracted "Chinese-language crypto"
   incident) — second misread as "the router misclassifies Chinese
   crypto-related requests specifically."

Both times, the same decisive test disproved it: send the **identical**
request via Python (`urllib.request`, JSON-encoded to UTF-8 bytes)
through the **same** transport (same tunnel, same endpoint) — it
succeeded immediately. The classifier/router was never broken either
time; the bytes that reached it were.

## What to do instead

```python
import json, urllib.request

payload = json.dumps({"chat_id": 999, "text": "我對區塊鏈很感興趣"}).encode("utf-8")
req = urllib.request.Request(
    "http://127.0.0.1:8765/test_message",
    data=payload,
    headers={"Content-Type": "application/json"},
)
print(json.loads(urllib.request.urlopen(req, timeout=60).read()))
```

`tools/measure_guardrails.py --via-http <url>` already does this
correctly (it uses `urllib.request` internally, not `curl`) — prefer it
over an ad hoc script whenever the case is already in its `LAYER2_CASES`/
`LAYER4_CASES` datasets, or add the case there if it's new. Only reach
for a one-off script for something not worth adding to that dataset.

If printing the response locally, remember Windows' console (`cp1252`)
can't display Chinese by default — add
`sys.stdout.reconfigure(encoding="utf-8")` before printing, or the
script itself will crash on a correct, successful response.

## If you already got a suspicious result from curl

Don't write it up as a finding yet. Isolate the transport before trusting
the content:

1. Re-run the **exact same text** via a Python script (above) through
   the **same** path (same tunnel, same endpoint) curl used. If it now
   succeeds, the bug was in the curl invocation, not the product — stop
   here, this is resolved.
2. If it still fails via Python too, THEN it's worth a real
   investigation — call the underlying function directly (e.g.
   `guardrails.classify_message`) both locally and via `docker exec`
   inside the deployed container, to further isolate whether it's the
   model/prompt itself or something about the live process's wiring
   (see docs/plans/guardrails-plan.md's incidents for worked examples of this
   elimination process).

This is the same discipline `docs/plans/guardrails-plan.md` and
`docs/plans/model-portability-plan.md` already apply to guardrail prompt
changes ("measure before shipping, and measure the actual failure mode
before assuming what it is") — applied one layer earlier, to the test
methodology itself, not just the fix.
