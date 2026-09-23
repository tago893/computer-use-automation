"""The vocabulary shared by perception and action.

Everything here is frozen. An ``Observation`` is a statement about what the
surface looked like at one instant; mutating it after the fact would make the
recorded step log a lie. Actions are likewise values, so the same action can be
logged, replayed, and compared without any risk of a caller having altered it in
between.

The important type is ``AXNode``. Its ``ordinal`` is the only element address the
model ever sees or emits, which is what keeps the model from inventing CSS
selectors and keeps the harness in sole control of how an address resolves to a
real element.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Final

from .locators import LocatorLadder, Resolution

#: AX roles the agent may act on. Anything outside this set is observable but not
#: clickable or typeable, which keeps the action space small and honest.
ACTIONABLE_ROLES: Final[frozenset[str]] = frozenset(
    {
        "button",
        "link",
        "textbox",
        "searchbox",
        "combobox",
        "checkbox",
        "radio",
        "switch",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "option",
        "tab",
        "slider",
        "spinbutton",
        "listbox",
    }
)

#: Roles worth showing the model for context even though they cannot be acted on.
#: Without these the model cannot read a balance or notice "No such member".
READABLE_ROLES: Final[frozenset[str]] = frozenset(
    {
        "heading",
        "StaticText",
        "cell",
        "gridcell",
        "columnheader",
        "rowheader",
        "paragraph",
        "alert",
        "alertdialog",
        "status",
        "LabelText",
    }
)

#: Hard ceiling on nodes per observation. A legacy page can expose thousands of AX
#: nodes; sending all of them wastes the context window and drowns the signal.
MAX_NODES: Final[int] = 120

#: Accessible names are truncated to this length in an observation.
MAX_NAME_CHARS: Final[int] = 120


class ActionKind(str, Enum):
    """The complete action space.

    Deliberately tiny. Every action the model can take must be expressible as an
    ordinal plus at most one string, because that is what records cleanly into an
    artifact step.
    """

    CLICK = "click"
    TYPE = "type"
    NAVIGATE = "navigate"
    READ = "read"
    FINISH = "finish"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class AXNode:
    """One node of the accessibility tree, addressed by ordinal."""

    ordinal: int
    role: str
    name: str
    value: str = ""
    description: str = ""
    frame_path: tuple[str, ...] = field(default_factory=tuple)
    focusable: bool = False
    disabled: bool = False

    @property
    def actionable(self) -> bool:
        return self.role in ACTIONABLE_ROLES and not self.disabled

    def render(self) -> str:
        """Format for the model prompt, WebArena-style.

        ``[12] textbox 'Member ID'`` reads unambiguously and costs few tokens. The
        frame suffix appears only when the node is not in the main frame, so the
        common case stays terse.
        """
        parts = [f"[{self.ordinal}]", self.role, f"'{self.name}'"]
        if self.value:
            parts.append(f"value='{self.value}'")
        if self.disabled:
            parts.append("(disabled)")
        if self.frame_path:
            parts.append(f"(frame: {'/'.join(self.frame_path)})")
        return " ".join(parts)


@dataclass(frozen=True)
class Observation:
    """A complete snapshot of the surface at one instant."""

    url: str
    title: str
    nodes: tuple[AXNode, ...]
    truncated: bool = False

    def by_ordinal(self, ordinal: int) -> AXNode | None:
        for node in self.nodes:
            if node.ordinal == ordinal:
                return node
        return None

    @property
    def actionable(self) -> tuple[AXNode, ...]:
        return tuple(node for node in self.nodes if node.actionable)

    def render(self) -> str:
        """The text the model actually sees."""
        lines = [f"URL: {self.url}", f"TITLE: {self.title}", "ELEMENTS:"]
        lines.extend(f"  {node.render()}" for node in self.nodes)
        if self.truncated:
            lines.append(f"  ... truncated at {MAX_NODES} nodes")
        return "\n".join(lines)

    def fingerprint(self) -> tuple[str, tuple[tuple[int, str, str], ...]]:
        """A comparable identity for no-progress detection.

        Two observations with the same fingerprint mean the last action changed
        nothing, which is the signal the discovery loop uses to stop rather than
        burn its step budget clicking the same dead control.
        """
        return (
            self.url,
            tuple((node.ordinal, node.role, node.name) for node in self.nodes),
        )


@dataclass(frozen=True)
class Action:
    """An action the model asked for, addressed by ordinal."""

    kind: ActionKind
    ordinal: int | None = None
    text: str = ""
    url: str = ""
    reason: str = ""

    def describe(self) -> str:
        if self.kind is ActionKind.NAVIGATE:
            return f"navigate {self.url}"
        if self.kind is ActionKind.TYPE:
            return f"type [{self.ordinal}] {self.text!r}"
        if self.kind in (ActionKind.FINISH, ActionKind.ESCALATE):
            return f"{self.kind.value} {self.reason!r}"
        return f"{self.kind.value} [{self.ordinal}]"


@dataclass(frozen=True)
class ActResult:
    """What happened when an action was attempted.

    ``ok=False`` here means the *action* could not be performed -- the ordinal did
    not resolve, the element was detached. It does not mean the business task
    failed. Keeping those two ideas in separate types from the very bottom layer is
    what prevents them being conflated at the top.

    ``ladder`` is the durable address of whatever was acted on, validated against
    the live element at the moment of acting. This is what gets recorded into an
    artifact step: the ordinal dies with the observation, the ladder outlives it.
    """

    ok: bool
    action: Action
    detail: str = ""
    read_value: str = ""
    url_after: str = ""
    ladder: LocatorLadder | None = None
    resolution: Resolution | None = None


class SurfaceError(RuntimeError):
    """A surface could not be driven at all (browser crashed, target gone)."""


class OrdinalNotFound(SurfaceError):
    """An ordinal did not correspond to any node in the last observation.

    Almost always means the model acted on a stale snapshot, so the message names
    the valid range to make that obvious in a log.
    """
