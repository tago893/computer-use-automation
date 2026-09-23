"""Append-only JSONL run log.

One event per line, flushed as it is written, so a run that crashes halfway still
leaves a readable log up to the crash -- the log most worth having.

Every line passes through ``scrub`` before it touches disk. Secrets never reach
the log by construction (steps record placeholders), and the scrub is the second
lock on the same door: if the surface itself echoes a credential back, it is
removed here.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class RunLog:
    """Writes ``<run_dir>/<name>.jsonl`` plus screenshots beside it."""

    def __init__(
        self,
        run_dir: Path,
        *,
        name: str = "discovery",
        scrub: Callable[[str], str] = lambda text: text,
    ) -> None:
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = run_dir / f"{name}.jsonl"
        self._scrub = scrub
        self._handle: TextIO | None = self.path.open("a", encoding="utf-8")

    def event(self, kind: str, **payload: Any) -> None:
        if self._handle is None:
            raise RuntimeError("run log is closed")
        line = json.dumps({"ts": utc_now(), "event": kind, **payload}, default=str)
        self._handle.write(self._scrub(line) + "\n")
        self._handle.flush()

    def screenshot(self, name: str, png: bytes) -> str:
        """Save evidence. Returns the path relative to the run directory."""
        path = self.run_dir / f"{name}.png"
        path.write_bytes(png)
        return path.name

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> RunLog:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
