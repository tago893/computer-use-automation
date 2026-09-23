"""The surface seam itself: conformance, and the frame-pairing fallback.

No browser needed. These are the tests that keep the seam honest.

A note on why the conformance test is written the way it is. ``isinstance(x, Surface)``
against a ``runtime_checkable`` Protocol only checks that the *named attributes
exist* -- not their signatures, parameters, or return types. An implementation whose
``act()`` took no arguments would still pass it. Since this project runs no static
type checker, an ``isinstance`` assertion alone would be close to worthless as a
guarantee while reading like a strong one. So signatures are compared explicitly.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

import pytest

from surfaces.ax import _pair_siblings
from surfaces.desktop import DesktopSurface, DesktopSurfaceNotImplemented
from surfaces.protocol import Surface

#: The seam's methods. Named explicitly so that *adding* a method to the protocol
#: without implementing it everywhere fails here.
SEAM_METHODS = ("observe", "act", "resolve", "screenshot", "close")


def _implementations() -> list[type]:
    """Every type claiming to be a Surface.

    ``WebSurface`` is imported lazily so this module still runs if Playwright is
    unavailable -- the desktop half of the seam must not need a browser.
    """
    from surfaces.web import WebSurface

    return [WebSurface, DesktopSurface]


@pytest.mark.parametrize("implementation", _implementations())
def test_implementation_exposes_every_seam_method(implementation: type) -> None:
    for method in SEAM_METHODS:
        assert callable(
            getattr(implementation, method, None)
        ), f"{implementation.__name__} is missing {method}()"


@pytest.mark.parametrize("implementation", _implementations())
@pytest.mark.parametrize("method", SEAM_METHODS)
def test_implementation_signature_matches_the_protocol(
    implementation: type, method: str
) -> None:
    """Signature-level conformance, which ``isinstance`` does not check."""
    expected = inspect.signature(getattr(Surface, method))
    actual = inspect.signature(getattr(implementation, method))

    assert [p.name for p in actual.parameters.values()] == [
        p.name for p in expected.parameters.values()
    ], f"{implementation.__name__}.{method} parameter names diverged from the seam"

    assert [p.kind for p in actual.parameters.values()] == [
        p.kind for p in expected.parameters.values()
    ], f"{implementation.__name__}.{method} parameter kinds diverged from the seam"

    assert actual.return_annotation == expected.return_annotation, (
        f"{implementation.__name__}.{method} returns "
        f"{actual.return_annotation!r}, seam declares {expected.return_annotation!r}"
    )


def test_protocol_has_no_query_method() -> None:
    """A deliberate omission, asserted so it cannot be added casually.

    If callers could ask the surface arbitrary questions in the surface's own
    language, the seam would leak and the desktop story would quietly die.
    """
    public = {
        name
        for name in dir(Surface)
        if not name.startswith("_") and callable(getattr(Surface, name, None))
    }
    assert public == set(SEAM_METHODS), (
        f"the seam's surface area changed: {public ^ set(SEAM_METHODS)}"
    )


def test_desktop_stub_refuses_loudly_rather_than_faking_success() -> None:
    desktop = DesktopSurface(window_title="Servicing Console")
    for method in ("observe", "screenshot"):
        with pytest.raises(DesktopSurfaceNotImplemented, match="documented stub"):
            getattr(desktop, method)()
    desktop.close()  # must be safe


# --- frame pairing ----------------------------------------------------------


@dataclass(frozen=True)
class FakeFrame:
    """Stands in for a Playwright ``Frame``; pairing reads only ``name``."""

    name: str


def _cdp(name: str | None) -> dict[str, Any]:
    return {"frame": {"id": f"id-{name}", "name": name, "url": f"/{name}"}}


def test_named_frames_pair_by_name_not_position() -> None:
    """Order must not matter when names are available and unique."""
    cdp = [_cdp("navframe"), _cdp("mainframe")]
    # Playwright hands them back in the opposite order.
    playwright = [FakeFrame("mainframe"), FakeFrame("navframe")]

    paired = _pair_siblings(cdp, playwright)  # type: ignore[arg-type]
    assert [(c["frame"]["name"], f.name) for c, f in paired] == [
        ("navframe", "navframe"),
        ("mainframe", "mainframe"),
    ]


def test_unnamed_frames_fall_back_to_document_order() -> None:
    """The fallback path the real app never exercises, since all its frames are named."""
    cdp = [_cdp(None), _cdp(None)]
    playwright = [FakeFrame(""), FakeFrame("")]

    paired = _pair_siblings(cdp, playwright)  # type: ignore[arg-type]
    assert [f for _, f in paired] == playwright, "must pair positionally, in order"


def test_duplicate_names_fall_back_to_document_order() -> None:
    """Two siblings sharing a name cannot be told apart by name, so position wins."""
    cdp = [_cdp("dup"), _cdp("dup")]
    first, second = FakeFrame("dup"), FakeFrame("dup")

    paired = _pair_siblings(cdp, [first, second])  # type: ignore[arg-type]
    assert [f for _, f in paired] == [first, second]


def test_mixed_named_and_unnamed_frames() -> None:
    cdp = [_cdp(None), _cdp("mainframe"), _cdp(None)]
    blank_a, main, blank_b = FakeFrame(""), FakeFrame("mainframe"), FakeFrame("")

    paired = _pair_siblings(cdp, [blank_a, main, blank_b])  # type: ignore[arg-type]
    by_name = {c["frame"]["name"]: f for c, f in paired if c["frame"]["name"]}
    assert by_name["mainframe"] is main, "the named frame must still pair by name"

    unnamed = [f for c, f in paired if not c["frame"]["name"]]
    assert unnamed == [blank_a, blank_b], "unnamed frames keep document order"


def test_more_cdp_frames_than_playwright_frames_yields_none() -> None:
    """An unpairable frame must be reported as unpaired, never guessed.

    ``frame_refs`` skips these, so a frame we could read but not act in is never
    presented to the model as actionable.
    """
    paired = _pair_siblings(
        [_cdp("a"), _cdp("b")], [FakeFrame("a")]  # type: ignore[arg-type]
    )
    assert paired[0][1] is not None
    assert paired[1][1] is None
