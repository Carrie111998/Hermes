"""Hermes memory-provider adapter for the Obsidian Memory Duo broker."""

from __future__ import annotations

from pathlib import Path

from agent.memory_provider import MemoryProvider

from .config import ObsidianDuoConfig


class ObsidianDuoMemoryProvider(MemoryProvider):
    def __init__(self, llm=None):
        self._llm = llm
        self._broker = None
        self._hermes_home = None

    @property
    def name(self) -> str:
        return "obsidian_duo"

    def is_available(self) -> bool:
        return ObsidianDuoConfig.find_config() is not None

    def initialize(self, session_id: str, **kwargs) -> None:
        self._hermes_home = Path(kwargs["hermes_home"])

    def get_tool_schemas(self):
        return []


def register(ctx):
    ctx.register_memory_provider(ObsidianDuoMemoryProvider(llm=ctx.llm))


__all__ = ["ObsidianDuoMemoryProvider", "register"]
