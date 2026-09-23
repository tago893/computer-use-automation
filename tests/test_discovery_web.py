"""The discovery loop on the real surface, with a scripted model.

This is the "run without live services" path proved end to end: a real browser, the
real hostile app, the real loop, guards, bindings and recorder. Only the choice of
action is canned -- and even that is made the way a model must make it, by reading
ordinals out of the prompt it was given.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent import Bindings, DiscoveryAgent, Limits, RunLog, RunStatus, ScriptedLLM
from agent.llm import ToolCall
from policy.guard import ActionPolicy, Allowlist
from surfaces import RungKind, WebSurface
from target_app.tenants import MERIDIAN

pytestmark = pytest.mark.integration

_NODE = re.compile(r"^\s*\[(\d+)\] (\S+) '(.*?)'", re.MULTILINE)


def on(tool: str, role: str, name: str, **arguments: object):
    """A script step that finds its ordinal in the prompt's observation."""

    def step(prompt: str) -> ToolCall:
        observation = prompt.split("CURRENT OBSERVATION:", 1)[1]
        for ordinal, node_role, node_name in _NODE.findall(observation):
            if node_role == role and node_name == name:
                return ToolCall(tool, {"ordinal": int(ordinal), "why": "script", **arguments})
        raise AssertionError(f"{role} {name!r} not in observation:\n{observation}")

    return step


def finish(**arguments: object) -> ToolCall:
    return ToolCall("finish", arguments)


LOGIN = [
    on("type_text", "textbox", "Operator ID", text="{{secret:operator_id}}"),
    on("type_text", "textbox", "Passphrase", text="{{secret:operator_passphrase}}"),
    on("click", "button", "Sign In"),
]


@pytest.fixture()
def surface(live_app: str) -> Iterator[WebSurface]:
    web = WebSurface(headless=True)
    web.start(f"{live_app}/t/meridian/login")
    try:
        yield web
    finally:
        web.close()


def _run(surface: WebSurface, live_app: str, script: list, member_id: str, log=None):
    agent = DiscoveryAgent(
        surface=surface,
        llm=ScriptedLLM(script),
        policy=ActionPolicy(Allowlist.for_tenant(live_app, "meridian")),
        bindings=Bindings(
            inputs={"member_id": member_id},
            secrets={
                "operator_id": MERIDIAN.username,
                "operator_passphrase": MERIDIAN.password,
            },
        ),
        limits=Limits(max_steps=15),
        log=log,
    )
    return agent.run("read the share savings balance", run_id="t")


def test_scripted_discovery_reaches_the_balance(
    surface: WebSurface, live_app: str, tmp_path: Path
) -> None:
    log = RunLog(tmp_path, scrub=lambda s: s.replace(MERIDIAN.password, "<X>"))
    result = _run(
        surface,
        live_app,
        [
            *LOGIN,
            on("type_text", "textbox", "Member ID", text="{{member_id}}"),
            on("click", "button", "Search"),
            on("click", "link", "Open Member"),
            on("click", "link", "Open Sub-Account SA-4471"),
            on("read", "cell", "$8,123.55", output_name="balance"),
            finish(outcome="success", summary="done"),
        ],
        "M-10001",
        log,
    )
    log.close()

    assert result.status is RunStatus.SUCCEEDED, result.reason
    assert result.outputs == {"balance": "$8,123.55"}
    # Every acting step carries a validated ladder whose best rung is durable.
    for step in result.steps[:-1]:
        assert step.ladder is not None, step.summary()
        assert step.ladder.best.kind <= RungKind.LABEL, step.ladder.describe()
    assert MERIDIAN.password not in (tmp_path / "discovery.jsonl").read_text("utf-8")


def test_scripted_discovery_reports_no_such_member(
    surface: WebSurface, live_app: str
) -> None:
    result = _run(
        surface,
        live_app,
        [
            *LOGIN,
            on("type_text", "textbox", "Member ID", text="{{member_id}}"),
            on("click", "button", "Search"),
            on("read", "StaticText", "No such member.", output_name="message"),
            finish(
                outcome="business_outcome",
                outcome_code="MEMBER_NOT_FOUND",
                summary="no such member",
            ),
        ],
        "M-99999",
    )
    assert result.status is RunStatus.BUSINESS_OUTCOME
    assert result.outcome_code == "MEMBER_NOT_FOUND"
    assert "No such member." in result.outputs["message"]


def test_the_irreversible_step_is_blocked_on_the_real_surface(
    surface: WebSurface, live_app: str
) -> None:
    result = _run(
        surface,
        live_app,
        [
            *LOGIN,
            on("type_text", "textbox", "Member ID", text="{{member_id}}"),
            on("click", "button", "Search"),
            on("click", "link", "Open Member"),
            on("click", "link", "Open Sub-Account SA-4471"),
            on("click", "button", "Confirm Transfer"),
            ToolCall("escalate", {"reason": "confirmation needs approval"}),
        ],
        "M-10001",
    )
    assert result.status is RunStatus.ESCALATED
    blocked = result.steps[-2]
    assert not blocked.allowed and blocked.risk == "risky"
    assert "Transfer Authorized" not in surface.observe().render()
