"""Central configuration, loaded from the environment.

Two rules hold here:

1.  Nothing is hardcoded at a call site. Every tunable lives in this file and is
    overridable from ``.env``.
2.  Config is validated at load, not at first use. A missing LLM key is a clear
    error at startup rather than a confusing failure fifteen steps into a
    discovery run.

Phases 0-2 (target app + surfaces) do not need an LLM key at all. Only
``llm_settings()`` requires one, so importing this module never forces the key
to exist.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent
EVIDENCE_DIR: Final[Path] = REPO_ROOT / "evidence"

load_dotenv(REPO_ROOT / ".env")


class ConfigError(RuntimeError):
    """Raised when configuration is missing or self-inconsistent."""


def _env(name: str, default: str) -> str:
    value = os.getenv(name, default)
    return value.strip() if value else default


def _env_int(name: str, default: int) -> int:
    raw = _env(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


# ---------------------------------------------------------------------------
# Target application
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AppSettings:
    """Where the target surface lives."""

    host: str
    port: int
    tenant: str

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def tenant_url(self, tenant: str | None = None) -> str:
        return f"{self.base_url}/t/{tenant or self.tenant}"


def app_settings() -> AppSettings:
    return AppSettings(
        host=_env("TARGET_APP_HOST", "127.0.0.1"),
        port=_env_int("TARGET_APP_PORT", 5099),
        tenant=_env("TARGET_APP_TENANT", "meridian"),
    )


# ---------------------------------------------------------------------------
# LLM provider seam
# ---------------------------------------------------------------------------

#: Providers reachable through the OpenAI-compatible adapter. The value is the
#: default base URL; ``LLM_BASE_URL`` overrides it.
OPENAI_COMPATIBLE: Final[dict[str, str]] = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "groq": "https://api.groq.com/openai/v1",
    "github": "https://models.inference.ai.azure.com",
    "cerebras": "https://api.cerebras.ai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama": "http://localhost:11434/v1",
    "openai": "https://api.openai.com/v1",
}

#: Providers with a native SDK rather than an OpenAI-compatible endpoint.
NATIVE_PROVIDERS: Final[frozenset[str]] = frozenset({"anthropic"})

#: Model used when ``LLM_MODEL`` is unset. One sensible tool-calling model per
#: provider, so switching provider is one setting rather than two.
DEFAULT_MODELS: Final[dict[str, str]] = {
    "gemini": "gemini-2.5-flash",
    "groq": "llama-3.3-70b-versatile",
    "github": "gpt-4o-mini",
    "cerebras": "llama-3.3-70b",
    "openrouter": "google/gemini-2.5-flash",
    "ollama": "qwen2.5:7b",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-5",
}

#: Key env var per provider, when it differs from the generic ``LLM_API_KEY``.
_KEY_VARS: Final[dict[str, str]] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "github": "GITHUB_TOKEN",
}


@dataclass(frozen=True)
class LLMSettings:
    """Everything the discovery agent needs to talk to a model.

    ``provider`` selects an adapter; it never leaks into the agent loop, which
    depends only on the tool-calling interface.
    """

    provider: str
    model: str
    api_key: str
    base_url: str | None
    max_steps: int
    wall_clock_s: int

    @property
    def is_openai_compatible(self) -> bool:
        return self.provider in OPENAI_COMPATIBLE


def llm_settings(
    *, provider: str | None = None, model: str | None = None
) -> LLMSettings:
    """Resolve and validate LLM configuration.

    ``provider`` and ``model`` override the environment, so a CLI flag can pick
    a provider without editing ``.env``.

    Raises:
        ConfigError: if the provider is unknown, or its API key is absent.
    """
    provider = (provider or _env("LLM_PROVIDER", "gemini")).strip().lower()
    known = set(OPENAI_COMPATIBLE) | NATIVE_PROVIDERS
    if provider not in known:
        raise ConfigError(
            f"LLM_PROVIDER={provider!r} is not supported. "
            f"Choose one of: {', '.join(sorted(known))}"
        )

    key_var = _KEY_VARS.get(provider, "LLM_API_KEY")
    api_key = _env(key_var, "") or _env("LLM_API_KEY", "")
    if not api_key:
        raise ConfigError(
            f"No API key for provider {provider!r}. "
            f"Set {key_var} (or LLM_API_KEY) in .env -- see .env.example."
        )

    # An explicit provider override must not inherit a model id configured for
    # a different provider in .env -- that pairing would fail at the first call.
    env_model = "" if provider_overridden(provider) else _env("LLM_MODEL", "")
    model = (model or env_model or DEFAULT_MODELS.get(provider, "")).strip()
    if not model:
        raise ConfigError("LLM_MODEL must be set -- see .env.example.")

    env_base = "" if provider_overridden(provider) else _env("LLM_BASE_URL", "")
    base_url = env_base or OPENAI_COMPATIBLE.get(provider)

    return LLMSettings(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        max_steps=_env_int("AGENT_MAX_STEPS", 25),
        wall_clock_s=_env_int("AGENT_WALL_CLOCK_S", 300),
    )


def provider_overridden(provider: str) -> bool:
    """Whether ``provider`` differs from the one ``.env`` configures."""
    return provider != _env("LLM_PROVIDER", "gemini").strip().lower()
