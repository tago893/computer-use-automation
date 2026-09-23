"""Locator ladder construction and ordering.

Pure unit tests -- no browser. The ladder is the recording format that artifacts
depend on, so its ordering guarantees are worth pinning down independently of
whether Playwright happens to be installed.
"""

from __future__ import annotations

import pytest

from surfaces.locators import (
    LocatorLadder,
    Resolution,
    Rung,
    RungKind,
    build_ladder,
)


# --- rung ordering ----------------------------------------------------------


def test_rungs_are_ordered_most_durable_first() -> None:
    assert RungKind.ROLE_NAME < RungKind.LABEL < RungKind.ANCHORED
    assert RungKind.ANCHORED < RungKind.DOM_PATH < RungKind.COORDINATES


def test_durability_and_fragility_classification() -> None:
    assert RungKind.ROLE_NAME.durable and RungKind.LABEL.durable
    assert not RungKind.ANCHORED.durable
    assert RungKind.DOM_PATH.fragile and RungKind.COORDINATES.fragile
    assert not RungKind.ROLE_NAME.fragile


def test_ladder_orders_rungs_regardless_of_construction_order() -> None:
    """Callers must not be able to change priority by reordering arguments."""
    ladder = LocatorLadder(
        rungs=(
            Rung(RungKind.COORDINATES, {"x": 10.0, "y": 20.0}),
            Rung(RungKind.ROLE_NAME, {"role": "button", "name": "Search"}),
            Rung(RungKind.DOM_PATH, {"css": "div > button"}),
        )
    )
    assert [rung.kind for rung in ladder.ordered] == [
        RungKind.ROLE_NAME,
        RungKind.DOM_PATH,
        RungKind.COORDINATES,
    ]
    assert ladder.best.kind is RungKind.ROLE_NAME


def test_ladder_with_no_rungs_is_rejected() -> None:
    with pytest.raises(ValueError, match="no rungs"):
        LocatorLadder(rungs=())


# --- construction -----------------------------------------------------------


def test_build_ladder_emits_every_available_rung() -> None:
    ladder = build_ladder(
        role="link",
        name="Open Sub-Account SA-4471",
        frame_path=("mainframe", "detailframe"),
        label="",
        text="Open Sub-Account",
        anchor_id="pnlMember",
        anchor_role="link",
        anchor_index=2,
        dom_path="table > tr:nth-of-type(3) > td > a",
        point=(410.5, 232.0),
    )
    assert [rung.kind for rung in ladder.ordered] == [
        RungKind.ROLE_NAME,
        RungKind.LABEL,
        RungKind.ANCHORED,
        RungKind.DOM_PATH,
        RungKind.COORDINATES,
    ]
    assert ladder.frame_path == ("mainframe", "detailframe")


def test_unknown_role_suppresses_rung_one_rather_than_emitting_it_broken() -> None:
    """CDP emits role names ARIA does not have; a broken rung is worse than none."""
    ladder = build_ladder(
        role="StaticText",
        name="No such member.",
        frame_path=(),
        text="No such member.",
    )
    assert ladder.rung(RungKind.ROLE_NAME) is None
    assert ladder.best.kind is RungKind.LABEL


def test_nameless_element_suppresses_rung_one() -> None:
    ladder = build_ladder(role="button", name="", frame_path=(), dom_path="form > button")
    assert ladder.rung(RungKind.ROLE_NAME) is None
    assert ladder.best.kind is RungKind.DOM_PATH


def test_label_wins_over_text_when_both_exist() -> None:
    """A programmatic label is stronger evidence than incidental visible text."""
    ladder = build_ladder(
        role="textbox",
        name="Member ID",
        frame_path=(),
        label="Member ID",
        text="whatever happened to be inside",
    )
    rung = ladder.rung(RungKind.LABEL)
    assert rung is not None and rung.params == {"label": "Member ID"}


def test_partial_anchor_information_is_not_emitted() -> None:
    """An anchored rung without an index cannot resolve, so it must not exist."""
    ladder = build_ladder(
        role="button",
        name="Search",
        frame_path=(),
        anchor_id="pnlSearch",
        anchor_role="button",
        anchor_index=None,
    )
    assert ladder.rung(RungKind.ANCHORED) is None


def test_element_with_no_handles_at_all_is_refused() -> None:
    """Refusing to record beats recording a step that can never be replayed."""
    with pytest.raises(ValueError, match="no usable locator"):
        build_ladder(role="generic", name="", frame_path=())


# --- resolution reporting ---------------------------------------------------


def test_resolution_on_best_rung_is_not_drift() -> None:
    resolution = Resolution(matched=RungKind.ROLE_NAME)
    assert resolution.found and not resolution.drifted


def test_resolution_on_lower_rung_is_reported_as_drift() -> None:
    """Found, but the surface moved -- worth a warning before it becomes a failure."""
    resolution = Resolution(
        matched=RungKind.ANCHORED,
        attempted=(
            (RungKind.ROLE_NAME, "no match"),
            (RungKind.LABEL, "ambiguous (3 matches)"),
        ),
    )
    assert resolution.found and resolution.drifted
    assert "drift" in resolution.describe()


def test_unresolved_ladder_reports_every_rung_it_tried() -> None:
    resolution = Resolution(
        matched=None,
        attempted=(
            (RungKind.ROLE_NAME, "no match"),
            (RungKind.DOM_PATH, "no match"),
        ),
    )
    assert not resolution.found
    described = resolution.describe()
    assert "unresolved" in described
    assert "ROLE_NAME" in described and "DOM_PATH" in described


# --- serialization ------------------------------------------------------------


def test_ladder_round_trips_through_json() -> None:
    import json

    from surfaces.locators import LocatorLadder

    ladder = build_ladder(
        role="textbox",
        name="Member ID",
        frame_path=("mainframe",),
        label="Member ID",
        anchor_id="pnlSearch",
        anchor_role="textbox",
        anchor_index=0,
        dom_path="form > input",
        point=(10.0, 20.5),
    )
    restored = LocatorLadder.from_dict(json.loads(json.dumps(ladder.to_dict())))
    assert restored == LocatorLadder(
        frame_path=ladder.frame_path,
        rungs=ladder.ordered,
        recorded_role=ladder.recorded_role,
        recorded_name=ladder.recorded_name,
    )


def test_serialized_rungs_are_named_not_numbered() -> None:
    """A recorded file must stay readable, and survive a rung renumbering."""
    ladder = build_ladder(role="button", name="Search", frame_path=())
    assert ladder.to_dict()["rungs"][0]["kind"] == "ROLE_NAME"


def test_unknown_rung_kind_is_rejected() -> None:
    from surfaces.locators import LocatorLadder

    with pytest.raises(ValueError, match="unknown rung kind"):
        LocatorLadder.from_dict({"rungs": [{"kind": "XPATH", "params": {}}]})
