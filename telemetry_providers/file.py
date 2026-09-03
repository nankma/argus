"""
FileProvider -- writes one structured JSON line per event directly to a
local file. No OTel/span involvement at all: this is what a "FileLogger"
actually promises (simple, robust local durability, readable with `tail
-f`/`jq` even when every hosted backend is unreachable), not
participation in the span model. KIND is {"general"} only -- a flat
JSON-line file has no way to represent the parent/child call structure
an LLM trace needs, so telemetry.py never routes this provider to the
llm category.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from opentelemetry.sdk.trace import TracerProvider

from telemetry_providers import Level


class FileProvider:
    TYPE = "file"
    KIND = frozenset({"general"})
    # Not span-based -- telemetry.py's coordinator calls this class's
    # own log() directly, once per event, since there's no OTel
    # processor to fan out through (see telemetry_providers/__init__.py's
    # module docstring).
    SPAN_BASED = False

    def initialize(self, config: dict, tracer_provider: TracerProvider, kind: str) -> None:
        """`kind` is always "general" here -- file.py's KIND is
        {"general"} only, so telemetry.py never calls this with
        anything else. Ignored; taken only to satisfy the Protocol
        signature every provider shares (see telemetry_providers/
        __init__.py)."""
        self._path = Path(config["path"])

    def log(self, scope: str, event: str, message: str | dict,
            level: int = Level.INFO, tags: tuple[str, ...] = (),
            exc: BaseException | None = None) -> None:
        """Appends one JSON object per line (JSON Lines) -- same
        "one record per call, fails open, never blocks the caller on a
        write error" shape as message_archive.archive_message's
        try/except around its own file write, applied here to a
        single append-mode file instead of one file per record (a
        continuous event stream reads and greps more naturally as
        one growing file than as thousands of tiny ones)."""
        as_dict = message if isinstance(message, dict) else {"message": message}
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "scope": scope,
            "event": event,
            "level": level,
            "tags": list(tags),
            **as_dict,
        }
        if exc is not None:
            record["exception"] = repr(exc)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            # Fails open, deliberately: a logging sink must never be the
            # reason the thing it was logging about (already in progress,
            # possibly a failure itself) doesn't complete. telemetry.py's
            # coordinator already printed this event to stdout before
            # calling here, regardless of whether this write succeeds.
            pass
