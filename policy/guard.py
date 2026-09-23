"""Guardrails checked *before* every action, in discovery and in replay alike.

Two questions, asked in this order:

1. **Is it in bounds?** An allowlist of origins and route prefixes. The agent
   cannot navigate outside it, and a run that *lands* outside it (a link that
   leaves the app) is stopped. The fault control plane (``/__control__``) is
   explicitly out of bounds: the agent must cope with faults, not switch them off.

2. **Is it risky?** An action that changes state irreversibly -- confirming a
   transfer, approving, deleting -- is ``RISKY``. A risky action is refused unless
   the run was explicitly granted permission, and the refusal tells the model to
   escalate. Conservative by design: the brief requires the risky class to be
   handled conservatively, and a false positive costs one human click while a
   false negative moves money.

Risk here is classified from the accessible name of the target. That is a
heuristic, and it is stated as one: in replay, risk is not re-guessed but read
from the ``risk_class`` the artifact declares for each step, which a human
approves. This module is the safety net for discovery, where no artifact exists
yet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Final
from urllib.parse import urlsplit

from surfaces.models import Action, ActionKind, AXNode

#: Verbs that mark an irreversible, state-changing control. Matched on whole
#: words against the accessible name, case-insensitively.
RISKY_VERBS: Final[re.Pattern[str]] = re.compile(
    r"\b(confirm|authori[sz]e|approve|transfer|pay|delete|remove|disburse|"
    r"withdraw|refund|reverse|void|submit payment|close account)\b",
    re.IGNORECASE,
)


class RiskClass(str, Enum):
    SAFE = "safe"  # observe-only or trivially reversible
    RISKY = "risky"  # irreversible or state-changing


@dataclass(frozen=True)
class Verdict:
    """Whether an action may proceed, and why not if it may not."""

    allowed: bool
    risk: RiskClass = RiskClass.SAFE
    reason: str = ""


def classify(action: Action, target: AXNode | None) -> RiskClass:
    """Classify one action. Only clicks can commit anything on this surface.

    Typing into a field commits nothing until something is clicked; reading and
    navigating are observe-only.
    """
    if action.kind is not ActionKind.CLICK or target is None:
        return RiskClass.SAFE
    return RiskClass.RISKY if RISKY_VERBS.search(target.name) else RiskClass.SAFE


@dataclass(frozen=True)
class Allowlist:
    """Where the agent may be. Origins exact, paths by prefix."""

    origins: frozenset[str]
    path_prefixes: tuple[str, ...]
    blocked_prefixes: tuple[str, ...] = ("/__control__",)

    def permits(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return False
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.origins:
            return False
        path = parts.path or "/"
        if any(path.startswith(prefix) for prefix in self.blocked_prefixes):
            return False
        return any(path.startswith(prefix) for prefix in self.path_prefixes)

    @classmethod
    def for_tenant(cls, base_url: str, tenant: str) -> Allowlist:
        """The allowlist for one tenant of the target app: its own routes only."""
        return cls(
            origins=frozenset({base_url.rstrip("/")}),
            path_prefixes=(f"/t/{tenant}/",),
        )


@dataclass(frozen=True)
class ActionPolicy:
    """The guard the discovery loop consults before acting."""

    allowlist: Allowlist
    allow_risky: bool = False

    def check(self, action: Action, target: AXNode | None) -> Verdict:
        if action.kind is ActionKind.NAVIGATE and not self.allowlist.permits(action.url):
            return Verdict(
                allowed=False,
                reason=f"{action.url} is outside the allowlist",
            )

        risk = classify(action, target)
        if risk is RiskClass.RISKY and not self.allow_risky:
            name = target.name if target else "?"
            return Verdict(
                allowed=False,
                risk=risk,
                reason=(
                    f"'{name}' is an irreversible action and this run is not "
                    "approved for risky actions; call escalate if the goal "
                    "requires it"
                ),
            )
        return Verdict(allowed=True, risk=risk)

    def check_location(self, url: str) -> Verdict:
        """Where the surface ended up after an action.

        ``about:blank`` is tolerated: it is where a browser starts, not a place
        an action can take you to that matters.
        """
        if url == "about:blank" or self.allowlist.permits(url):
            return Verdict(allowed=True)
        return Verdict(allowed=False, reason=f"surface left the allowlist: {url}")
