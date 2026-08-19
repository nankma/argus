"""
A LangChain-shaped model backed by the `claude` CLI instead of a paid API key.

Why this exists: the manual harnesses in this folder (measure_guardrails.py,
run_eval.py) and the classification path in news_classify.py all need a *real*
model -- fakes can't tell you whether a prompt actually works. That normally
means real DEEPSEEK_API_KEY spend. When a Claude Code subscription has headroom
that DeepSeek's balance doesn't, this routes those same calls through the CLI,
which bills against the subscription rather than per-token API credit.

It implements only the slice of the model interface this project's non-agent
code paths use:

    model.with_structured_output(SomePydanticModel).invoke(messages)

...which is what news_classify.classify_articles, guardrails.classify_message,
and guardrails.is_output_on_topic all call. It is deliberately NOT a full
BaseChatModel -- the agent loop needs bind_tools() and streaming, and this
can't provide either. Use it to verify prompts and structured-output shapes,
not to run the agent.

    from tools.claude_cli_model import ClaudeCLIModel
    model = ClaudeCLIModel(model="sonnet")
    result = model.with_structured_output(ClassificationBatch).invoke(messages)

WHAT A PASS HERE DOES AND DOESN'T PROVE
---------------------------------------
Verifying a prompt through this shim confirms the prompt is well-formed, the
schema is satisfiable, and the calling code's own logic (chunking, index
mapping, fail-open handling) works against a real model. It does NOT transfer
model-specific limits: news_classify.MAX_ARTICLES_PER_CALL exists because
DeepSeek's *output token limit* truncated large batches, and Claude's limit is
different. A batch size that passes here can still fail on DeepSeek. Model-
specific thresholds have to be measured against the model that will run them.

`--safe-mode` is passed on every call so the classification prompt isn't
polluted by this repo's CLAUDE.md, skills, or hooks -- without it the CLI
would inject project instructions into what is supposed to be a bare
classifier.
"""

import json
import shutil
import subprocess

from pydantic import BaseModel

DEFAULT_MODEL = "sonnet"
DEFAULT_TIMEOUT_SECONDS = 300


class ClaudeCLIError(RuntimeError):
    """The CLI itself failed -- not found, non-zero exit, timeout, or a
    response that wasn't the JSON envelope we asked for."""


class _StructuredRunner:
    """What with_structured_output() hands back: something with .invoke()."""

    def __init__(self, parent: "ClaudeCLIModel", schema: type[BaseModel]):
        self._parent = parent
        self._schema = schema

    def invoke(self, messages: list[dict]) -> BaseModel | None:
        """Returns a `schema` instance, or None if the model's reply didn't
        validate against it. None rather than an exception because that's what
        the callers already handle -- both news_classify and guardrails treat a
        None result as "classification unavailable" and fail open."""
        system = "\n\n".join(
            m["content"] for m in messages if m.get("role") == "system"
        )
        user = "\n\n".join(
            m["content"] for m in messages if m.get("role") != "system"
        )
        raw = self._parent._run(system, user, self._schema.model_json_schema())
        try:
            return self._schema.model_validate_json(raw)
        except Exception:
            # The CLI validates against the JSON Schema, but that schema is a
            # lossy projection of the pydantic model (Literal enums, nested
            # models), so a schema-valid reply can still fail model validation.
            return None


class ClaudeCLIModel:
    """Minimal stand-in for a chat model, backed by `claude -p`."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        max_budget_usd: float | None = None,
    ):
        if shutil.which("claude") is None:
            raise ClaudeCLIError(
                "the `claude` CLI isn't on PATH -- this shim shells out to it"
            )
        self.model = model
        self.timeout = timeout
        self.max_budget_usd = max_budget_usd
        # Rough running totals, so a harness can report what a run actually
        # cost instead of the operator having to guess.
        self.total_cost_usd = 0.0
        self.calls = 0

    def with_structured_output(self, schema: type[BaseModel]) -> _StructuredRunner:
        return _StructuredRunner(self, schema)

    def _run(self, system: str, user: str, json_schema: dict) -> str:
        cmd = [
            "claude",
            "-p",
            "--safe-mode",
            "--model", self.model,
            "--output-format", "json",
            "--json-schema", json.dumps(json_schema),
        ]
        if system:
            cmd += ["--system-prompt", system]
        if self.max_budget_usd is not None:
            cmd += ["--max-budget-usd", str(self.max_budget_usd)]

        try:
            # The prompt goes over stdin, not argv: a 50-article batch is tens
            # of kilobytes and Windows caps a command line around 32k.
            proc = subprocess.run(
                cmd,
                input=user,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            raise ClaudeCLIError(f"claude CLI timed out after {self.timeout}s")

        if proc.returncode != 0:
            raise ClaudeCLIError(
                f"claude CLI exited {proc.returncode}: {proc.stderr[:400]}"
            )
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise ClaudeCLIError(f"unparseable CLI output: {proc.stdout[:400]}")

        if envelope.get("is_error"):
            raise ClaudeCLIError(f"CLI reported an error: {envelope.get('result')}")

        self.calls += 1
        self.total_cost_usd += float(envelope.get("total_cost_usd") or 0.0)
        return envelope.get("result") or ""
