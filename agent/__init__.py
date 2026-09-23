"""The discovery agent: an LLM finds how to do a task, once.

The loop depends on two protocols -- ``Surface`` and ``ToolCallingLLM`` -- and
nothing else. What it produces is a list of ``StepRecord`` s carrying validated
locator ladders; Phase 4 compiles those into a capability artifact.
"""

from .bindings import BindingError, Bindings
from .llm import (
    AnthropicLLM,
    Decision,
    LLMError,
    OpenAICompatibleLLM,
    ScriptedLLM,
    ToolCall,
    ToolCallingLLM,
    ToolSpec,
    build_llm,
)
from .loop import DiscoveryAgent, Limits
from .records import DiscoveryResult, RunStatus, StepRecord, TargetInfo
from .runlog import RunLog

__all__ = [
    "AnthropicLLM",
    "BindingError",
    "Bindings",
    "Decision",
    "DiscoveryAgent",
    "DiscoveryResult",
    "LLMError",
    "Limits",
    "OpenAICompatibleLLM",
    "RunLog",
    "RunStatus",
    "ScriptedLLM",
    "StepRecord",
    "TargetInfo",
    "ToolCall",
    "ToolCallingLLM",
    "ToolSpec",
    "build_llm",
]
