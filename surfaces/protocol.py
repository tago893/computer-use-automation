"""The surface seam.

Everything above this line -- the discovery agent, the replay executor -- is
written against ``Surface`` and never against Playwright. That is what makes the
claim "this design would work on a Windows desktop app" checkable rather than
rhetorical: a desktop implementation has to satisfy this protocol and nothing
else.

The protocol is four methods wide on purpose. Each one earns its place:

``observe``   perception. Returns ordinals, never selectors.
``act``       action, addressed by ordinal from the most recent observation.
``resolve``   ladder -> element, reporting which rung matched. Replay's entry point.
``screenshot`` evidence. Never a perception channel -- the agent does not see pixels.

A deliberate omission: there is no ``find`` or ``query`` method. If the layers above
could ask the surface arbitrary questions in the surface's own language, the seam
would leak and the desktop story would quietly die.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .locators import LocatorLadder, Resolution
from .models import Action, ActResult, Observation


@runtime_checkable
class Surface(Protocol):
    """A driveable surface: a browser page, or a desktop window."""

    def observe(self) -> Observation:
        """Snapshot the surface as ordinal-indexed accessibility nodes.

        Each call renumbers ordinals from zero. Ordinals are valid only against the
        observation that produced them; acting on a stale ordinal must raise rather
        than silently hit the wrong element.
        """
        ...

    def act(self, action: Action) -> ActResult:
        """Perform an action addressed by ordinal.

        A failure to perform the action is reported as ``ActResult(ok=False)``, not
        raised, so the caller can decide whether to retry, escalate, or record a
        business outcome. Only a surface that has become undriveable raises
        ``SurfaceError``.
        """
        ...

    def resolve(self, ladder: LocatorLadder) -> Resolution:
        """Walk a ladder against the live surface, reporting which rung matched.

        Used by deterministic replay, which has no ordinals because it has no
        observation from the discovery run -- only the recorded ladders.
        """
        ...

    def screenshot(self) -> bytes:
        """Capture PNG evidence of the current state."""
        ...

    def close(self) -> None:
        """Release the underlying resources. Must be idempotent."""
        ...
