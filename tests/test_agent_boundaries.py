"""The agent's boundaries: tool parsing, bindings, and the guard.

Everything the model sends is untrusted input. These tests pin down that a bad
call is rejected with a reason rather than raising or half-acting, that secrets
cannot reach a prompt or a recording, and that the guard refuses what it must.
"""

from __future__ import annotations

import pytest

from agent import tools
from agent.bindings import BindingError, Bindings
from agent.llm import ToolCall
from agent.tools import Intent, Outcome, ToolRejected
from policy.guard import ActionPolicy, Allowlist, RiskClass, classify
from surfaces.models import Action, ActionKind, AXNode

# --- tool parsing ---------------------------------------------------------------


def _parse(name: str, **arguments: object) -> Intent | ToolRejected:
    return tools.parse(ToolCall(name=name, arguments=arguments))


def test_click_parses_to_an_action() -> None:
    intent = _parse("click", ordinal=7, why="submit")
    assert isinstance(intent, Intent)
    assert intent.action == Action(ActionKind.CLICK, ordinal=7)
    assert intent.why == "submit"


@pytest.mark.parametrize("ordinal", [-1, "7", 7.5, None, True])
def test_bad_ordinals_are_rejected(ordinal: object) -> None:
    """``True`` is an int in Python; it must not silently address ordinal 1."""
    assert isinstance(_parse("click", ordinal=ordinal, why=""), ToolRejected)


def test_no_tool_call_is_a_rejection_not_a_crash() -> None:
    rejected = tools.parse(None)
    assert isinstance(rejected, ToolRejected)
    assert "exactly one tool" in rejected.problem


def test_unknown_tool_is_rejected() -> None:
    assert isinstance(_parse("execute_js", code="alert(1)"), ToolRejected)


def test_malformed_arguments_are_rejected() -> None:
    call = ToolCall(name="click", arguments={"__malformed__": "{ordinal:"})
    assert isinstance(tools.parse(call), ToolRejected)


def test_read_requires_a_snake_case_output_name() -> None:
    assert isinstance(_parse("read", ordinal=3, output_name="Balance!"), ToolRejected)
    intent = _parse("read", ordinal=3, output_name="balance", why="")
    assert isinstance(intent, Intent) and intent.output_name == "balance"


def test_business_outcome_requires_a_code() -> None:
    """The distinction between success and business outcome is enforced here."""
    assert isinstance(
        _parse("finish", outcome="business_outcome", summary="none"), ToolRejected
    )
    intent = _parse(
        "finish",
        outcome="business_outcome",
        outcome_code="MEMBER_NOT_FOUND",
        summary="no such member",
    )
    assert isinstance(intent, Intent)
    assert intent.outcome is Outcome.BUSINESS_OUTCOME
    assert intent.outcome_code == "MEMBER_NOT_FOUND"


def test_success_drops_any_outcome_code() -> None:
    intent = _parse("finish", outcome="success", outcome_code="X_Y_Z", summary="ok")
    assert isinstance(intent, Intent) and intent.outcome_code == ""


def test_escalate_requires_a_reason() -> None:
    assert isinstance(_parse("escalate", reason="  "), ToolRejected)


def test_every_offered_tool_has_a_parser_and_vice_versa() -> None:
    for spec in tools.TOOLS:
        rejected = tools.parse(ToolCall(spec.name, {}))
        assert not (
            isinstance(rejected, ToolRejected) and "unknown tool" in rejected.problem
        ), f"{spec.name} is offered to the model but cannot be parsed"


# --- bindings -------------------------------------------------------------------

BINDINGS = Bindings(
    inputs={"member_id": "M-10001"},
    secrets={"operator_passphrase": "demo-pass-1"},
)


def test_expand_substitutes_inputs_and_secrets() -> None:
    assert BINDINGS.expand("{{member_id}}") == "M-10001"
    assert BINDINGS.expand("{{ secret:operator_passphrase }}") == "demo-pass-1"


