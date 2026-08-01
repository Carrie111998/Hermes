"""RecursiveIntell llm-pipeline transport for Hermes.

Provides a Rust-backed LLM calling interface that replaces raw httpx/openai
calls. Falls back gracefully if the native extension is not installed.

Usage::

    from agent.transports.ri_llm import RiPipeline

    pipe = RiPipeline("http://localhost:11434", "llama3.2:3b")
    result = pipe.call("What is 2+2?")
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_NATIVE_AVAILABLE = False
try:
    from llm_pipeline._native import LlmConfig, Pipeline as _NativePipeline

    _NATIVE_AVAILABLE = True
except ImportError:
    logger.debug("llm-pipeline native extension not available; using None")


class RiLlmConfig:
    """Python-side mirror of the Rust LlmConfig."""

    def __init__(
        self,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        thinking: bool = False,
        json_mode: bool = False,
    ):
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.thinking = thinking
        self.json_mode = json_mode

    def _to_native(self):
        if not _NATIVE_AVAILABLE:
            return None
        return LlmConfig(
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            thinking=self.thinking,
            json_mode=self.json_mode,
        )


class RiPipeline:
    """Rust-backed LLM pipeline for Hermes transport."""

    def __init__(self, url: str, model: str, *, config: RiLlmConfig | None = None):
        self.url = url
        self.model = model
        self.config = config or RiLlmConfig()
        self._native: _NativePipeline | None = None
        if _NATIVE_AVAILABLE:
            self._native = _NativePipeline(
                url, model, config=self.config._to_native()
            )

    @property
    def available(self) -> bool:
        return self._native is not None

    def call(
        self,
        prompt: str,
        *,
        system: str | None = None,
        config: RiLlmConfig | None = None,
    ) -> str:
        """Call the LLM and return the raw response text."""
        if self._native is None:
            raise RuntimeError(
                "llm-pipeline native extension is not installed. "
                "Install with: pip install llm-pipeline"
            )
        native_config = config._to_native() if config else None
        return self._native.call(prompt, system=system, config=native_config)

    def call_structured(
        self,
        prompt: str,
        json_schema: str,
        *,
        system: str | None = None,
        config: RiLlmConfig | None = None,
    ) -> str:
        """Call the LLM with a JSON schema constraint."""
        if self._native is None:
            raise RuntimeError("llm-pipeline native extension is not installed")
        native_config = config._to_native() if config else None
        return self._native.call_structured(
            prompt, json_schema, system=system, config=native_config
        )

    def __repr__(self) -> str:
        status = "native" if self.available else "unavailable"
        return f"RiPipeline(url={self.url}, model={self.model}, {status})"
