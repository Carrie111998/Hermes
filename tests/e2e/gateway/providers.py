"""Provider matrix for the dockerized-gateway e2e probes.

Each :class:`ProviderSpec` describes one LLM backend the gateway can be
configured to talk to: the ``provider`` value written into the container's
``config.yaml``, the env var holding its API key, a default model (cheap, and
overridable), and how that backend is expected to behave for the corner cases
the probes assert on (notably ``response_format: json_object``).

:func:`discover_providers` resolves the matrix against an environment mapping.
It MUST be called at import time of the e2e conftest — before the repo's
hermetic autouse fixture blanks every ``*_API_KEY`` for the duration of each
test — otherwise the keys are gone and the matrix is empty. The resolved key
value is captured into the returned objects so the rest of the run never has to
read ``os.environ`` again.

Model defaults are best-effort and drift as providers rename models. Override
any of them without touching code via ``HERMES_E2E_MODEL_<PROVIDER>`` (the
provider id upper-cased, ``-`` → ``_``), e.g. ``HERMES_E2E_MODEL_ANTHROPIC``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Optional

# How a backend handles OpenAI's `response_format: {"type": "json_object"}`:
#   "accept" — the gateway maps it to a native equivalent; expect HTTP 200.
#   "reject" — no native mapping (e.g. Anthropic Messages); expect HTTP 400
#              up-front rather than a silently-dropped constraint.
#   "any"    — unverified for this backend; the probe reports but does not fail.
JsonObjectMode = str  # one of {"accept", "reject", "any"}


@dataclass(frozen=True)
class ProviderSpec:
    """Static description of one backend in the matrix."""

    id: str
    """Value written to ``model.provider`` in the container's config.yaml."""
    label: str
    env_keys: tuple[str, ...]
    """API-key env vars to probe, in priority order. First present one wins."""
    default_model: str
    base_url: Optional[str] = None
    json_object: JsonObjectMode = "any"
    # Whether this backend/model is expected to emit reasoning. Only gates a
    # soft probe — reasoning is opt-in and model-dependent, so its absence is
    # never a hard failure.
    reasoning: bool = False

    def model_override_env(self) -> str:
        return "HERMES_E2E_MODEL_" + self.id.upper().replace("-", "_")


@dataclass(frozen=True)
class ResolvedProvider:
    """A :class:`ProviderSpec` with its key + model resolved from the env."""

    spec: ProviderSpec
    key_env: str
    key_value: str = field(repr=False)
    model: str

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def container_env(self) -> dict[str, str]:
        """Provider-credential env vars to pass into the gateway container."""
        return {self.key_env: self.key_value}


# Curated matrix. Only providers whose key is present at import time activate,
# so listing one the user has no key for costs nothing. Keep model defaults
# cheap — these run real upstream calls on every invocation.
PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        id="anthropic",
        label="Anthropic (native Messages API)",
        env_keys=("ANTHROPIC_API_KEY",),
        default_model="claude-haiku-4-5-20251001",
        json_object="reject",  # no native json_object on the Messages API
        reasoning=True,
    ),
    ProviderSpec(
        id="openrouter",
        label="OpenRouter (OpenAI-compatible)",
        env_keys=("OPENROUTER_API_KEY", "OPENAI_API_KEY"),
        default_model="anthropic/claude-3.5-haiku",
        base_url="https://openrouter.ai/api/v1",
        json_object="accept",
    ),
    ProviderSpec(
        id="gemini",
        label="Google AI Studio (Gemini)",
        env_keys=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        default_model="gemini-2.5-flash",
        json_object="any",
    ),
    ProviderSpec(
        id="zai",
        label="z.ai / ZhipuAI GLM",
        env_keys=("GLM_API_KEY", "ZAI_API_KEY"),
        default_model="glm-4.6",
        json_object="any",
    ),
    ProviderSpec(
        id="kimi-coding",
        label="Kimi / Moonshot",
        env_keys=("KIMI_API_KEY", "MOONSHOT_API_KEY"),
        default_model="kimi-k2-turbo-preview",
        json_object="any",
    ),
    ProviderSpec(
        id="minimax",
        label="MiniMax (global)",
        env_keys=("MINIMAX_API_KEY",),
        default_model="MiniMax-M2",
        json_object="any",
    ),
)


def _resolve_one(spec: ProviderSpec, env: Mapping[str, str]) -> Optional[ResolvedProvider]:
    for key_env in spec.env_keys:
        value = env.get(key_env)
        if value:
            model = env.get(spec.model_override_env()) or spec.default_model
            return ResolvedProvider(spec=spec, key_env=key_env, key_value=value, model=model)
    return None


def discover_providers(env: Optional[Mapping[str, str]] = None) -> list[ResolvedProvider]:
    """Return the activated matrix for ``env`` (defaults to ``os.environ``).

    Call at import time, before credential-blanking fixtures run.
    """
    env = os.environ if env is None else env
    resolved = (_resolve_one(spec, env) for spec in PROVIDERS)
    return [r for r in resolved if r is not None]
