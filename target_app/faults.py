"""Deterministic fault injection.

The gradeable question in this project is not "can replay walk a happy path" but
"what does replay do when the surface misbehaves". A live third-party site cannot
be made to fail on command, so the target app can.

Two ways to arm a fault:

* **One-shot, per request.** ``?inject=slow`` on any pane URL. Convenient while
  exploring by hand.
* **Sticky, via the control plane.** ``POST /__control__/inject`` with
  ``{"mode": "timeout", "count": 1}`` arms the next N pane renders. This is what
  the replay tests use, because the fault has to fire mid-flow on a URL the test
  does not construct itself.

Every mode maps onto a distinct branch of the Phase 5 error taxonomy, and that
mapping is the point:

=================  ====================================================
mode               replay must classify it as
=================  ====================================================
``notfound``       BusinessOutcome -- a declared answer for the caller
``validation``     BusinessOutcome -- the input was rejected, and why
``slow``           Recoverable -- bounded condition-based wait
``interstitial``   Recoverable -- dismiss a known obstacle, carry on
``timeout``        Recoverable once (re-authenticate), else HardFailure
``error500``       HardFailure -- stop, surface a debuggable error
=================  ====================================================
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final

from flask import session

_SESSION_KEY: Final[str] = "_fault"
_SLOW_SECONDS: Final[float] = 4.0


class FaultMode(str, Enum):
    """The faults the target app can be told to produce."""

    NONE = "none"
    NOTFOUND = "notfound"
    VALIDATION = "validation"
    TIMEOUT = "timeout"
    INTERSTITIAL = "interstitial"
    SLOW = "slow"
    ERROR500 = "500"

    @classmethod
    def parse(cls, raw: str | None) -> FaultMode:
        """Coerce untrusted input to a mode, defaulting to ``NONE``.

        Unknown strings become ``NONE`` rather than raising: the query parameter
        is attacker-controlled in spirit, and an unknown fault is not a fault.
        """
        if not raw:
            return cls.NONE
        try:
            return cls(raw.strip().lower())
        except ValueError:
            return cls.NONE


@dataclass(frozen=True)
class FaultState:
    """An armed fault and how many renders it still applies to."""

    mode: FaultMode = FaultMode.NONE
    remaining: int = 0

    @property
    def active(self) -> bool:
        return self.mode is not FaultMode.NONE and self.remaining > 0

    def consumed(self) -> FaultState:
        """Return the state after one render has used this fault."""
        if not self.active:
            return FaultState()
        remaining = self.remaining - 1
        if remaining <= 0:
            return FaultState()
        return FaultState(mode=self.mode, remaining=remaining)


SLOW_SECONDS: Final[float] = _SLOW_SECONDS


def arm(mode: FaultMode, count: int = 1) -> FaultState:
    """Arm a sticky fault for the next ``count`` pane renders."""
    state = FaultState(mode=mode, remaining=max(0, count))
    _write(state)
    return state


def disarm() -> None:
    """Clear any armed fault."""
    session.pop(_SESSION_KEY, None)


def armed() -> FaultState:
    """Read the currently armed sticky fault without consuming it."""
    return _read()


def take(one_shot: str | None) -> FaultMode:
    """Resolve the fault for the current render, consuming a sticky one.

    A one-shot ``?inject=`` parameter wins over a sticky fault so that manual
    exploration is never confused by leftover state.
    """
    explicit = FaultMode.parse(one_shot)
    if explicit is not FaultMode.NONE:
        return explicit

    state = _read()
    if not state.active:
        return FaultMode.NONE
    _write(state.consumed())
    return state.mode


def _read() -> FaultState:
    raw = session.get(_SESSION_KEY)
    if not isinstance(raw, dict):
        return FaultState()
    return FaultState(
        mode=FaultMode.parse(raw.get("mode")),
        remaining=int(raw.get("remaining", 0)),
    )


def _write(state: FaultState) -> None:
    if not state.active:
        session.pop(_SESSION_KEY, None)
        return
    session[_SESSION_KEY] = {
        "mode": state.mode.value,
        "remaining": state.remaining,
    }
