"""The LLM provider seam.

The discovery loop depends on exactly one capability: *given a prompt and a set of
tools, choose one tool call*. It never imports a vendor SDK. Everything
vendor-specific lives in an adapter below, and choosing one is a config value.

Two design choices worth defending:

**Stateless per step.** Each decision is one system prompt plus one user message
holding the goal, a compact history of steps taken, and the *current*
observation. There is no growing provider-side transcript. The observation is the
state; replaying old observations would only spend tokens on screens that no
longer exist. It also means the adapters never have to reconcile each vendor's
tool-result bookkeeping, which is where multi-provider code usually rots.

**Tool use is forced.** Every adapter asks the provider to *require* a tool call,
so a reply is always a structured, schema-shaped action -- never prose to parse.
A model that answers in text anyway yields ``Decision.call is None``, which the
loop treats as a protocol violation rather than guessing at intent.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from config import LLMSettings

logger = logging.getLogger(__name__)

#: Per-request ceiling. Discovery steps are small; a request that takes longer
#: than this is a provider problem, not a thinking problem.
REQUEST_TIMEOUT_S: Final[float] = 60.0

#: SDK-level retries for transient failures (429s, 5xx, connection resets). The
#: SDKs back off exponentially; the loop itself never retries a decision.
MAX_RETRIES: Final[int] = 3

#: Output ceiling per decision. One tool call with a short rationale.
MAX_OUTPUT_TOKENS: Final[int] = 1024


@dataclass(frozen=True)
class ToolSpec:
    """A tool offered to the model, in provider-neutral JSON Schema."""

    name: str
    description: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """The one tool call a model chose."""

    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    """A model's reply to one step.

    ``rationale`` is whatever free text the model emitted alongside the call. It
    goes to the run log for debugging and is never parsed for behaviour.
    """

    call: ToolCall | None
    rationale: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


class LLMError(RuntimeError):
    """The provider could not produce a decision (auth, quota, network, 5xx)."""


class ToolCallingLLM(Protocol):
    """The whole of what the discovery loop needs from a model."""

    @property
    def model_id(self) -> str:
        """``provider/model``, recorded into run provenance."""
        ...

    def decide(self, *, system: str, prompt: str, tools: Sequence[ToolSpec]) -> Decision:
        """Choose one tool call.

        Raises:
            LLMError: when the provider fails. A model that merely declines to
                call a tool is *not* an error: it returns ``call=None``.
        """
        ...


# ---------------------------------------------------------------------------
# OpenAI-compatible: Gemini, Groq, GitHub Models, Cerebras, OpenRouter, Ollama
# ---------------------------------------------------------------------------


class OpenAICompatibleLLM:
    """One adapter for every provider speaking the OpenAI chat-completions API."""

    def __init__(self, settings: LLMSettings) -> None:
        from openai import OpenAI

        self._settings = settings
        self._client = OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=REQUEST_TIMEOUT_S,
            max_retries=MAX_RETRIES,
        )

    @property
    def model_id(self) -> str:
        return f"{self._settings.provider}/{self._settings.model}"

    def decide(self, *, system: str, prompt: str, tools: Sequence[ToolSpec]) -> Decision:
        from openai import OpenAIError

        try:
            response = self._client.chat.completions.create(
                model=self._settings.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": dict(tool.parameters),
                        },
                    }
                    for tool in tools
                ],
                tool_choice="required",
                max_tokens=MAX_OUTPUT_TOKENS,
                temperature=0,
            )
        except OpenAIError as exc:
            raise LLMError(f"{self.model_id}: {type(exc).__name__}: {exc}") from exc

        if not response.choices:
            raise LLMError(f"{self.model_id}: response had no choices")
        message = response.choices[0].message
        usage = response.usage
        call = None
        if message.tool_calls:
            first = message.tool_calls[0]
            call = ToolCall(
                name=first.function.name,
                arguments=_parse_arguments(first.function.arguments),
            )
        return Decision(
            call=call,
            rationale=(message.content or "").strip(),
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )


def _parse_arguments(raw: str | None) -> dict[str, Any]:
    """Decode tool arguments. Malformed JSON becomes a sentinel the loop rejects,
    rather than an exception that would end the run over one bad reply."""
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {"__malformed__": raw[:200]}
    return decoded if isinstance(decoded, dict) else {"__malformed__": raw[:200]}


# ---------------------------------------------------------------------------
# Anthropic: native Messages API
# ---------------------------------------------------------------------------


class AnthropicLLM:
    """Adapter for the Anthropic Messages API."""

    def __init__(self, settings: LLMSettings) -> None:
        from anthropic import Anthropic

        self._settings = settings
        self._client = Anthropic(
            api_key=settings.api_key,
            timeout=REQUEST_TIMEOUT_S,
            max_retries=MAX_RETRIES,
        )

    @property
    def model_id(self) -> str:
        return f"{self._settings.provider}/{self._settings.model}"

    def decide(self, *, system: str, prompt: str, tools: Sequence[ToolSpec]) -> Decision:
        from anthropic import AnthropicError

        try:
            response = self._client.messages.create(
                model=self._settings.model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                tools=[
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "input_schema": dict(tool.parameters),
                    }
                    for tool in tools
                ],
                # "any" = must call some tool. Parallel calls are disabled
                # because the loop executes exactly one action per observation.
                tool_choice={"type": "any", "disable_parallel_tool_use": True},
            )
        except AnthropicError as exc:
            raise LLMError(f"{self.model_id}: {type(exc).__name__}: {exc}") from exc

        call = None
        text: list[str] = []
        for block in response.content:
            if block.type == "tool_use" and call is None:
                arguments = block.input if isinstance(block.input, dict) else {}
                call = ToolCall(name=block.name, arguments=arguments)
            elif block.type == "text":
                text.append(block.text)
        return Decision(
            call=call,
            rationale=" ".join(text).strip(),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )


# ---------------------------------------------------------------------------
# Scripted: no network, for tests and for running the pipeline offline
# ---------------------------------------------------------------------------

#: A script step: a fixed call, or a function of the prompt that returns one.
ScriptStep = ToolCall | Callable[[str], ToolCall]


class ScriptedLLM:
    """A deterministic stand-in that plays back a fixed sequence of calls.

    This is the documented seam for running without live services: the loop, the
    guards, the recorder, and the run log are all exercised for real; only the
    choice of action is canned. Steps may be callables so a script can pick an
    ordinal from the observation it is shown, exactly as a model must.
    """

    def __init__(self, steps: Sequence[ScriptStep], *, name: str = "scripted") -> None:
        self._steps = list(steps)
        self._cursor = 0
        self._name = name
        self.prompts: list[str] = []

    @property
    def model_id(self) -> str:
        return f"scripted/{self._name}"

    def decide(self, *, system: str, prompt: str, tools: Sequence[ToolSpec]) -> Decision:
        self.prompts.append(prompt)
        if self._cursor >= len(self._steps):
            raise LLMError("script exhausted")
        step = self._steps[self._cursor]
        self._cursor += 1
        call = step(prompt) if callable(step) else step
        return Decision(call=call, rationale=f"script step {self._cursor}")


def build_llm(settings: LLMSettings) -> ToolCallingLLM:
    """Pick the adapter for a provider. The only place a provider name matters."""
    if settings.is_openai_compatible:
        return OpenAICompatibleLLM(settings)
    if settings.provider == "anthropic":
        return AnthropicLLM(settings)
    raise LLMError(f"no adapter for provider {settings.provider!r}")
