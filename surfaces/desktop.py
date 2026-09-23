"""``DesktopSurface`` -- the seam's second implementation, deliberately unbuilt.

This exists to make one claim falsifiable: that the perception model chosen here
is not browser-specific. The brief asks for an approach that would still work on a
surface with no clean DOM, and the honest way to show that is a stub whose
signature already fits, with the real integration named precisely enough that a
reader can check the reasoning.

**Why the mapping works.** The ordinal-indexed accessibility tree translates
directly:

=====================  ==================================================
This project            Windows UI Automation
=====================  ==================================================
AX node                 ``AutomationElement``
``role``                ``ControlType`` (Button, Edit, Hyperlink, ...)
``name``                ``Name`` property (from ``AutomationProperties``)
frame path              window / pane ancestry
rung 1 role+name        ``AndCondition(ControlType, Name)`` via ``FindAll``
rung 3 anchored         ``AutomationId`` ancestor + typed child index
rung 4 DOM path         no analogue; omitted rather than faked
rung 5 coordinates      ``ClickablePoint``
=====================  ==================================================

Rung 1 -- the rung that matters -- is *more* reliable on Windows than in a browser,
because ``AutomationId`` and ``Name`` are set by the application rather than
inferred from markup. Rung 4 has no analogue and would simply be absent from a
desktop ladder, which the ladder already tolerates: rungs are emitted only when
their inputs exist.

**What is actually missing** is plumbing, not design: a ``pywinauto`` or
``uiautomation`` dependency, a tree walk, and a translation of five action kinds
into UIA patterns (``InvokePattern`` for click, ``ValuePattern`` for type). That is
a day of work with no new decisions in it, which is exactly why it was cut -- see
REPORT.md, Cuts.
"""

from __future__ import annotations

from .locators import LocatorLadder, Resolution
from .models import Action, ActResult, Observation


class DesktopSurfaceNotImplemented(NotImplementedError):
    """Raised on any attempt to actually drive a desktop surface.

    A loud failure rather than a silent no-op: a stub that pretended to work would
    make the seam look verified when it is not.
    """


class DesktopSurface:
    """Satisfies ``Surface`` structurally; refuses to pretend it is implemented."""

    def __init__(self, *, window_title: str) -> None:
        self.window_title = window_title

    def _refuse(self, what: str) -> "DesktopSurfaceNotImplemented":
        return DesktopSurfaceNotImplemented(
            f"DesktopSurface.{what} is a documented stub. The seam is real and its "
            "UI Automation mapping is in this module's docstring; the integration "
            "itself was cut. Use WebSurface."
        )

    def observe(self) -> Observation:
        raise self._refuse("observe")

    def act(self, action: Action) -> ActResult:
        raise self._refuse("act")

    def resolve(self, ladder: LocatorLadder) -> Resolution:
        raise self._refuse("resolve")

    def screenshot(self) -> bytes:
        raise self._refuse("screenshot")

    def close(self) -> None:
        """Safe to call: there is nothing to release."""
        return None
