"""The locator ladder.

An ordinal is useful for exactly one instant: it addresses a node in one snapshot
and is meaningless afterwards. An artifact has to outlive the snapshot, so every
recorded step stores a *ladder* instead -- several independent ways to find the
same element, ordered most-durable first.

The rungs, and why they are in this order:

1. ``ROLE_NAME``   -- role + accessible name. Survives restyling, reordering, class
   renames, and whole framework migrations. It is the only rung that exists on
   desktop surfaces too, which is why it is first.
2. ``LABEL``       -- the associated ``<label>`` or visible text. Survives markup
   churn, but breaks when a tenant relabels a field.
3. ``ANCHORED``    -- nearest identified ancestor, then role and index within it.
   Survives page-level churn; breaks if rows reorder.
4. ``DOM_PATH``    -- a CSS path. Breaks on any structural edit. Recorded for
   diagnosis, used only as a last resort.
5. ``COORDINATES`` -- a viewport point. Recorded and flagged fragile; breaks on any
   layout change and is never durable. Present because a real system does
   occasionally face a canvas with no other option, and honesty about that is
   better than pretending.

Which rung actually matched at replay time is recorded. A step that used to match
at rung 1 and now matches at rung 3 has not failed -- but the surface has drifted,
and that is worth reporting before it becomes a failure.

This module holds no Playwright import on purpose: it is the recording format, and
the artifact layer depends on it without inheriting a browser dependency.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Final


class RungKind(IntEnum):
    """Ladder rungs, lowest value = most durable = tried first."""

    ROLE_NAME = 1
    LABEL = 2
    ANCHORED = 3
    DOM_PATH = 4
    COORDINATES = 5

    @property
    def durable(self) -> bool:
        """Whether a match on this rung should be trusted without comment."""
        return self <= RungKind.LABEL

    @property
    def fragile(self) -> bool:
        """Whether a match here warrants a drift warning in the replay log."""
        return self >= RungKind.DOM_PATH


@dataclass(frozen=True)
class Rung:
    """One way to find an element.

    ``params`` is intentionally a plain mapping rather than a per-rung type: it is
    serialized into the artifact as JSON, and a flat mapping keeps the schema
    readable on one screen, which the report requires.
    """

    kind: RungKind
    params: dict[str, str | int | float] = field(default_factory=dict)

    def describe(self) -> str:
        rendered = ", ".join(f"{k}={v!r}" for k, v in sorted(self.params.items()))
        return f"{self.kind.name}({rendered})"


@dataclass(frozen=True)
class LocatorLadder:
    """Every known way to find one element, plus where it lives.

    ``frame_path`` is not optional decoration. On a framed surface a locator
    without a frame path is ambiguous, and the target app has three frame levels
    precisely to stop that shortcut being taken.
    """

    frame_path: tuple[str, ...] = field(default_factory=tuple)
    rungs: tuple[Rung, ...] = field(default_factory=tuple)
    recorded_role: str = ""
    recorded_name: str = ""

    def __post_init__(self) -> None:
        if not self.rungs:
            raise ValueError("a ladder with no rungs cannot locate anything")

    @property
    def ordered(self) -> tuple[Rung, ...]:
        """Rungs most-durable-first, regardless of construction order."""
        return tuple(sorted(self.rungs, key=lambda rung: rung.kind))

    @property
    def best(self) -> Rung:
        return self.ordered[0]

    def rung(self, kind: RungKind) -> Rung | None:
        for candidate in self.rungs:
            if candidate.kind is kind:
                return candidate
        return None

    def describe(self) -> str:
        where = f"[{'/'.join(self.frame_path)}] " if self.frame_path else ""
        return where + " -> ".join(rung.describe() for rung in self.ordered)

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-able form. Rung kinds are written by *name*, not number,
        so a recorded file stays readable and survives a renumbering."""
        return {
            "frame_path": list(self.frame_path),
            "recorded_role": self.recorded_role,
            "recorded_name": self.recorded_name,
            "rungs": [
                {"kind": rung.kind.name, "params": dict(rung.params)}
                for rung in self.ordered
            ],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> LocatorLadder:
        """Inverse of ``to_dict``. Rejects unknown rung kinds loudly.

        Raises:
            ValueError: on an unknown rung kind or a ladder with no rungs.
        """
        rungs: list[Rung] = []
        for entry in raw.get("rungs", []):
            try:
                kind = RungKind[str(entry["kind"])]
            except KeyError as exc:
                raise ValueError(f"unknown rung kind {entry.get('kind')!r}") from exc
            rungs.append(Rung(kind, dict(entry.get("params", {}))))
        return cls(
            frame_path=tuple(str(part) for part in raw.get("frame_path", [])),
            rungs=tuple(rungs),
            recorded_role=str(raw.get("recorded_role", "")),
            recorded_name=str(raw.get("recorded_name", "")),
        )


@dataclass(frozen=True)
class Resolution:
    """The outcome of walking a ladder against a live surface.

    Carrying ``attempted`` rather than only the winner is what makes drift
    visible: the log shows every rung that was tried and why each earlier one
    declined.
    """

    matched: RungKind | None
    attempted: tuple[tuple[RungKind, str], ...] = field(default_factory=tuple)

    @property
    def found(self) -> bool:
        return self.matched is not None

    @property
    def drifted(self) -> bool:
        """True when the element was found, but not on the most durable rung."""
        return self.matched is not None and self.matched is not RungKind.ROLE_NAME

    def describe(self) -> str:
        if not self.found:
            tried = "; ".join(f"{kind.name}: {why}" for kind, why in self.attempted)
            return f"unresolved (tried {tried})"
        note = " [drift]" if self.drifted else ""
        return f"matched {self.matched.name}{note}"


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

#: Roles Playwright's ``get_by_role`` understands. CDP emits some names that are
#: not valid ARIA roles (``StaticText``, ``LabelText``, ``generic``); for those,
#: rung 1 is not emitted at all rather than emitted broken.
PLAYWRIGHT_ROLES: Final[frozenset[str]] = frozenset(
    {
        "alert", "alertdialog", "application", "article", "banner", "blockquote",
        "button", "caption", "cell", "checkbox", "code", "columnheader",
        "combobox", "complementary", "contentinfo", "definition", "deletion",
        "dialog", "directory", "document", "emphasis", "feed", "figure", "form",
        "generic", "grid", "gridcell", "group", "heading", "img", "insertion",
        "link", "list", "listbox", "listitem", "log", "main", "marquee", "math",
        "menu", "menubar", "menuitem", "menuitemcheckbox", "menuitemradio",
        "meter", "navigation", "none", "note", "option", "paragraph",
        "presentation", "progressbar", "radio", "radiogroup", "region",
        "row", "rowgroup", "rowheader", "scrollbar", "search", "searchbox",
        "separator", "slider", "spinbutton", "status", "strong", "subscript",
        "superscript", "switch", "tab", "table", "tablist", "tabpanel", "term",
        "textbox", "time", "timer", "toolbar", "tooltip", "tree", "treegrid",
        "treeitem",
    }
)


def build_ladder(
    *,
    role: str,
    name: str,
    frame_path: tuple[str, ...],
    label: str = "",
    text: str = "",
    anchor_id: str = "",
    anchor_role: str = "",
    anchor_index: int | None = None,
    dom_path: str = "",
    point: tuple[float, float] | None = None,
) -> LocatorLadder:
    """Assemble a ladder from what perception observed about one element.

    Rungs are emitted only when their inputs are actually present, so a ladder
    never contains a rung that is guaranteed to fail. A ladder with a single rung
    is legitimate and simply means the element offered only one handle.

    Raises:
        ValueError: if nothing usable was supplied, since a ladder that cannot
            locate anything must not be recordable.
    """
    rungs: list[Rung] = []

    if role in PLAYWRIGHT_ROLES and name:
        rungs.append(Rung(RungKind.ROLE_NAME, {"role": role, "name": name}))

    if label:
        rungs.append(Rung(RungKind.LABEL, {"label": label}))
    elif text:
        rungs.append(Rung(RungKind.LABEL, {"text": text}))

    if anchor_id and anchor_role and anchor_index is not None:
        rungs.append(
            Rung(
                RungKind.ANCHORED,
                {
                    "anchor_id": anchor_id,
                    "role": anchor_role,
                    "index": int(anchor_index),
                },
            )
        )

    if dom_path:
        rungs.append(Rung(RungKind.DOM_PATH, {"css": dom_path}))

    if point is not None:
        rungs.append(Rung(RungKind.COORDINATES, {"x": point[0], "y": point[1]}))

    if not rungs:
        raise ValueError(
            f"no usable locator for role={role!r} name={name!r}: refusing to "
            "record a step that can never be replayed"
        )

    return LocatorLadder(
        frame_path=frame_path,
        rungs=tuple(rungs),
        recorded_role=role,
        recorded_name=name,
    )
