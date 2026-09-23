"""The surface seam: how this system perceives and acts on any UI.

Import from here rather than from submodules.

``WebSurface`` is resolved lazily (PEP 562) so that importing this package does not
import Playwright. That is not a micro-optimisation: this package's claim is that
the perception model is not browser-specific, and a package that cannot be imported
without a browser driver installed would quietly contradict it. ``surfaces.web`` is
the only module that knows what a browser is, and nothing else here pulls it in.
"""

from typing import TYPE_CHECKING, Any

from .desktop import DesktopSurface, DesktopSurfaceNotImplemented
from .locators import (
    LocatorLadder,
    Resolution,
    Rung,
    RungKind,
    build_ladder,
)
from .models import (
    ACTIONABLE_ROLES,
    READABLE_ROLES,
    Action,
    ActionKind,
    ActResult,
    AXNode,
    Observation,
    OrdinalNotFound,
    SurfaceError,
)
from .protocol import Surface

if TYPE_CHECKING:  # import for type checkers only; never at runtime
    from .web import WebSurface

_LAZY: dict[str, tuple[str, str]] = {"WebSurface": (".web", "WebSurface")}


def __getattr__(name: str) -> Any:
    """Resolve ``WebSurface`` on first use, importing Playwright only then."""
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from importlib import import_module

    module = import_module(target[0], __name__)
    value = getattr(module, target[1])
    globals()[name] = value  # cache, so this runs once
    return value


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    "ACTIONABLE_ROLES",
    "READABLE_ROLES",
    "AXNode",
    "Action",
    "ActionKind",
    "ActResult",
    "DesktopSurface",
    "DesktopSurfaceNotImplemented",
    "LocatorLadder",
    "Observation",
    "OrdinalNotFound",
    "Resolution",
    "Rung",
    "RungKind",
    "Surface",
    "SurfaceError",
    "WebSurface",
    "build_ladder",
]
