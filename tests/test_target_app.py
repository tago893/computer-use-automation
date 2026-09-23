"""Target app behavior, especially the outcome-vs-failure distinction.

These tests exist to pin down the thing the brief calls the most common design
mistake on this project: a business outcome and a failure must be
*distinguishable from the surface itself*, not by guesswork later. If the app ever
starts returning 500 for "no such member", replay's taxonomy becomes untestable,
and these tests are what catches that.
"""

from __future__ import annotations

import pytest

from target_app import fields
from target_app.data import ABSENT_MEMBER_ID, MEMBERS
from target_app.faults import FaultMode, FaultState
from target_app.tenants import MERIDIAN, NORTHGATE, TENANTS, UnknownTenant, get_tenant


# --- tenants ---------------------------------------------------------------


def test_both_tenants_registered() -> None:
    assert set(TENANTS) == {"meridian", "northgate"}


def test_unknown_tenant_raises_at_the_boundary() -> None:
    with pytest.raises(UnknownTenant):
        get_tenant("does-not-exist")


def test_tenants_differ_in_the_ways_replay_must_survive() -> None:
    """The variation has to be real, or the heterogeneity story is theatre."""
    assert MERIDIAN.label_member_id != NORTHGATE.label_member_id
    assert MERIDIAN.extra_table_nesting != NORTHGATE.extra_table_nesting
    assert NORTHGATE.disclosure_interstitial and not MERIDIAN.disclosure_interstitial
    assert MERIDIAN.class_prefix != NORTHGATE.class_prefix


# --- fault state ------------------------------------------------------------


def test_unknown_fault_mode_is_not_a_fault() -> None:
    assert FaultMode.parse("nonsense") is FaultMode.NONE
    assert FaultMode.parse(None) is FaultMode.NONE
    assert FaultMode.parse("  TIMEOUT ") is FaultMode.TIMEOUT


def test_fault_state_consumes_down_to_inactive() -> None:
    state = FaultState(mode=FaultMode.SLOW, remaining=2)
    assert state.active

    once = state.consumed()
    assert once.remaining == 1 and once.active
    # Immutability: consuming returns a new state, the original is untouched.
    assert state.remaining == 2

    twice = once.consumed()
    assert not twice.active and twice.mode is FaultMode.NONE


# --- flow -------------------------------------------------------------------


def _login(client, tenant=MERIDIAN) -> None:
    response = client.post(
        f"/t/{tenant.tenant_id}/login",
        data={fields.LOGIN_USER: tenant.username, fields.LOGIN_PASS: tenant.password},
        follow_redirects=False,
    )
    assert response.status_code == 302, "valid credentials must sign in"


def test_bad_credentials_are_rejected_with_401(client) -> None:
    response = client.post(
        "/t/meridian/login",
        data={fields.LOGIN_USER: "wrong", fields.LOGIN_PASS: "wrong"},
    )
    assert response.status_code == 401
    assert b"Sign-in failed" in response.data


def test_panes_require_authentication(client) -> None:
    response = client.get("/t/meridian/pane/search")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_happy_path_reaches_confirmation(client) -> None:
    _login(client)
    member = MEMBERS["M-10001"]
    account = member.sub_accounts[0]

    results = client.post(
        "/t/meridian/pane/results", data={fields.MEMBER_ID: member.member_id}
    )
    assert results.status_code == 200
    assert member.full_name.encode() in results.data

    detail = client.get(f"/t/meridian/pane/member/{member.member_id}")
    assert detail.status_code == 200

    confirm = client.post(
        "/t/meridian/pane/confirm",
        data={
            fields.MEMBER_ID_HIDDEN: member.member_id,
            fields.ACCOUNT_ID_HIDDEN: account.account_id,
        },
    )
    assert confirm.status_code == 200
    assert b'data-outcome="CONFIRMED"' in confirm.data


# --- the distinction that matters ------------------------------------------


def test_absent_member_is_an_answer_not_an_error(client) -> None:
    """HTTP 200, a stable outcome code, and no error marker."""
    _login(client)
    response = client.post(
        "/t/meridian/pane/results", data={fields.MEMBER_ID: ABSENT_MEMBER_ID}
    )
    assert response.status_code == 200
    assert b'data-outcome="MEMBER_NOT_FOUND"' in response.data
    assert b"No such member" in response.data
    assert b"data-error" not in response.data


def test_injected_500_is_an_error_not_an_answer(client) -> None:
    _login(client)
    response = client.post(
        "/t/meridian/pane/results?inject=500",
        data={fields.MEMBER_ID: "M-10001"},
    )
    assert response.status_code == 500
    assert b'data-error="500"' in response.data
    assert b"data-outcome" not in response.data


def test_empty_input_is_rejected_as_validation(client) -> None:
    _login(client)
    response = client.post("/t/meridian/pane/results", data={fields.MEMBER_ID: ""})
    assert response.status_code == 200
    assert b"is required" in response.data


# --- injected faults --------------------------------------------------------


def test_timeout_fault_bounces_to_login(client) -> None:
    _login(client)
    response = client.get("/t/meridian/pane/search?inject=timeout")
    assert response.status_code == 302
    assert "expired=1" in response.headers["Location"]


def test_interstitial_fault_blocks_then_clears(client) -> None:
    _login(client)
    blocked = client.get("/t/meridian/pane/search?inject=interstitial")
    assert b'data-interstitial="security"' in blocked.data

    # Acknowledging returns to the pane it interrupted.
    acknowledged = client.post(
        "/t/meridian/pane/acknowledge",
        data={fields.CONTINUE_TO: "/t/meridian/pane/search"},
    )
    assert acknowledged.status_code == 302
    assert acknowledged.headers["Location"].endswith("/t/meridian/pane/search")


def test_acknowledge_refuses_offsite_redirect(client) -> None:
    """A form field must never be able to steer the browser off this app."""
    _login(client)
    response = client.post(
        "/t/meridian/pane/acknowledge",
        data={fields.CONTINUE_TO: "https://evil.example/harvest"},
    )
    assert response.status_code == 302
    assert "evil.example" not in response.headers["Location"]


def test_sticky_fault_arms_via_control_plane(client) -> None:
    _login(client)
    armed = client.post("/__control__/inject", json={"mode": "500", "count": 1})
    assert armed.get_json() == {"ok": True, "mode": "500", "remaining": 1}

    # Fires once...
    assert client.get("/t/meridian/pane/search").status_code == 500
    # ...then the surface is healthy again.
    assert client.get("/t/meridian/pane/search").status_code == 200


def test_northgate_interposes_a_disclosure_meridian_never_shows(client) -> None:
    """The extra step an artifact recorded on meridian has never seen."""
    _login(client, NORTHGATE)
    response = client.get("/t/northgate/pane/member/M-10001")
    assert b'data-interstitial="disclosure"' in response.data

    _login(client, MERIDIAN)
    clean = client.get("/t/meridian/pane/member/M-10001")
    assert b"data-interstitial" not in clean.data
