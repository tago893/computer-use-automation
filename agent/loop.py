"""The discovery loop: observe -> decide -> guard -> act -> record.

This is the only place a model influences what happens on the surface, so it is
also where every bound lives. A run always ends, and always ends for a named
reason:

* the model calls ``finish`` (success or business outcome) or ``escalate``;
* the step budget or the wall clock runs out;
* the run **stalls** -- clicks that change nothing, or actions that keep failing;
* the surface ends up **outside the allowlist**;
* the provider or the surface fails outright.

Guardrails are checked *inside* the loop, between the decision and the act, so no
path exists from a model's output to the surface that skips them. A blocked action
is not performed, is recorded as blocked, and is explained to the model on the
next turn.

The loop depends on ``Surface`` and ``ToolCallingLLM`` -- protocols -- and on
nothing vendor- or browser-specific.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Final

from policy.guard import ActionPolicy
from surfaces.models import ActionKind, Observation, OrdinalNotFound, SurfaceError
from surfaces.protocol import Surface

from . import tools
from .bindings import BindingError, Bindings
from .llm import Decision, LLMError, ToolCallingLLM
from .prompt import SYSTEM_PROMPT, render_step_prompt
from .records import DiscoveryResult, RunStatus, StepRecord, TargetInfo
from .runlog import RunLog
from .tools import Intent, Outcome, ToolRejected

logger = logging.getLogger(__name__)

_ORDINAL_ACTIONS: Final[frozenset[ActionKind]] = frozenset(
    {ActionKind.CLICK, ActionKind.TYPE, ActionKind.READ}
)


@dataclass(frozen=True)
class Limits:
    """Every bound on a run, in one place."""

    max_steps: int = 25
    wall_clock_s: float = 300.0
    #: Consecutive successful clicks that leave the page unchanged.
    stall_limit: int = 3
    #: Consecutive steps that were rejected, blocked, or failed.
    failure_limit: int = 3


@dataclass
class _RunState:
    """Mutable accumulator for one run. Never escapes ``run``; the result is frozen."""

    steps: list[StepRecord] = field(default_factory=list)
    outputs: dict[str, str] = field(default_factory=dict)
    note: str = ""
    failures: int = 0
    stalls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class _Terminal:
    status: RunStatus
    reason: str
    outcome_code: str = ""


class DiscoveryAgent:
    """Drives one surface toward one goal with one model."""

    def __init__(
        self,
        *,
        surface: Surface,
        llm: ToolCallingLLM,
        policy: ActionPolicy,
        bindings: Bindings,
        limits: Limits = Limits(),
        log: RunLog | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._surface = surface
        self._llm = llm
        self._policy = policy
        self._bindings = bindings
        self._limits = limits
        self._log = log
        self._clock = clock

    # -- public ------------------------------------------------------------

    def run(self, goal: str, *, run_id: str) -> DiscoveryResult:
        state = _RunState()
        started = self._clock()
        self._emit(
            "run_started",
            run_id=run_id,
            goal=goal,
            model=self._llm.model_id,
            inputs=dict(self._bindings.inputs),
            secrets=sorted(self._bindings.secrets),
            limits=vars(self._limits),
        )

        terminal: _Terminal | None = None
        try:
            observation = self._surface.observe()
            self._evidence("step-00-start")
            for index in range(self._limits.max_steps):
                if self._clock() - started > self._limits.wall_clock_s:
                    terminal = _Terminal(RunStatus.TIMED_OUT, "wall-clock limit reached")
                    break
                observation, terminal = self._step(index, goal, observation, state)
                if terminal is not None:
                    break
            else:
                terminal = _Terminal(
                    RunStatus.BUDGET_EXHAUSTED,
                    f"step limit of {self._limits.max_steps} reached",
                )
        except SurfaceError as exc:
            terminal = _Terminal(RunStatus.SURFACE_ERROR, str(exc))

        self._evidence("final")
        result = DiscoveryResult(
            run_id=run_id,
            goal=goal,
            status=terminal.status,
            reason=terminal.reason,
            steps=tuple(state.steps),
            outputs=dict(state.outputs),
            outcome_code=terminal.outcome_code,
            model_id=self._llm.model_id,
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
        )
        self._emit(
            "run_finished",
            status=result.status.value,
            reason=result.reason,
            outputs=result.outputs,
            outcome_code=result.outcome_code,
            steps=len(result.steps),
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )
        return result

    # -- one step ----------------------------------------------------------

    def _step(
        self, index: int, goal: str, observation: Observation, state: _RunState
    ) -> tuple[Observation, _Terminal | None]:
        prompt = render_step_prompt(
            goal=goal,
            bindings=self._bindings,
            history=state.steps,
            observation=observation,
            outputs=state.outputs,
            step_index=index,
            max_steps=self._limits.max_steps,
            note=state.note,
        )
        state.note = ""
        tick = self._clock()
        try:
            decision = self._llm.decide(
                system=SYSTEM_PROMPT, prompt=prompt, tools=tools.TOOLS
            )
        except LLMError as exc:
            return observation, _Terminal(RunStatus.LLM_ERROR, str(exc))
        state.input_tokens += decision.input_tokens
        state.output_tokens += decision.output_tokens

        parsed = tools.parse(decision.call)
        base = StepRecord(
            index=index,
            tool=decision.call.name if decision.call else "<none>",
            why=parsed.why if isinstance(parsed, Intent) else "",
            url_before=observation.url,
            input_tokens=decision.input_tokens,
            output_tokens=decision.output_tokens,
        )

        if isinstance(parsed, ToolRejected):
            record = replace(base, ok=False, detail=f"rejected: {parsed.problem}")
            return observation, self._settle(record, decision, state, tick)

        if parsed.action.kind in (ActionKind.FINISH, ActionKind.ESCALATE):
            return observation, self._conclude(parsed, base, decision, state, tick)

        return self._act(parsed, base, decision, observation, state, tick)

    def _act(
        self,
        intent: Intent,
        base: StepRecord,
        decision: Decision,
        observation: Observation,
        state: _RunState,
        tick: float,
    ) -> tuple[Observation, _Terminal | None]:
        action = intent.action
        target = None
        if action.kind in _ORDINAL_ACTIONS:
            target = observation.by_ordinal(action.ordinal or 0)
            if target is None:
                record = replace(
                    base,
                    ordinal=action.ordinal,
                    ok=False,
                    detail=f"ordinal {action.ordinal} is not in the current observation",
                )
                return observation, self._settle(record, decision, state, tick)

        base = replace(
            base,
            ordinal=action.ordinal,
            target=(
                TargetInfo(target.role, target.name, target.frame_path)
                if target
                else None
            ),
            text_template=self._bindings.lift(action.text) if action.text else "",
            url=action.url,
            output_name=intent.output_name,
        )

        verdict = self._policy.check(action, target)
        if not verdict.allowed:
            record = replace(
                base, risk=verdict.risk.value, allowed=False, detail=verdict.reason
            )
            return observation, self._settle(record, decision, state, tick)

        try:
            performed = replace(action, text=self._bindings.expand(action.text))
        except BindingError as exc:
            record = replace(base, ok=False, detail=str(exc))
            return observation, self._settle(record, decision, state, tick)

        try:
            result = self._surface.act(performed)
        except OrdinalNotFound as exc:
            record = replace(base, ok=False, detail=str(exc))
            return observation, self._settle(record, decision, state, tick)

        after = self._surface.observe()
        progressed = action.kind is not ActionKind.CLICK or (
            after.fingerprint() != observation.fingerprint()
        )
        record = replace(
            base,
            risk=verdict.risk.value,
            ok=result.ok,
            detail=result.detail,
            read_value=result.read_value,
            url_after=after.url,
            ladder=result.ladder,
            resolution=result.resolution,
            progressed=progressed,
        )
        if result.ok and intent.output_name:
            state.outputs[intent.output_name] = result.read_value

        terminal = self._settle(record, decision, state, tick)
        location = self._policy.check_location(after.url)
        if terminal is None and not location.allowed:
            terminal = _Terminal(RunStatus.POLICY_STOP, location.reason)
        return after, terminal

    def _conclude(
        self,
        intent: Intent,
        base: StepRecord,
        decision: Decision,
        state: _RunState,
        tick: float,
    ) -> _Terminal:
        """Record a ``finish`` or ``escalate`` and turn it into a terminal status."""
        action = intent.action
        record = replace(base, ok=True, detail=action.reason)
        self._record(record, decision, state, tick)

        if action.kind is ActionKind.ESCALATE:
            return _Terminal(RunStatus.ESCALATED, action.reason)
        if intent.outcome is Outcome.BUSINESS_OUTCOME:
            return _Terminal(
                RunStatus.BUSINESS_OUTCOME, action.reason, intent.outcome_code
            )
        return _Terminal(RunStatus.SUCCEEDED, action.reason)

    # -- bookkeeping -------------------------------------------------------

    def _settle(
        self, record: StepRecord, decision: Decision, state: _RunState, tick: float
    ) -> _Terminal | None:
        """Record a step, update the stall/failure counters, and apply the limits."""
        self._record(record, decision, state, tick)

        failed = not (record.ok and record.allowed)
        state.failures = state.failures + 1 if failed else 0
        if not failed and record.tool == "click":
            state.stalls = 0 if record.progressed else state.stalls + 1

        if failed:
            state.note = (
                f"Your last action did not happen: {record.detail}. "
                "Choose again from the CURRENT observation."
            )
            self._evidence(f"step-{record.index + 1:02d}-failed")
        elif not record.progressed:
            state.note = "Your last click did not change the page."

        if state.failures >= self._limits.failure_limit:
            return _Terminal(
                RunStatus.STALLED, f"{state.failures} consecutive actions failed"
            )
        if state.stalls >= self._limits.stall_limit:
            return _Terminal(
                RunStatus.STALLED, f"{state.stalls} consecutive clicks changed nothing"
            )
        return None

    def _record(
        self, record: StepRecord, decision: Decision, state: _RunState, tick: float
    ) -> None:
        record = replace(record, duration_ms=int((self._clock() - tick) * 1000))
        state.steps.append(record)
        logger.info("step %d: %s", record.index + 1, record.summary())
        self._emit("step", **record.to_dict(), rationale=decision.rationale)

    def _emit(self, kind: str, **payload: object) -> None:
        if self._log is not None:
            self._log.event(kind, **payload)

    def _evidence(self, name: str) -> None:
        """Best-effort screenshot. Evidence must never be the reason a run fails."""
        if self._log is None:
            return
        try:
            self._log.screenshot(name, self._surface.screenshot())
        except SurfaceError as exc:
            logger.debug("screenshot %s skipped: %s", name, exc)

