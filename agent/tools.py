"""The tools the model may call, and strict parsing of what it sends back.

Every argument is validated here, at the boundary between model output and the
harness. The model is an untrusted input source like any other: a malformed call
becomes a ``ToolRejected`` fed back to the model as feedback, never an exception
and never a half-performed action.

Two tools carry design weight beyond "do a thing":

``read(ordinal, output_name)``
    Outputs are bound to *read steps*, not to prose in ``finish``. That is what
    lets replay reproduce an output deterministically: the artifact knows which
    element yields ``balance``, so no model is needed to extract it later.

``finish(outcome, outcome_code, summary)``
    The model must say whether the run ended in success or in a *business
    outcome* ("no such member"). The two are different results for the caller,
    and the distinction is made at discovery time so the artifact can declare it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final

from surfaces.models import Action, ActionKind

from .llm import ToolCall, ToolSpec

#: Output names become typed fields in the artifact's output schema.
OUTPUT_NAME: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

#: Business outcome codes, e.g. ``MEMBER_NOT_FOUND``.
OUTCOME_CODE: Final[re.Pattern[str]] = re.compile(r"^[A-Z][A-Z0-9_]{2,47}$")

MAX_TEXT_CHARS: Final[int] = 500
MAX_REASON_CHARS: Final[int] = 500

_WHY = {
    "type": "string",
    "description": "One short sentence: why this action, now.",
}


class Outcome(str, Enum):
    """How a run ended, as the model reports it in ``finish``."""

    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"


TOOLS: Final[tuple[ToolSpec, ...]] = (
    ToolSpec(
        name="click",
        description="Click the element with this ordinal in the CURRENT observation.",
        parameters={
            "type": "object",
            "properties": {
                "ordinal": {"type": "integer", "minimum": 0},
                "why": _WHY,
            },
            "required": ["ordinal", "why"],
        },
    ),
    ToolSpec(
        name="type_text",
        description=(
            "Replace the contents of a text field. To enter a task input or a "
            "credential, pass its placeholder exactly, e.g. {{member_id}} or "
            "{{secret:operator_passphrase}} -- never a literal credential."
        ),
        parameters={
            "type": "object",
            "properties": {
                "ordinal": {"type": "integer", "minimum": 0},
                "text": {"type": "string", "maxLength": MAX_TEXT_CHARS},
                "why": _WHY,
            },
            "required": ["ordinal", "text", "why"],
        },
    ),
    ToolSpec(
        name="read",
        description=(
            "Read the text of an element and store it as a named output of the "
            "task. Every value you report must come from a read."
        ),
        parameters={
            "type": "object",
            "properties": {
                "ordinal": {"type": "integer", "minimum": 0},
                "output_name": {
                    "type": "string",
                    "description": "snake_case name for this value, e.g. balance",
                },
                "why": _WHY,
            },
            "required": ["ordinal", "output_name", "why"],
        },
    ),
    ToolSpec(
        name="navigate",
        description="Load a URL in the top-level page. Only allow-listed URLs work.",
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string"}, "why": _WHY},
            "required": ["url", "why"],
        },
    ),
    ToolSpec(
        name="finish",
        description=(
            "End the task. outcome=success when the goal is achieved. "
            "outcome=business_outcome when the surface gave a definitive answer "
            "that prevents the goal (e.g. the record does not exist) -- that is a "
            "valid result, not a failure; give an UPPER_SNAKE outcome_code."
        ),
        parameters={
            "type": "object",
            "properties": {
                "outcome": {"type": "string", "enum": [o.value for o in Outcome]},
                "outcome_code": {"type": "string"},
                "summary": {"type": "string", "maxLength": MAX_REASON_CHARS},
            },
            "required": ["outcome", "summary"],
        },
    ),
    ToolSpec(
        name="escalate",
        description=(
            "Stop and hand over to a human operator: you are stuck, the surface "
            "is in a state you do not understand, or the next step needs approval."
        ),
        parameters={
            "type": "object",
            "properties": {
                "reason": {"type": "string", "maxLength": MAX_REASON_CHARS}
            },
            "required": ["reason"],
        },
    ),
)


@dataclass(frozen=True)
class Intent:
    """A validated tool call: the action plus what only the agent layer needs."""

    action: Action
    why: str = ""
    output_name: str = ""
    outcome: Outcome | None = None
    outcome_code: str = ""


@dataclass(frozen=True)
class ToolRejected:
    """A tool call that failed validation. Fed back to the model verbatim."""

    tool: str
    problem: str


def parse(call: ToolCall | None) -> Intent | ToolRejected:
    """Validate one tool call into an ``Intent``."""
    if call is None:
        return ToolRejected("<none>", "you must call exactly one tool")

    args = dict(call.arguments)
    if "__malformed__" in args:
        return ToolRejected(call.name, "arguments were not valid JSON")

    parser = _PARSERS.get(call.name)
    if parser is None:
        return ToolRejected(call.name, f"unknown tool {call.name!r}")
    return parser(call.name, args)


def _ordinal(args: dict[str, Any]) -> int | None:
    value = args.get("ordinal")
    # bool is an int subclass; True must not address ordinal 1.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _why(args: dict[str, Any]) -> str:
    return str(args.get("why", ""))[:MAX_REASON_CHARS]


def _click(name: str, args: dict[str, Any]) -> Intent | ToolRejected:
    ordinal = _ordinal(args)
    if ordinal is None:
        return ToolRejected(name, "ordinal must be a non-negative integer")
    return Intent(Action(ActionKind.CLICK, ordinal=ordinal), why=_why(args))


def _type(name: str, args: dict[str, Any]) -> Intent | ToolRejected:
    ordinal = _ordinal(args)
    if ordinal is None:
        return ToolRejected(name, "ordinal must be a non-negative integer")
    text = args.get("text")
    if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS:
        return ToolRejected(name, f"text must be a string of at most {MAX_TEXT_CHARS}")
    return Intent(Action(ActionKind.TYPE, ordinal=ordinal, text=text), why=_why(args))


def _read(name: str, args: dict[str, Any]) -> Intent | ToolRejected:
    ordinal = _ordinal(args)
    if ordinal is None:
        return ToolRejected(name, "ordinal must be a non-negative integer")
    output_name = str(args.get("output_name", ""))
    if not OUTPUT_NAME.match(output_name):
        return ToolRejected(name, "output_name must be snake_case, e.g. balance")
    return Intent(
        Action(ActionKind.READ, ordinal=ordinal),
        why=_why(args),
        output_name=output_name,
    )


def _navigate(name: str, args: dict[str, Any]) -> Intent | ToolRejected:
    url = args.get("url")
    if not isinstance(url, str) or not url.strip():
        return ToolRejected(name, "url must be a non-empty string")
    return Intent(Action(ActionKind.NAVIGATE, url=url.strip()), why=_why(args))


def _finish(name: str, args: dict[str, Any]) -> Intent | ToolRejected:
    try:
        outcome = Outcome(str(args.get("outcome", "")))
    except ValueError:
        return ToolRejected(name, "outcome must be 'success' or 'business_outcome'")
    code = str(args.get("outcome_code", "") or "").strip()
    if outcome is Outcome.BUSINESS_OUTCOME and not OUTCOME_CODE.match(code):
        return ToolRejected(
            name, "a business_outcome needs an UPPER_SNAKE outcome_code"
        )
    summary = str(args.get("summary", ""))[:MAX_REASON_CHARS]
    return Intent(
        Action(ActionKind.FINISH, reason=summary),
        outcome=outcome,
        outcome_code=code if outcome is Outcome.BUSINESS_OUTCOME else "",
    )


def _escalate(name: str, args: dict[str, Any]) -> Intent | ToolRejected:
    reason = str(args.get("reason", "")).strip()[:MAX_REASON_CHARS]
    if not reason:
        return ToolRejected(name, "escalate needs a reason a human can act on")
    return Intent(Action(ActionKind.ESCALATE, reason=reason))


_PARSERS = {
    "click": _click,
    "type_text": _type,
    "read": _read,
    "navigate": _navigate,
    "finish": _finish,
    "escalate": _escalate,
}
