"""Proxy-mode methods for ``GatewayRunner``.

Extracted from ``gateway/run.py`` (god-file decomposition campaign, wave 1).
Holds the remote-proxy cluster: proxy-URL resolution, the shared stream
consumer config builder, and the SSE relay run path.

Behavior-neutral: every method is lifted verbatim from ``GatewayRunner``.
``_load_gateway_config``, ``_platform_config_key`` and
``_GATEWAY_PROXY_SSE_BUFFER_MAX_CHARS`` stay in ``gateway/run.py`` and are
imported lazily inside the methods that use them.
"""


from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from typing import Any, Callable, Dict, List, Optional

from gateway.config import Platform

logger = logging.getLogger("gateway.run")


class GatewayProxyMixin:

    def _get_proxy_url(self) -> Optional[str]:
        """Return the proxy URL if proxy mode is configured, else None.

        Checks GATEWAY_PROXY_URL env var first (convenient for Docker),
        then ``gateway.proxy_url`` in config.yaml.
        """
        from gateway.run import _load_gateway_config
        url = os.getenv("GATEWAY_PROXY_URL", "").strip()
        if url:
            return url.rstrip("/")
        cfg = _load_gateway_config()
        url = (cfg.get("gateway") or {}).get("proxy_url")
        url = (url or "").strip()
        if url:
            return url.rstrip("/")
        return None

    def _build_stream_consumer_config(
        self,
        source: "SessionSource",
        scfg: Any,
        adapter: Any,
        *,
        on_missing_cursor: str,
    ) -> "tuple[Any, Optional[Callable[[], None]]]":
        """Build the shared ``StreamConsumerConfig`` and the optional
        Telegram pause-typing closure used by both agent-run paths.

        ``on_missing_cursor`` controls how platforms whose adapter sets
        ``SUPPORTS_MESSAGE_EDITING = False`` are handled — both semantics
        are preserved verbatim from the pre-refactor call sites:

        - ``"fallback"`` (proxy path): stream anyway with an empty cursor.
        - ``"raise"`` (in-process agent path): raise ``RuntimeError`` so
          the caller's ``except`` skips streaming entirely.

        Returns ``(consumer_cfg, pause_typing_before_finalize)``.
        """
        from gateway.stream_consumer import StreamConsumerConfig

        _pause_typing_before_finalize = None
        if source.platform == Platform.TELEGRAM and hasattr(adapter, "pause_typing_for_chat"):
            def _pause_typing_before_finalize(
                _adapter=adapter,
                _chat_id=source.chat_id,
            ) -> None:
                _adapter.pause_typing_for_chat(_chat_id)
        # Platforms that don't support editing sent messages
        # (e.g. QQ, WeChat) should skip streaming entirely —
        # without edit support, the consumer sends a partial
        # first message that can never be updated, resulting in
        # duplicate messages (partial + final).
        # (The proxy path instead opts into a cursorless fallback
        # via on_missing_cursor="fallback".)
        _adapter_supports_edit = getattr(adapter, "SUPPORTS_MESSAGE_EDITING", True)
        if not _adapter_supports_edit and on_missing_cursor == "raise":
            raise RuntimeError("skip streaming for non-editable platform")
        _effective_cursor = scfg.cursor if _adapter_supports_edit else ""
        # Some Matrix clients render the streaming cursor
        # as a visible tofu/white-box artifact.  Keep
        # streaming text on Matrix, but suppress the cursor.
        _buffer_only = False
        if source.platform == Platform.MATRIX:
            _effective_cursor = ""
            _buffer_only = True
        # Fresh-final applies to Telegram only — other
        # platforms either edit in place cheaply (Discord,
        # Slack) or don't have the timestamp-on-edit /
        # edit-timestamp-stays-stale problem.
        # (Ported from openclaw/openclaw#72038.)
        _fresh_final_secs = (
            float(getattr(scfg, "fresh_final_after_seconds", 0.0) or 0.0)
            if source.platform == Platform.TELEGRAM
            else 0.0
        )
        _consumer_cfg = StreamConsumerConfig(
            edit_interval=scfg.edit_interval,
            buffer_threshold=scfg.buffer_threshold,
            cursor=_effective_cursor,
            buffer_only=_buffer_only,
            fresh_final_after_seconds=_fresh_final_secs,
            transport=scfg.transport or "edit",
            chat_type=getattr(source, "chat_type", "") or "",
        )
        return _consumer_cfg, _pause_typing_before_finalize

    async def _run_agent_via_proxy(
        self,
        message: str,
        context_prompt: str,
        history: List[Dict[str, Any]],
        source: "SessionSource",
        session_id: str,
        session_key: str = None,
        run_generation: Optional[int] = None,
        event_message_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Forward the message to a remote Hermes API server instead of
        running a local AIAgent.

        When ``GATEWAY_PROXY_URL`` (or ``gateway.proxy_url`` in config.yaml)
        is set, the gateway becomes a thin relay: it handles platform I/O
        (encryption, threading, media) and delegates all agent work to the
        remote server via ``POST /v1/chat/completions`` with SSE streaming.

        This lets a Docker container handle Matrix E2EE while the actual
        agent runs on the host with full access to local files, memory,
        skills, and a unified session store.
        """
        from gateway.run import _GATEWAY_PROXY_SSE_BUFFER_MAX_CHARS, _load_gateway_config, _platform_config_key
        try:
            from aiohttp import ClientSession as _AioClientSession, ClientTimeout
        except ImportError:
            return {
                "final_response": "⚠️ Proxy mode requires aiohttp. Install with: pip install aiohttp",
                "messages": [],
                "api_calls": 0,
                "tools": [],
            }

        proxy_url = self._get_proxy_url()
        if not proxy_url:
            return {
                "final_response": "⚠️ Proxy URL not configured (GATEWAY_PROXY_URL or gateway.proxy_url)",
                "messages": [],
                "api_calls": 0,
                "tools": [],
            }

        # Scope-aware read: the proxy key is a per-profile credential; under
        # multiplex honor the installed scope's verdict (Slack pattern for
        # the unscoped default-profile loop).
        try:
            from agent.secret_scope import UnscopedSecretError, get_secret

            try:
                proxy_key = (get_secret("GATEWAY_PROXY_KEY") or "").strip()
            except UnscopedSecretError:
                proxy_key = os.getenv("GATEWAY_PROXY_KEY", "").strip()
        except Exception:
            proxy_key = os.getenv("GATEWAY_PROXY_KEY", "").strip()

        def _run_still_current() -> bool:
            if run_generation is None or not session_key:
                return True
            return self._is_session_run_current(session_key, run_generation)

        # Build messages in OpenAI chat format --------------------------
        #
        # The remote api_server can maintain session continuity via
        # X-Hermes-Session-Id, so it loads its own history.  We only
        # need to send the current user message.  If the remote has
        # no history for this session yet, include what we have locally
        # so the first exchange has context.
        #
        # We always include the current message.  For history, send a
        # compact version (text-only user/assistant turns) — the remote
        # handles tool replay and system prompts.
        api_messages: List[Dict[str, str]] = []

        if context_prompt:
            api_messages.append({"role": "system", "content": context_prompt})

        for msg in history:
            role = msg.get("role")
            content = msg.get("content")
            if role in {"user", "assistant"} and content:
                api_messages.append({"role": role, "content": content})

        api_messages.append({"role": "user", "content": message})

        # HTTP headers ---------------------------------------------------
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if proxy_key:
            headers["Authorization"] = f"Bearer {proxy_key}"
        if session_id:
            headers["X-Hermes-Session-Id"] = session_id

        body = {
            "model": "hermes-agent",
            "messages": api_messages,
            "stream": True,
        }

        # Set up platform streaming if available -------------------------
        _stream_consumer = None
        _scfg = getattr(getattr(self, "config", None), "streaming", None)
        if _scfg is None:
            from gateway.config import StreamingConfig
            _scfg = StreamingConfig()

        platform_key = _platform_config_key(source.platform)
        user_config = _load_gateway_config()
        from gateway.display_config import resolve_display_setting
        _plat_streaming = resolve_display_setting(
            user_config, platform_key, "streaming"
        )
        _streaming_enabled = (
            _scfg.enabled and _scfg.transport != "off"
            if _plat_streaming is None
            else bool(_plat_streaming)
        )

        _thread_metadata: Optional[Dict[str, Any]] = self._thread_metadata_for_source(source, event_message_id)

        if _streaming_enabled:
            try:
                from gateway.stream_consumer import GatewayStreamConsumer
                _adapter = self._adapter_for_source(source)
                if _adapter:
                    _consumer_cfg, _pause_typing_before_finalize = (
                        self._build_stream_consumer_config(
                            source, _scfg, _adapter,
                            on_missing_cursor="fallback",
                        )
                    )
                    _stream_consumer = GatewayStreamConsumer(
                        adapter=_adapter,
                        chat_id=source.chat_id,
                        config=_consumer_cfg,
                        metadata=_thread_metadata,
                        on_before_finalize=_pause_typing_before_finalize,
                        initial_reply_to_id=event_message_id,
                        run_still_current=_run_still_current,
                    )
            except Exception as _sc_err:
                logger.debug("Proxy: could not set up stream consumer: %s", _sc_err)

        # Run the stream consumer task in the background
        stream_task = None
        if _stream_consumer:
            stream_task = asyncio.create_task(_stream_consumer.run())

        # Send typing indicator
        _adapter = self._adapter_for_source(source)
        if _adapter:
            try:
                await _adapter.send_typing(source.chat_id, metadata=_thread_metadata)
            except Exception:
                pass

        # Make the HTTP request with SSE streaming -----------------------
        full_response = ""
        _start = time.time()

        try:
            _timeout = ClientTimeout(total=0, sock_read=1800)
            async with _AioClientSession(timeout=_timeout) as session:
                async with session.post(
                    f"{proxy_url}/v1/chat/completions",
                    json=body,
                    headers=headers,
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.warning(
                            "Proxy error (%d) from %s: %s",
                            resp.status, proxy_url, error_text[:500],
                        )
                        return {
                            "final_response": f"⚠️ Proxy error ({resp.status}): {error_text[:300]}",
                            "messages": [],
                            "api_calls": 0,
                            "tools": [],
                        }

                    # Parse SSE stream
                    buffer = ""
                    async for chunk in resp.content.iter_any():
                        if not _run_still_current():
                            logger.info(
                                "Discarding stale proxy stream for %s — generation %d is no longer current",
                                session_key or "?",
                                run_generation or 0,
                            )
                            return {
                                "final_response": "",
                                "messages": [],
                                "api_calls": 0,
                                "tools": [],
                                "history_offset": len(history),
                                "session_id": session_id,
                                "response_previewed": False,
                            }
                        text = chunk.decode("utf-8", errors="replace")
                        buffer += text

                        # Process complete SSE lines
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()
                            if not line:
                                continue
                            if line.startswith("data: "):
                                data = line[6:]
                                if data.strip() == "[DONE]":
                                    break
                                try:
                                    obj = json.loads(data)
                                    choices = obj.get("choices", [])
                                    if choices:
                                        delta = choices[0].get("delta", {})
                                        content = delta.get("content", "")
                                        if content:
                                            full_response += content
                                            if _stream_consumer:
                                                _stream_consumer.on_delta(content)
                                except json.JSONDecodeError:
                                    pass
                        if len(buffer) > _GATEWAY_PROXY_SSE_BUFFER_MAX_CHARS:
                            raise ValueError(
                                "Proxy SSE stream exceeded max buffer size without a line boundary"
                            )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Proxy connection error to %s: %s", proxy_url, e)
            if not full_response:
                return {
                    "final_response": f"⚠️ Proxy connection error: {e}",
                    "messages": [],
                    "api_calls": 0,
                    "tools": [],
                }
            # Partial response — return what we got
        finally:
            # Finalize stream consumer
            if _stream_consumer:
                _stream_consumer.finish()
            if stream_task:
                try:
                    await asyncio.wait_for(stream_task, timeout=5.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    stream_task.cancel()

        _elapsed = time.time() - _start
        if not _run_still_current():
            logger.info(
                "Discarding stale proxy result for %s — generation %d is no longer current",
                session_key or "?",
                run_generation or 0,
            )
            return {
                "final_response": "",
                "messages": [],
                "api_calls": 0,
                "tools": [],
                "history_offset": len(history),
                "session_id": session_id,
                "response_previewed": False,
            }
        logger.info(
            "proxy response: url=%s session=%s time=%.1fs response=%d chars",
            proxy_url, (session_id or "")[:20], _elapsed, len(full_response),
        )

        return {
            "final_response": full_response or "(No response from remote agent)",
            "messages": [
                {"role": "user", "content": message},
                {"role": "assistant", "content": full_response},
            ],
            "api_calls": 1,
            "tools": [],
            "history_offset": len(history),
            "session_id": session_id,
            "response_previewed": _stream_consumer is not None and bool(full_response),
        }

