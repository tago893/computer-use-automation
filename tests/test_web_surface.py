"""``WebSurface`` against the real target app in a real browser.

These are the tests that prove the Phase 2 claim rather than asserting it: that a
hostile, deeply framed, test-hook-free surface can be perceived as ordinal-indexed
accessibility nodes, acted on by ordinal, and re-found later by ladder alone.

**Every test drives itself to the state it needs.** The browser is shared for
speed, but no test depends on another having run first -- an earlier version did,
and `pytest tests/test_web_surface.py::test_recorded_ladder_is_validated_and_durable`
failed in isolation while passing in a full run. A suite that only passes in
file-declaration order is a trap: it breaks under `-k`, under `--last-failed`, and
under any parallel runner that distributes by anything other than file.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from surfaces import ActionKind, RungKind, WebSurface
from surfaces.locators import LocatorLadder, Rung
from surfaces.models import Action, Observation, OrdinalNotFound
from target_app.tenants import MERIDIAN, Tenant

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def surface() -> Iterator[WebSurface]:
    """One headless browser for the module. Carries no page state between tests."""
    web = WebSurface(headless=True)
    web.start("about:blank")
    try:
        yield web
    finally:
        web.close()


# --- helpers ----------------------------------------------------------------


def _ordinal(observation: Observation, role: str, name: str) -> int:
    """Find one node by role and accessible name, insisting it is unambiguous."""
    hits = [
        node
        for node in observation.nodes
        if node.role == role and node.name == name
    ]
    assert hits, (
        f"no {role} named {name!r}. Saw:\n"
        + "\n".join(f"  {node.render()}" for node in observation.nodes)
    )
    assert len(hits) == 1, f"{role} {name!r} was ambiguous ({len(hits)} matches)"
    return hits[0].ordinal


def _type_into(surface: WebSurface, role: str, name: str, text: str) -> None:
    observation = surface.observe()
    result = surface.act(
        Action(
            kind=ActionKind.TYPE,
            ordinal=_ordinal(observation, role, name),
            text=text,
        )
    )
    assert result.ok, result.detail


def _click(surface: WebSurface, role: str, name: str) -> None:
    observation = surface.observe()
    result = surface.act(
        Action(kind=ActionKind.CLICK, ordinal=_ordinal(observation, role, name))
    )
    assert result.ok, result.detail


def at_login(
    surface: WebSurface, live_app: str, tenant: Tenant = MERIDIAN
) -> Observation:
    """Put the surface on a fresh login page, whatever state it was in."""
    result = surface.act(
        Action(
            kind=ActionKind.NAVIGATE,
            url=f"{live_app}/t/{tenant.tenant_id}/login",
        )
    )
    assert result.ok, result.detail
    return surface.observe()


def at_console(
    surface: WebSurface, live_app: str, tenant: Tenant = MERIDIAN
) -> Observation:
    """Sign in, landing on the framed console."""
    at_login(surface, live_app, tenant)
    _type_into(surface, "textbox", "Operator ID", tenant.username)
    _type_into(surface, "textbox", "Passphrase", tenant.password)
    _click(surface, "button", "Sign In")
    observation = surface.observe()
    assert "/console" in observation.url, f"login did not land: {observation.url}"
    return observation


def at_member_detail(
    surface: WebSurface,
    live_app: str,
    tenant: Tenant = MERIDIAN,
    member_id: str = "M-10001",
) -> Observation:
    """Drive the whole flow to the member record, three frame levels deep."""
    at_console(surface, live_app, tenant)
    _type_into(surface, "textbox", tenant.label_member_id, member_id)
    _click(surface, "button", tenant.label_submit_search)
    _click(surface, "link", "Open Member")
    observation = surface.observe()
    assert any(
        len(node.frame_path) > 1 for node in observation.nodes
    ), "nested products frame never appeared"
    return observation


# --- perception -------------------------------------------------------------


def test_observation_exposes_labelled_controls_with_no_test_hooks(
    surface: WebSurface, live_app: str
) -> None:
    """The app has no test ids at all; ARIA alone must be enough."""
    observation = at_login(surface, live_app)
    assert "/login" in observation.url

    roles = {node.role for node in observation.nodes}
    assert "textbox" in roles and "button" in roles

    # The accessible names come from real <label for=...> associations.
    assert _ordinal(observation, "textbox", "Operator ID") >= 0
    assert _ordinal(observation, "textbox", "Passphrase") >= 0


def test_ordinals_are_dense_and_unique(surface: WebSurface, live_app: str) -> None:
    observation = at_login(surface, live_app)
    ordinals = [node.ordinal for node in observation.nodes]
    assert len(ordinals) == len(set(ordinals)), "ordinals must be unique"
    assert ordinals == sorted(ordinals), "ordinals must be in document order"


def test_ordinals_are_unique_across_frames(surface: WebSurface, live_app: str) -> None:
    """Regression: each frame used to number from ``len(nodes)``.

    Text nodes that cannot take a stamp were dropped *after* numbering, so the
    counter lagged and the next frame reused ordinals already handed out -- the
    console showed ``[5] link 'Sign Out'`` and ``[5] heading 'Member Search'``
    side by side. Clicking ``[5]`` could then hit either element.
    """
    observation = at_member_detail(surface, live_app)
    ordinals = [node.ordinal for node in observation.nodes]
    assert len(ordinals) == len(set(ordinals)), observation.render()
    assert ordinals == sorted(ordinals), "ordinals must follow frame-tree order"


def test_actions_on_each_frame_hit_the_element_they_name(
    surface: WebSurface, live_app: str
) -> None:
    """The consequence of a collision, asserted directly: the ladder recorded
    for an action must describe the node the ordinal named."""
    observation = at_console(surface, live_app)
    target = next(n for n in observation.nodes if n.role == "textbox")
    result = surface.act(
        Action(kind=ActionKind.TYPE, ordinal=target.ordinal, text="M-10001")
    )
    assert result.ok, result.detail
    assert result.ladder is not None
    assert result.ladder.recorded_name == target.name
    assert result.ladder.frame_path == target.frame_path


def test_business_outcome_text_is_visible_to_the_model(
    surface: WebSurface, live_app: str
) -> None:
    """Regression: bare text nodes cannot carry a DOM attribute, so they failed
    to stamp and vanished from the observation. "No such member." is exactly
    such a node -- the agent could not see the answer it was looking for."""
    at_console(surface, live_app)
    _type_into(surface, "textbox", MERIDIAN.label_member_id, "M-99999")
    _click(surface, "button", MERIDIAN.label_submit_search)
    rendered = surface.observe().render()
    assert "No such member." in rendered, rendered
    assert "MEMBER_NOT_FOUND" in rendered, rendered


def test_text_nodes_are_readable_through_their_host_element(
    surface: WebSurface, live_app: str
) -> None:
    observation = at_console(surface, live_app)
    _type_into(surface, "textbox", MERIDIAN.label_member_id, "M-99999")
    _click(surface, "button", MERIDIAN.label_submit_search)
    observation = surface.observe()
    text = next(n for n in observation.nodes if n.name == "No such member.")
    result = surface.act(Action(kind=ActionKind.READ, ordinal=text.ordinal))
    assert result.ok, result.detail
    assert "No such member." in result.read_value


def test_stale_ordinal_raises_instead_of_hitting_the_wrong_element(
    surface: WebSurface, live_app: str
) -> None:
    """The failure mode that silently corrupts automation must be loud here."""
    at_login(surface, live_app)
    with pytest.raises(OrdinalNotFound, match="not in the last observation"):
        surface.act(Action(kind=ActionKind.CLICK, ordinal=9_999))


def test_tenant_labels_differ_in_what_perception_reports(
    surface: WebSurface, live_app: str
) -> None:
    """The same field, two tenants, two accessible names -- the heterogeneity case."""
    from target_app.tenants import NORTHGATE

    meridian_view = at_console(surface, live_app, MERIDIAN)
    assert _ordinal(meridian_view, "textbox", MERIDIAN.label_member_id) >= 0

    northgate_view = at_console(surface, live_app, NORTHGATE)
    assert _ordinal(northgate_view, "textbox", NORTHGATE.label_member_id) >= 0

    names = {node.name for node in northgate_view.nodes}
    assert MERIDIAN.label_member_id not in names, (
        "northgate must not expose meridian's label; that would make the "
        "cross-tenant test vacuous"
    )


# --- acting across the frame tree ------------------------------------------


def test_perception_reaches_every_frame_level(
    surface: WebSurface, live_app: str
) -> None:
    """Three frame levels deep, reached without a single CSS selector."""
    observation = at_console(surface, live_app)

    frames = {node.frame_path for node in observation.nodes}
    assert ("navframe",) in frames, f"nav frame not perceived; saw {frames}"
    assert ("mainframe",) in frames, f"main frame not perceived; saw {frames}"

    _type_into(surface, "textbox", MERIDIAN.label_member_id, "M-10001")
    _click(surface, "button", MERIDIAN.label_submit_search)

    observation = surface.observe()
    names = {node.name for node in observation.nodes}
    assert "Ada Sable" in names, f"member row missing; saw {sorted(names)[:25]}"

    _click(surface, "link", "Open Member")
    observation = surface.observe()
    deep = [
        node
        for node in observation.nodes
        if node.frame_path == ("mainframe", "detailframe")
    ]
    assert deep, (
        "nested frame never perceived; frames seen: "
        f"{ {node.frame_path for node in observation.nodes} }"
    )


def test_recorded_ladder_is_validated_and_durable(
    surface: WebSurface, live_app: str
) -> None:
    """The ladder recorded for an action must actually re-find that element.

    This is the core Phase 2 guarantee. Whatever the artifact stores, replay can
    walk it later with no ordinals and no observation.
    """
    observation = at_member_detail(surface, live_app)
    target = next(
        (
            node
            for node in observation.nodes
            if node.role == "link"
            and node.name.startswith(MERIDIAN.label_subaccount)
        ),
        None,
    )
    assert target is not None, (
        "sub-account link not found; saw "
        f"{[n.render() for n in observation.nodes if n.role == 'link']}"
    )

    result = surface.act(Action(kind=ActionKind.READ, ordinal=target.ordinal))
    assert result.ok, result.detail
    assert result.ladder is not None, "acting must produce a recordable ladder"

    ladder = result.ladder
    assert ladder.frame_path == ("mainframe", "detailframe")
    assert ladder.rungs, "validation must not strip every rung"

    # And the ladder resolves on its own, which is what replay will do.
    resolution = surface.resolve(ladder)
    assert resolution.found, resolution.describe()
    assert resolution.matched is ladder.best.kind


def test_anchored_rung_is_recorded_for_the_nested_grid(
    surface: WebSurface, live_app: str
) -> None:
    """Rung 3 exists where it is most needed: rows that look alike."""
    observation = at_member_detail(surface, live_app)
    second = next(
        node
        for node in observation.nodes
        if node.role == "link" and node.name.endswith("SA-4472")
    )
    result = surface.act(Action(kind=ActionKind.READ, ordinal=second.ordinal))
    assert result.ok and result.ladder is not None

    anchored = result.ladder.rung(RungKind.ANCHORED)
    assert anchored is not None, (
        "expected an anchored rung; got " + result.ladder.describe()
    )
    assert anchored.params["anchor_id"] == "pnlProducts"
    # Second row, so index 1 -- and validation confirmed it hits *this* element.
    assert anchored.params["index"] == 1


def test_ambiguous_rung_is_refused_rather_than_guessed(
    surface: WebSurface, live_app: str
) -> None:
    """Both product rows share visible text. Refusing is the only safe answer.

    Silently taking the first of several matches is how automation transfers money
    out of the wrong account, so ambiguity is treated as a failed rung.
    """
    at_member_detail(surface, live_app)

    ambiguous = LocatorLadder(
        frame_path=("mainframe", "detailframe"),
        rungs=(Rung(RungKind.LABEL, {"text": MERIDIAN.label_subaccount}),),
    )
    resolution = surface.resolve(ambiguous)
    assert not resolution.found, "an ambiguous locator must not resolve"
    assert "ambiguous" in resolution.describe()


def test_ladder_for_a_missing_element_does_not_resolve(
    surface: WebSurface, live_app: str
) -> None:
    """A ladder must fail honestly rather than match something approximate."""
    at_console(surface, live_app)

    absent = LocatorLadder(
        frame_path=("mainframe",),
        rungs=(
            Rung(RungKind.ROLE_NAME, {"role": "button", "name": "Delete Everything"}),
        ),
    )
    resolution = surface.resolve(absent)
    assert not resolution.found
    assert "no match" in resolution.describe()


def test_ladder_in_an_absent_frame_reports_the_frame(
    surface: WebSurface, live_app: str
) -> None:
    at_console(surface, live_app)

    ladder = LocatorLadder(
        frame_path=("nosuchframe",),
        rungs=(Rung(RungKind.ROLE_NAME, {"role": "button", "name": "Sign In"}),),
    )
    resolution = surface.resolve(ladder)
    assert not resolution.found
    assert "absent" in resolution.describe()


# --- evidence ---------------------------------------------------------------


def test_screenshot_returns_a_png(surface: WebSurface, live_app: str) -> None:
    at_login(surface, live_app)
    image = surface.screenshot()
    assert image.startswith(b"\x89PNG\r\n\x1a\n"), "must be a real PNG"
    assert len(image) > 1_000
