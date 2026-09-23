"""What a discovery run leaves behind.

A ``StepRecord`` is the durable account of one step: what the model asked for,
what the guard said, which element was touched -- by *ladder*, not by ordinal --
and what happened. Phase 4 compiles an artifact from these records alone, never
from the model transcript: the transcript says what the model thought, the
records say what the surface did.

Everything here is frozen. A record is a statement of fact about the past.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from surfaces.locators import LocatorLadder, Resolution


class RunStatus(str, Enum):
    """How a discovery run ended.

    Only ``SUCCEEDED`` and ``BUSINESS_OUTCOME`` produce a compilable recording.
    Every other status is a reason the run could not finish on its own, and each
    is distinct because each wants a different response from whoever reads it.
    """

    SUCCEEDED = "succeeded"
    BUSINESS_OUTCOME = "business_outcome"
    ESCALATED = "escalated"  # the model asked for a human
    STALLED = "stalled"  # no progress, or repeated failures
    BUDGET_EXHAUSTED = "budget_exhausted"  # step limit
    TIMED_OUT = "timed_out"  # wall-clock limit
    POLICY_STOP = "policy_stop"  # the surface left the allowlist
    LLM_ERROR = "llm_error"  # the provider failed
    SURFACE_ERROR = "surface_error"  # the surface became undriveable

    @property
    def completed(self) -> bool:
        return self in (RunStatus.SUCCEEDED, RunStatus.BUSINESS_OUTCOME)


@dataclass(frozen=True)
class TargetInfo:
    """The node an action addressed, as the model saw it."""

    role: str
    name: str
    frame_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class StepRecord:
    """One step of a discovery run."""

    index: int
    tool: str
    why: str
    url_before: str
    ordinal: int | None = None
    target: TargetInfo | None = None
    #: What was typed, *as recorded*: a placeholder, never an expanded secret.
    text_template: str = ""
    url: str = ""
    output_name: str = ""
    risk: str = "safe"
    allowed: bool = True
    ok: bool = False
    detail: str = ""
    read_value: str = ""
    url_after: str = ""
    ladder: LocatorLadder | None = None
    resolution: Resolution | None = None
    progressed: bool = True
    duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "tool": self.tool,
            "why": self.why,
            "url_before": self.url_before,
            "ordinal": self.ordinal,
            "target": (
                {
                    "role": self.target.role,
                    "name": self.target.name,
                    "frame_path": list(self.target.frame_path),
                }
                if self.target
                else None
            ),
            "text_template": self.text_template,
            "url": self.url,
            "output_name": self.output_name,
            "risk": self.risk,
            "allowed": self.allowed,
            "ok": self.ok,
            "detail": self.detail,
            "read_value": self.read_value,
            "url_after": self.url_after,
            "ladder": self.ladder.to_dict() if self.ladder else None,
            "resolution": (
                {
                    "matched": self.resolution.matched.name
                    if self.resolution.matched
                    else None,
                    "attempted": [
                        [kind.name, why] for kind, why in self.resolution.attempted
                    ],
                }
                if self.resolution
                else None
            ),
            "progressed": self.progressed,
            "duration_ms": self.duration_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }

    def summary(self) -> str:
        """One line for the model's step history."""
        what = self.tool
        if self.target is not None:
            what += f" [{self.ordinal}] {self.target.role} '{self.target.name}'"
        elif self.url:
            what += f" {self.url}"
        if self.text_template:
            what += f" text={self.text_template!r}"
        if self.output_name:
            what += f" as {self.output_name}"
        if not self.allowed:
            return f"{what} -> BLOCKED: {self.detail}"
        if not self.ok:
            return f"{what} -> FAILED: {self.detail}"
        result = f"{what} -> ok"
        if self.read_value:
            result += f", read {self.read_value!r}"
        if not self.progressed:
            result += " (page did not change)"
        return result


@dataclass(frozen=True)
class DiscoveryResult:
    """The outcome of one discovery run."""

    run_id: str
    goal: str
    status: RunStatus
    reason: str
    steps: tuple[StepRecord, ...] = field(default_factory=tuple)
    outputs: dict[str, str] = field(default_factory=dict)
    outcome_code: str = ""
    model_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        """The recording Phase 4 compiles into an artifact."""
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "status": self.status.value,
            "reason": self.reason,
            "outcome_code": self.outcome_code,
            "outputs": dict(self.outputs),
            "model_id": self.model_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "steps": [step.to_dict() for step in self.steps],
        }
