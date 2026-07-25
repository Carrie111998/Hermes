"""Gateway ownership for the experimental Discord ↔ Codex realtime route."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Optional

from agent.transports.codex_realtime_voice import (
    CodexRealtimeCapabilities,
    CodexRealtimeSession,
    safe_realtime_error,
)

logger = logging.getLogger(__name__)


def _config_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _positive_id(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


@dataclass(frozen=True)
class CodexRealtimeVoiceConfig:
    enabled: bool = False
    user_id: Optional[int] = None
    voice: Optional[str] = None
    fallback_to_classic: bool = True
    codex_bin: str = "codex"
    codex_home: Optional[str] = None

    @classmethod
    def from_adapter(cls, adapter: Any) -> "CodexRealtimeVoiceConfig":
        platform_config = getattr(adapter, "config", None)
        extra = getattr(platform_config, "extra", None)
        raw = extra.get("codex_realtime_voice") if isinstance(extra, dict) else None
        if not isinstance(raw, dict):
            return cls()
        voice = raw.get("voice")
        voice = str(voice).strip() if voice is not None else None
        codex_bin = str(raw.get("codex_bin") or "codex").strip() or "codex"
        codex_home = raw.get("codex_home")
        codex_home = str(codex_home).strip() if codex_home else None
        return cls(
            enabled=_config_bool(raw.get("enabled"), False),
            user_id=_positive_id(raw.get("user_id")),
            voice=voice or None,
            fallback_to_classic=_config_bool(raw.get("fallback_to_classic"), True),
            codex_bin=codex_bin,
            codex_home=codex_home,
        )


@dataclass(frozen=True)
class CodexRealtimeStartResult:
    enabled: bool
    active: bool
    fallback_to_classic: bool
    reason: Optional[str] = None
    capabilities: Optional[CodexRealtimeCapabilities] = None


@dataclass
class _ManagedSession:
    session: Any
    user_id: int
    adapter: Any
    fallback_to_classic: bool


class CodexRealtimeVoiceManager:
    """Owns one optional realtime session per Discord adapter/guild."""

    def __init__(
        self,
        *,
        session_factory: Callable[..., Any] = CodexRealtimeSession,
        dependency_ensurer: Optional[Callable[[], None]] = None,
    ) -> None:
        self._session_factory = session_factory
        self._dependency_ensurer = dependency_ensurer or self._ensure_dependencies
        self._sessions: dict[tuple[int, int], _ManagedSession] = {}
        self._starting: set[tuple[int, int]] = set()
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._cleanup_tasks: set[asyncio.Task] = set()
        self._closed = False

    @staticmethod
    def _ensure_dependencies() -> None:
        from tools.lazy_deps import ensure

        ensure("voice.codex_realtime", prompt=False)

    @staticmethod
    def _key(adapter: Any, guild_id: int) -> tuple[int, int]:
        return (id(adapter), int(guild_id))

    @staticmethod
    def _end_output(adapter: Any, guild_id: int) -> None:
        try:
            adapter.end_realtime_voice_output(guild_id)
        except Exception:
            logger.debug("Codex realtime mixer cleanup failed", exc_info=True)

    def session_for(self, adapter: Any, guild_id: int):
        managed = self._sessions.get(self._key(adapter, guild_id))
        return managed.session if managed else None

    def is_active(self, adapter: Any, guild_id: int) -> bool:
        session = self.session_for(adapter, guild_id)
        return bool(session and getattr(session, "active", False))

    def classic_fallback_enabled(self, adapter: Any, guild_id: int) -> bool:
        managed = self._sessions.get(self._key(adapter, guild_id))
        return True if managed is None else managed.fallback_to_classic

    @staticmethod
    def configured_fallback_enabled(adapter: Any) -> bool:
        """Return the durable fallback policy even after a session is removed."""
        return CodexRealtimeVoiceConfig.from_adapter(adapter).fallback_to_classic

    def prepare_for_voice_channel(self, adapter: Any, guild_id: int) -> bool:
        """Reserve PCM before Discord starts its receiver during an opted-in join."""

        config = CodexRealtimeVoiceConfig.from_adapter(adapter)
        if self._closed or not config.enabled:
            return False
        self._starting.add(self._key(adapter, guild_id))
        return True

    def cancel_voice_channel_start(self, adapter: Any, guild_id: int) -> None:
        """Release a pre-join PCM reservation after startup or join failure."""

        self._starting.discard(self._key(adapter, guild_id))

    async def start_for_voice_channel(
        self,
        *,
        adapter: Any,
        guild_id: int,
        user_id: int,
        on_transcript: Callable[[int, str, int], Any],
        on_runtime_failure: Optional[Callable[[str, bool], Any]] = None,
    ) -> CodexRealtimeStartResult:
        config = CodexRealtimeVoiceConfig.from_adapter(adapter)
        if self._closed:
            return CodexRealtimeStartResult(
                config.enabled,
                False,
                config.fallback_to_classic,
                "realtime voice manager is shutting down",
            )
        if not config.enabled:
            return CodexRealtimeStartResult(False, False, True)
        if config.user_id is None:
            return CodexRealtimeStartResult(
                True,
                False,
                config.fallback_to_classic,
                "codex_realtime_voice.user_id must be one positive Discord user ID",
            )
        bound_user_id = config.user_id
        if int(user_id) != bound_user_id:
            return CodexRealtimeStartResult(
                True,
                False,
                config.fallback_to_classic,
                "the joining user is not the configured realtime voice user",
            )

        key = self._key(adapter, guild_id)
        async with self._locks.setdefault(key, asyncio.Lock()):
            existing = self._sessions.get(key)
            if existing and getattr(existing.session, "active", False):
                return CodexRealtimeStartResult(True, True, config.fallback_to_classic)
            if existing:
                await self._stop_locked(key, adapter)

            self._starting.add(key)
            try:
                event_loop = asyncio.get_running_loop()
                await asyncio.to_thread(self._dependency_ensurer)
                output_ready = await adapter.ensure_realtime_voice_output(guild_id)
                if not output_ready:
                    raise RuntimeError("Discord voice mixer is unavailable")

                def _on_transcript(text: str, generation: int) -> Any:
                    return on_transcript(bound_user_id, text, generation)

                def _on_output_pcm(pcm: bytes) -> Any:
                    return adapter.push_realtime_voice_pcm(guild_id, pcm)

                def _on_error(reason: str) -> None:
                    safe_reason = safe_realtime_error(reason)
                    logger.warning(
                        "Codex realtime voice ended: %s",
                        safe_reason,
                    )
                    if not event_loop.is_closed():
                        event_loop.call_soon_threadsafe(
                            self._schedule_runtime_cleanup,
                            key,
                            adapter,
                            session,
                            on_runtime_failure,
                            safe_reason,
                            config.fallback_to_classic,
                        )

                session = self._session_factory(
                    cwd=os.getcwd(),
                    codex_bin=config.codex_bin,
                    codex_home=config.codex_home,
                    on_user_transcript=_on_transcript,
                    on_output_pcm=_on_output_pcm,
                    on_error=_on_error,
                )
                self._sessions[key] = _ManagedSession(
                    session=session,
                    user_id=bound_user_id,
                    adapter=adapter,
                    fallback_to_classic=config.fallback_to_classic,
                )
                capabilities = await session.start(voice=config.voice)
                if self._closed:
                    removed = self._sessions.pop(key, None)
                    await session.stop()
                    if removed is not None:
                        self._end_output(adapter, guild_id)
                    return CodexRealtimeStartResult(
                        True,
                        False,
                        config.fallback_to_classic,
                        "realtime voice manager is shutting down",
                    )
                return CodexRealtimeStartResult(
                    True,
                    True,
                    config.fallback_to_classic,
                    capabilities=capabilities,
                )
            except Exception as exc:
                safe_reason = safe_realtime_error(exc)
                logger.warning(
                    "Codex realtime voice unavailable (%s): %s",
                    (
                        "classic fallback enabled"
                        if config.fallback_to_classic
                        else "classic fallback disabled"
                    ),
                    safe_reason,
                )
                managed = self._sessions.pop(key, None)
                if managed is not None:
                    try:
                        await managed.session.stop()
                    except Exception:
                        logger.debug(
                            "Partial Codex realtime cleanup failed", exc_info=True
                        )
                self._end_output(adapter, guild_id)
                return CodexRealtimeStartResult(
                    True,
                    False,
                    config.fallback_to_classic,
                    safe_reason,
                )
            finally:
                self._starting.discard(key)

    def push_discord_pcm(
        self,
        adapter: Any,
        guild_id: int,
        user_id: int,
        pcm: bytes,
    ) -> bool:
        key = self._key(adapter, guild_id)
        # During negotiation, consume frames instead of creating a classic-STT
        # buffer that could be emitted later alongside the new realtime route.
        if key in self._starting:
            return True
        managed = self._sessions.get(key)
        if managed is None:
            config = CodexRealtimeVoiceConfig.from_adapter(adapter)
            # With fallback explicitly disabled, PCM stays consumed for the
            # entire failure-to-disconnect window, including after provider
            # cleanup has removed the managed session.
            return config.enabled and not config.fallback_to_classic
        if not getattr(managed.session, "active", False):
            # A no-fallback route remains fail-closed while transport cleanup
            # runs and before Discord is disconnected.
            return not managed.fallback_to_classic
        if managed.user_id != int(user_id):
            # An active realtime route is intentionally single-user. Consume
            # other speakers' PCM instead of leaking it into the classic STT
            # fallback and creating a second, ambiguous conversation path.
            return True
        consumed = bool(managed.session.push_discord_pcm(pcm))
        return consumed or not managed.fallback_to_classic

    async def append_speech(
        self,
        adapter: Any,
        guild_id: int,
        text: str,
        *,
        transcript_generation: Optional[int] = None,
    ) -> bool:
        managed = self._sessions.get(self._key(adapter, guild_id))
        if managed is None:
            return False
        return bool(
            await managed.session.append_speech(
                text,
                transcript_generation=transcript_generation,
            )
        )

    async def stop_for_voice_channel(self, adapter: Any, guild_id: int) -> None:
        key = self._key(adapter, guild_id)
        async with self._locks.setdefault(key, asyncio.Lock()):
            await self._stop_locked(key, adapter)

    def _schedule_runtime_cleanup(
        self,
        key: tuple[int, int],
        adapter: Any,
        failed_session: Any,
        on_runtime_failure: Optional[Callable[[str, bool], Any]],
        reason: str,
        fallback_to_classic: bool,
    ) -> None:
        task = asyncio.create_task(
            self._cleanup_after_runtime_failure(
                key,
                adapter,
                failed_session,
                on_runtime_failure,
                reason,
                fallback_to_classic,
            )
        )
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    async def _cleanup_after_runtime_failure(
        self,
        key: tuple[int, int],
        adapter: Any,
        failed_session: Any,
        on_runtime_failure: Optional[Callable[[str, bool], Any]],
        reason: str,
        fallback_to_classic: bool,
    ) -> None:
        try:
            async with self._locks.setdefault(key, asyncio.Lock()):
                managed = self._sessions.get(key)
                if managed is None or managed.session is not failed_session:
                    return
                await self._stop_locked(key, adapter)
        except Exception:
            logger.debug("Codex realtime runtime cleanup failed", exc_info=True)
        if on_runtime_failure is not None:
            try:
                result = on_runtime_failure(reason, fallback_to_classic)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.debug("Codex realtime failure callback failed", exc_info=True)

    async def _stop_locked(self, key: tuple[int, int], adapter: Any) -> None:
        managed = self._sessions.get(key)
        if managed is not None:
            try:
                await managed.session.stop()
            finally:
                if self._sessions.get(key) is managed:
                    self._sessions.pop(key, None)
                self._end_output(adapter, key[1])

    async def close(self) -> None:
        self._closed = True
        for key, managed in list(self._sessions.items()):
            try:
                await managed.session.stop()
            except Exception:
                logger.debug("Codex realtime shutdown failed", exc_info=True)
            finally:
                if self._sessions.get(key) is managed:
                    self._sessions.pop(key, None)
                self._end_output(managed.adapter, key[1])
        if self._cleanup_tasks:
            await asyncio.gather(*list(self._cleanup_tasks), return_exceptions=True)
