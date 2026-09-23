"""The discovery loop, against an in-memory surface and a scripted model.

No browser, no network. The point is to put every exit of the loop under test --
each stopping condition, each guard -- which a live model would never exercise on
demand. ``test_discovery_live.py`` then proves the same loop on the real surface.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agent import Bindings, DiscoveryAgent, Limits, RunLog, RunStatus, ScriptedLLM
from agent.llm import ToolCall
from policy.guard import ActionPolicy, Allowlist
from surfaces.locators import Resolution, build_ladder
from surfaces.models import Action, ActionKind, ActResult, AXNode, Observation

BASE = "http://app.test"
ALLOW = Allowlist.for_tenant(BASE, "meridian")
SECRET = "hunter2-passphrase"


@dataclass
class Page:
    url: str
    nodes: tuple[AXNode, ...]
    #: ordinal clicked -> key of the page it leads to
    links: dict[int, str] = field(default_factory=dict)


def _n(ordinal: int, role: str, name: str) -> AXNode:
    return AXNode(ordinal=ordinal, role=role, name=name)


PAGES: dict[str, Page] = {
    "login": Page(
        f"{BASE}/t/meridian/login",
        (
            _n(0, "textbox", "Operator ID"),
            _n(1, "textbox", "Passphrase"),
            _n(2, "button", "Sign In"),
        ),
        links={2: "search"},
    ),
    "search": Page(
        f"{BASE}/t/meridian/console",
        (
            _n(0, "textbox", "Member ID"),
            _n(1, "button", "Search"),
            _n(2, "button", "Refresh"),  # does nothing: a stall trap
            _n(3, "link", "Vendor Portal"),  # leaves the allowlist
        ),
        links={1: "account", 3: "offsite"},
    ),
    "account": Page(
        f"{BASE}/t/meridian/console",
        (
            _n(0, "rowheader", "Balance"),
            _n(1, "cell", "$8,123.55"),
            _n(2, "button", "Confirm Transfer"),
        ),
        links={2: "confirmed"},
    ),
    "confirmed": Page(f"{BASE}/t/meridian/console", (_n(0, "status", "Authorized"),)),
    "offsite": Page("https://vendor.example/portal", (_n(0, "heading", "Vendor"),)),
}


class FakeSurface:
    """Satisfies the ``Surface`` protocol with a page table."""

    def __init__(self, start: str = "login") -> None:
        self.page = start
        self.performed: list[Action] = []

    def observe(self) -> Observation:
        page = PAGES[self.page]
        return Observation(url=page.url, title=self.page, nodes=page.nodes)

    def act(self, action: Action) -> ActResult:
        self.performed.append(action)
        page = PAGES[self.page]
        node = next(n for n in page.nodes if n.ordinal == action.ordinal)
        ladder = build_ladder(role=node.role, name=node.name, frame_path=())
        if action.kind is ActionKind.CLICK and action.ordinal in page.links:
            self.page = page.links[action.ordinal]
        return ActResult(
            ok=True,
            action=action,
            read_value=node.name if action.kind is ActionKind.READ else "",
            url_after=PAGES[self.page].url,
            ladder=ladder,
            resolution=Resolution(matched=ladder.best.kind),
        )

    def resolve(self, ladder: object) -> Resolution:
        return Resolution(matched=None)

    def screenshot(self) -> bytes:
        return b"\x89PNG fake"

    def close(self) -> None:
        pass


def call(name: str, **arguments: object) -> ToolCall:
    return ToolCall(name=name, arguments={"why": "test", **arguments})


LOGIN = [
    call("type_text", ordinal=0, text="{{secret:operator_id}}"),
    call("type_text", ordinal=1, text="{{secret:operator_passphrase}}"),
    call("click", ordinal=2),
]


def _agent(
    surface: FakeSurface,
    script: list[ToolCall],
    *,
    limits: Limits = Limits(),
    log: RunLog | None = None,
    allow_risky: bool = False,
    clock: object = None,
) -> tuple[DiscoveryAgent, ScriptedLLM]:
    llm = ScriptedLLM(script)
    agent = DiscoveryAgent(
        surface=surface,
        llm=llm,
        policy=ActionPolicy(ALLOW, allow_risky=allow_risky),
        bindings=Bindings(
            inputs={"member_id": "M-10001"},
            secrets={"operator_id": "svc.agent", "operator_passphrase": SECRET},
        ),
        limits=limits,
        log=log,
        **({"clock": clock} if clock else {}),
    )
    return agent, llm


# --- terminal states ----------------------------------------------------------


def test_success_path_records_placeholders_and_outputs(tmp_path: Path) -> None:
    surface = FakeSurface()
    log = RunLog(tmp_path)
    agent, llm = _agent(
        surface,
        [
            *LOGIN,
            call("type_text", ordinal=0, text="M-10001"),  # a literal, not a placeholder
            call("click", ordinal=1),
            call("read", ordinal=1, output_name="balance"),
            call("finish", outcome="success", summary="read the balance"),
        ],
        log=log,
    )
    result = agent.run("read the balance", run_id="r1")
    log.close()

    assert result.status is RunStatus.SUCCEEDED
    assert result.outputs == {"balance": "$8,123.55"}

    typed = [s.text_template for s in result.steps if s.tool == "type_text"]
    assert typed == [
        "{{secret:operator_id}}",
        "{{secret:operator_passphrase}}",
        "{{member_id}}",  # the literal was lifted to its placeholder
    ]
    # ...but the surface received real values.
    assert [a.text for a in surface.performed if a.kind is ActionKind.TYPE] == [
        "svc.agent",
        SECRET,
        "M-10001",
    ]
    assert all(s.ladder is not None for s in result.steps if s.tool != "finish")

    # The secret reached neither the model nor the disk.
    assert not any(SECRET in prompt for prompt in llm.prompts)
    assert SECRET not in (tmp_path / "discovery.jsonl").read_text(encoding="utf-8")


def test_business_outcome_is_its_own_status() -> None:
    agent, _ = _agent(
        FakeSurface(),
        [
            call(
                "finish",
                outcome="business_outcome",
                outcome_code="MEMBER_NOT_FOUND",
                summary="no such member",
            )
        ],
    )
    result = agent.run("look up", run_id="r")
    assert result.status is RunStatus.BUSINESS_OUTCOME
    assert result.status.completed
    assert result.outcome_code == "MEMBER_NOT_FOUND"


def test_risky_click_is_blocked_explained_and_not_performed() -> None:
    surface = FakeSurface(start="account")
    agent, llm = _agent(
        surface,
        [call("click", ordinal=2), call("escalate", reason="needs approval")],
    )
    result = agent.run("confirm", run_id="r")

    assert result.status is RunStatus.ESCALATED
    blocked = result.steps[0]
    assert not blocked.allowed and blocked.risk == "risky"
    assert surface.performed == [], "a blocked action must never reach the surface"
    assert "BLOCKED" in llm.prompts[1] and "NOTE:" in llm.prompts[1]


def test_risky_click_proceeds_when_the_run_is_approved() -> None:
    surface = FakeSurface(start="account")
    agent, _ = _agent(
        surface,
        [call("click", ordinal=2), call("finish", outcome="success", summary="done")],
        allow_risky=True,
    )
    result = agent.run("confirm", run_id="r")
    assert result.status is RunStatus.SUCCEEDED
    assert surface.page == "confirmed"
    assert result.steps[0].risk == "risky"


def test_clicks_that_change_nothing_stall_the_run() -> None:
    agent, _ = _agent(FakeSurface(start="search"), [call("click", ordinal=2)] * 5)
    result = agent.run("x", run_id="r")
    assert result.status is RunStatus.STALLED
    assert "changed nothing" in result.reason
    assert len(result.steps) == Limits().stall_limit


def test_repeated_failures_stall_the_run_without_touching_the_surface() -> None:
    surface = FakeSurface()
    agent, llm = _agent(surface, [call("click", ordinal=99)] * 5)
    result = agent.run("x", run_id="r")
    assert result.status is RunStatus.STALLED
    assert "consecutive actions failed" in result.reason
    assert surface.performed == []
    assert "not in the current observation" in llm.prompts[1]


def test_an_unbound_placeholder_is_refused_before_acting() -> None:
    surface = FakeSurface()
    agent, _ = _agent(
        surface,
        [
            call("type_text", ordinal=0, text="{{secret:ssn}}"),
            call("escalate", reason="stuck"),
        ],
    )
    result = agent.run("x", run_id="r")
    assert "no secret named 'ssn'" in result.steps[0].detail
    assert surface.performed == []


def test_rejected_tool_calls_count_as_failures() -> None:
    agent, _ = _agent(FakeSurface(), [ToolCall("click", {"ordinal": "seven"})] * 3)
    assert agent.run("x", run_id="r").status is RunStatus.STALLED


def test_leaving_the_allowlist_stops_the_run() -> None:
    agent, _ = _agent(FakeSurface(start="search"), [call("click", ordinal=3)])
    result = agent.run("x", run_id="r")
    assert result.status is RunStatus.POLICY_STOP
    assert "vendor.example" in result.reason


def test_step_budget_is_enforced() -> None:
    agent, _ = _agent(
        FakeSurface(), [call("read", ordinal=0, output_name="x")] * 5,
        limits=Limits(max_steps=2),
    )
    result = agent.run("x", run_id="r")
    assert result.status is RunStatus.BUDGET_EXHAUSTED
    assert len(result.steps) == 2


def test_wall_clock_is_enforced() -> None:
    ticks = itertools.count(0, 100)  # every clock read advances 100s
    agent, _ = _agent(
        FakeSurface(),
        [call("read", ordinal=0, output_name="x")] * 5,
        limits=Limits(wall_clock_s=250),
        clock=lambda: float(next(ticks)),
    )
    assert agent.run("x", run_id="r").status is RunStatus.TIMED_OUT


def test_provider_failure_ends_the_run_cleanly() -> None:
    agent, _ = _agent(FakeSurface(), [])  # script exhausted -> LLMError
    result = agent.run("x", run_id="r")
    assert result.status is RunStatus.LLM_ERROR
    assert not result.status.completed


# --- the run log ----------------------------------------------------------------


def test_run_log_is_complete_jsonl(tmp_path: Path) -> None:
    log = RunLog(tmp_path)
    agent, _ = _agent(
        FakeSurface(),
        [*LOGIN, call("finish", outcome="success", summary="signed in")],
        log=log,
    )
    agent.run("sign in", run_id="r-log")
    log.close()

    events = [
        json.loads(line)
        for line in (tmp_path / "discovery.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    kinds = [event["event"] for event in events]
    assert kinds[0] == "run_started" and kinds[-1] == "run_finished"
    assert kinds.count("step") == 4
    assert events[0]["secrets"] == ["operator_id", "operator_passphrase"]
    assert events[-1]["status"] == "succeeded"
    step = events[1]
    assert step["ladder"]["rungs"][0]["kind"] == "ROLE_NAME"
    assert (tmp_path / "final.png").exists()


@pytest.mark.parametrize("status", list(RunStatus))
def test_only_finished_runs_count_as_completed(status: RunStatus) -> None:
    assert status.completed is (
        status in (RunStatus.SUCCEEDED, RunStatus.BUSINESS_OUTCOME)
    )
