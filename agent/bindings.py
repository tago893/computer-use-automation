"""Placeholders for task inputs and secrets.

The model never handles a credential. It is told that ``{{secret:operator_passphrase}}``
exists and types exactly that string; the harness substitutes the real value at
the instant of acting, and records the *placeholder*. Three properties follow:

1. **The model never sees a secret**, so no provider -- free tier or otherwise --
   can retain one. Secret values are also scrubbed from every observation before
   it is rendered into a prompt, in case the surface echoes one back.
2. **The run log never contains a secret**, because what is logged is the
   placeholder the model typed, not the expansion.
3. **The recording is already parameterized.** A step that typed
   ``{{member_id}}`` replays with any member id. Parameterization is not a
   guess made afterwards by diffing values; it is what was recorded.

Task inputs (``member_id``) are *not* secret -- the model needs to see the id to
pick the right row -- so they are shown to the model. If the model types an
input's literal value instead of its placeholder, ``lift`` still records the
placeholder.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

_PLACEHOLDER: Final[re.Pattern[str]] = re.compile(
    r"\{\{\s*(secret:)?([a-z][a-z0-9_]*)\s*\}\}"
)
_NAME: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

#: Secrets shorter than this are not scrubbed from observations: replacing
#: every "1" on a page would destroy the page, not protect anything.
MIN_SCRUB_CHARS: Final[int] = 4


class BindingError(ValueError):
    """A placeholder named something that was not bound."""


@dataclass(frozen=True)
class Bindings:
    """The values a run may type, addressed by name."""

    inputs: Mapping[str, str] = field(default_factory=dict)
    secrets: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (*self.inputs, *self.secrets):
            if not _NAME.match(name):
                raise BindingError(f"binding name {name!r} must be snake_case")
        overlap = set(self.inputs) & set(self.secrets)
        if overlap:
            raise BindingError(f"names bound as both input and secret: {overlap}")

    def expand(self, text: str) -> str:
        """Substitute placeholders with real values, for the moment of acting.

        Raises:
            BindingError: on a placeholder with no binding. Typing the literal
                string ``{{member_id}}`` into a form is never what was meant.
        """

        def substitute(match: re.Match[str]) -> str:
            is_secret, name = bool(match.group(1)), match.group(2)
            pool = self.secrets if is_secret else self.inputs
            if name not in pool:
                kind = "secret" if is_secret else "input"
                raise BindingError(f"no {kind} named {name!r} is bound")
            return pool[name]

        return _PLACEHOLDER.sub(substitute, text)

    def lift(self, text: str) -> str:
        """The form of ``text`` that is safe to record.

        An exact literal match on a bound value becomes its placeholder. Partial
        matches are left alone: substring-replacing ``M-1`` inside ``M-10001``
        would corrupt a recording far more quietly than not lifting at all.
        """
        for name, value in self.secrets.items():
            if value and text == value:
                return "{{secret:" + name + "}}"
        for name, value in self.inputs.items():
            if value and text == value:
                return "{{" + name + "}}"
        return text

    def scrub(self, text: str) -> str:
        """Remove secret values from text bound for a prompt or a log."""
        for name, value in self.secrets.items():
            if len(value) >= MIN_SCRUB_CHARS:
                text = text.replace(value, "{{secret:" + name + "}}")
        return text

    def describe_for_model(self) -> str:
        """What the model is told it may type. Secret *values* never appear."""
        lines = []
        for name, value in self.inputs.items():
            lines.append(f"  {{{{{name}}}}} = {value!r}")
        for name in self.secrets:
            lines.append(f"  {{{{secret:{name}}}}} = (hidden credential)")
        return "\n".join(lines) if lines else "  (none)"