def test_expand_refuses_an_unbound_placeholder() -> None:
    with pytest.raises(BindingError, match="no input named 'account_id'"):
        BINDINGS.expand("{{account_id}}")
    with pytest.raises(BindingError, match="no secret"):
        BINDINGS.expand("{{secret:member_id}}")


def test_lift_records_placeholders_for_exact_literals_only() -> None:
    assert BINDINGS.lift("M-10001") == "{{member_id}}"
    assert BINDINGS.lift("demo-pass-1") == "{{secret:operator_passphrase}}"
    assert BINDINGS.lift("M-1000") == "M-1000", "partial matches must not lift"


def test_scrub_removes_secret_values() -> None:
    scrubbed = BINDINGS.scrub("value='demo-pass-1' and M-10001")
    assert "demo-pass-1" not in scrubbed
    assert "M-10001" in scrubbed, "inputs are not secret and stay visible"


def test_secret_values_never_appear_in_the_model_description() -> None:
    assert "demo-pass-1" not in BINDINGS.describe_for_model()
    assert "{{secret:operator_passphrase}}" in BINDINGS.describe_for_model()


def test_binding_names_are_validated() -> None:
    with pytest.raises(BindingError):
        Bindings(inputs={"Member ID": "x"})
    with pytest.raises(BindingError, match="both input and secret"):
        Bindings(inputs={"x_id": "1"}, secrets={"x_id": "2"})


# --- guard ----------------------------------------------------------------------

ALLOW = Allowlist.for_tenant("http://127.0.0.1:5099", "meridian")


@pytest.mark.parametrize(
    ("url", "permitted"),
    [
        ("http://127.0.0.1:5099/t/meridian/console", True),
        ("http://127.0.0.1:5099/t/northgate/console", False),  # other tenant
        ("http://127.0.0.1:5099/__control__/reset", False),  # fault plane
        ("http://127.0.0.1:5098/t/meridian/console", False),  # other origin
        ("https://evil.example/t/meridian/", False),
        ("javascript:alert(1)", False),
        ("file:///etc/passwd", False),
    ],
)
def test_allowlist(url: str, permitted: bool) -> None:
    assert ALLOW.permits(url) is permitted


@pytest.mark.parametrize(
    ("name", "risk"),
    [
        ("Confirm Transfer", RiskClass.RISKY),
        ("Authorize Transfer", RiskClass.RISKY),
        ("Search", RiskClass.SAFE),
        ("Open Sub-Account SA-4471", RiskClass.SAFE),
        ("Fee Reversal Queue", RiskClass.SAFE),  # "Reversal" is not "reverse"
        ("Sign In", RiskClass.SAFE),
    ],
)
def test_risk_classification(name: str, risk: RiskClass) -> None:
    node = AXNode(ordinal=1, role="button", name=name)
    assert classify(Action(ActionKind.CLICK, ordinal=1), node) is risk


def test_only_clicks_carry_risk() -> None:
    node = AXNode(ordinal=1, role="button", name="Confirm Transfer")
    assert classify(Action(ActionKind.READ, ordinal=1), node) is RiskClass.SAFE


def test_risky_click_is_blocked_unless_approved() -> None:
    node = AXNode(ordinal=1, role="button", name="Confirm Transfer")
    click = Action(ActionKind.CLICK, ordinal=1)

    blocked = ActionPolicy(ALLOW).check(click, node)
    assert not blocked.allowed and blocked.risk is RiskClass.RISKY
    assert "escalate" in blocked.reason

    assert ActionPolicy(ALLOW, allow_risky=True).check(click, node).allowed


def test_navigation_outside_the_allowlist_is_blocked() -> None:
    verdict = ActionPolicy(ALLOW).check(
        Action(ActionKind.NAVIGATE, url="http://127.0.0.1:5099/__control__/reset"),
        None,
    )
    assert not verdict.allowed


def test_location_check_tolerates_a_blank_start_page() -> None:
    policy = ActionPolicy(ALLOW)
    assert policy.check_location("about:blank").allowed
    assert not policy.check_location("https://evil.example/").allowed
