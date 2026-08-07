"""Media-processing methods for ``GatewayRunner``.

Extracted verbatim from ``gateway/run.py`` (god-file decomposition campaign,
issue #54962, Phase 3 mechanical mixin lifts). This mixin holds the inbound
media cluster: image/audio/video classification and enrichment, STT
transcription and transcript echo, native-image buffering, explicit
``MEDIA:`` post-stream delivery, and the image-input-mode routing decision.

Behavior-neutral: every method is lifted verbatim from ``GatewayRunner``.
``self.*`` calls resolve unchanged via the MRO. Imports that come from
``gateway.run`` itself (``_event_media_is_*``, ``_build_document_context_note``,
``_load_gateway_config``, ``_probe_audio_duration``, ``_profile_runtime_scope``,
the ``_DOCKER_*`` constants) are made lazily inside the method that uses them,
so this module never imports ``gateway.run`` at import time -> no import cycle.
The ``_UNSET`` sentinel moves here with the cluster (it is used as a default
argument, so it must resolve at class-definition time); ``gateway.run``
re-exports it. The module-level ``logger`` keeps the original logger name
(``"gateway.run"``) so log records are unchanged.
"""


from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import (
    SessionSource,
    is_shared_multi_user_session,
    neutralize_untrusted_inline_text,
)

# Match the logger run.py uses (logging.getLogger(__name__) where __name__ ==
# "gateway.run") so extracted log records keep their original logger name.
logger = logging.getLogger("gateway.run")

# Sentinel for "caller did not pass metadata" vs "caller passed None".
_UNSET = object()



class GatewayMediaMixin:

    def _warn_if_docker_media_delivery_is_risky(self) -> None:
        """Warn when Docker-backed gateways lack an explicit export mount.

        MEDIA delivery happens in the gateway process, so paths emitted by the model
        must be readable from the host. A plain container-local path like
        `/workspace/report.txt` or `/output/report.txt` often exists only inside
        Docker, so users commonly need a dedicated export mount such as
        `host-dir:/output`.
        """
        from gateway.run import _DOCKER_MEDIA_OUTPUT_CONTAINER_PATHS, _DOCKER_VOLUME_SPEC_RE
        if os.getenv("TERMINAL_ENV", "").strip().lower() != "docker":
            return

        connected = self.config.get_connected_platforms()
        messaging_platforms = [p for p in connected if p not in {Platform.LOCAL, Platform.API_SERVER, Platform.WEBHOOK}]
        if not messaging_platforms:
            return

        raw_volumes = os.getenv("TERMINAL_DOCKER_VOLUMES", "").strip()
        volumes: List[str] = []
        if raw_volumes:
            try:
                parsed = json.loads(raw_volumes)
                if isinstance(parsed, list):
                    volumes = [str(v) for v in parsed if isinstance(v, str)]
            except Exception:
                logger.debug("Could not parse TERMINAL_DOCKER_VOLUMES for gateway media warning", exc_info=True)

        has_explicit_output_mount = False
        for spec in volumes:
            match = _DOCKER_VOLUME_SPEC_RE.match(spec)
            if not match:
                continue
            container_path = match.group("container")
            if container_path in _DOCKER_MEDIA_OUTPUT_CONTAINER_PATHS:
                has_explicit_output_mount = True
                break

        if has_explicit_output_mount:
            return

        logger.warning(
            "Docker backend is enabled for the messaging gateway but no explicit host-visible "
            "output mount (for example '/home/user/.hermes/cache/documents:/output') is configured. "
            "This is fine if the model already emits host-visible paths, but MEDIA file delivery can fail "
            "for container-local paths like '/workspace/...' or '/output/...'."
        )

    async def _prepare_inbound_message_text(
        self,
        *,
        event: MessageEvent,
        source: SessionSource,
        history: List[Dict[str, Any]],
        session_key: Optional[str] = None,
    ) -> Optional[str]:
        """Prepare inbound event text for the agent.

        Keep the normal inbound path and the queued follow-up path on the same
        preprocessing pipeline so sender attribution, image enrichment, STT,
        document notes, reply context, and @ references all behave the same.

        Side effect: buffers per-session native image paths when the active
        model supports native vision AND the user has images attached. The
        caller consumes and clears that session-scoped buffer at the
        ``run_conversation`` site to build a multimodal user turn. When the
        list is empty, the ``_enrich_message_with_vision`` text path has
        already run and images are represented in-text.
        """
        from gateway.run import (_build_document_context_note, _event_media_is_audio,
            _event_media_is_image, _event_media_is_stt_input, _event_media_is_video,
            _load_gateway_config)
        history = history or []
        _pending_stt_prepared = hasattr(event, "_gateway_pending_stt_text")
        message_text = (
            getattr(event, "_gateway_pending_stt_text", None)
            if _pending_stt_prepared
            else event.text
        ) or ""
        _group_sessions_per_user = getattr(self.config, "group_sessions_per_user", True)
        _thread_sessions_per_user = getattr(self.config, "thread_sessions_per_user", False)
        # Prefer the already resolved session key from the caller so this write
        # key matches the consume key at the run_conversation site. Fall back
        # to deriving it here for tests and legacy standalone callers.
        session_key = session_key or self._session_key_for_source(source)
        # Reset only this session's per-call buffer; other sessions may be
        # concurrently preparing multimodal turns on the same runner.
        self._consume_pending_native_image_paths(session_key)

        _is_shared_multi_user = is_shared_multi_user_session(
            source,
            group_sessions_per_user=_group_sessions_per_user,
            thread_sessions_per_user=_thread_sessions_per_user,
        )
        if _is_shared_multi_user and source.user_name:
            # source.user_name is the platform display name — attacker-
            # influenceable on any platform that lets participants set their
            # own name. Neutralize embedded newlines/control chars before
            # interpolating it into every message in the shared session, or
            # a hostile name can masquerade as a fake markdown section
            # (mirrors the same field's treatment in
            # build_session_context_prompt via _format_untrusted_prompt_value).
            _safe_user_name = neutralize_untrusted_inline_text(source.user_name)
            # On Slack, expose the current author's verifiable user ID next to
            # the display name (#17916): "mention me again" requests need a
            # trusted `<@U...>` target for the CURRENT speaker — display names
            # are ambiguous and historical mentions may point at someone else.
            # The user_id comes from the Slack event envelope (not
            # user-editable text), so it does not need neutralization.
            if source.platform == Platform.SLACK and source.user_id:
                _safe_user_name = (
                    f"{_safe_user_name} | Slack user <@{source.user_id}>"
                )
            message_text = f"[{_safe_user_name}] {message_text}"

        # Prepend channel context from history backfill (if any).  This
        # happens after sender-prefix so the prefix only applies to the
        # trigger message, not the backfill block.
        if getattr(event, "channel_context", None):
            message_text = f"{event.channel_context}\n\n[New message]\n{message_text}"

        # Declare at outer scope so the audio-file-paths handling block below
        # remains safe when ``event.media_urls`` is empty (no inner block runs).
        audio_file_paths: list[str] = []
        video_paths: list[str] = []

        if event.media_urls:
            image_paths = []
            audio_paths = []
            for i, path in enumerate(event.media_urls):
                mtype = event.media_types[i] if i < len(event.media_types) else ""
                # Classify images per-attachment: trust this attachment's own
                # MIME, and only honour the message-level PHOTO type when the
                # per-attachment MIME is unknown. Otherwise a document (or any
                # non-image) sent alongside an image in the same message gets
                # mis-routed here as an image and the provider 400s.
                if _event_media_is_image(event, i):
                    image_paths.append(path)
                # MessageType.AUDIO = audio file attachment (e.g. .mp3, .m4a) — never STT
                # MessageType.VOICE = voice message (Opus/OGG) — always STT
                if event.message_type == MessageType.AUDIO:
                    audio_file_paths.append(path)
                elif not _pending_stt_prepared and _event_media_is_stt_input(event, i):
                    audio_paths.append(path)
                if mtype.startswith("video/") or (not mtype and event.message_type == MessageType.VIDEO):
                    video_paths.append(path)

            if image_paths:
                # Decide routing: native (attach pixels) vs text (vision_analyze
                # pre-run + prepend description).  See agent/image_routing.py.
                # Offload to a worker thread: the decision does blocking network
                # I/O — a models.dev fetch on cache miss, and the Ollama
                # ``/api/show`` capability probe for local servers — whose
                # request timeout would otherwise stall the whole gateway event
                # loop (every session) while a single image is routed.
                _img_mode = await asyncio.to_thread(
                    self._decide_image_input_mode,
                    source=source,
                    session_key=session_key,
                )
                if _img_mode == "native":
                    # Defer attachment to the run_conversation call site.
                    self._session_state(
                        session_key
                    ).persistent.native_image_paths = list(image_paths)
                    logger.info(
                        "Image routing: native (model supports vision). %d image(s) will be attached inline.",
                        len(image_paths),
                    )
                else:
                    logger.info(
                        "Image routing: text (mode=%s). Pre-analyzing %d image(s) via vision_analyze.",
                        _img_mode, len(image_paths),
                    )
                    # Vision enrichment runs before AIAgent.run_conversation(),
                    # so bind this session's resolved runtime explicitly rather
                    # than consulting process-global compatibility mirrors.
                    vision_runtime = None
                    try:
                        turn_model, runtime_kwargs = self._resolve_session_agent_runtime(
                            source=source,
                            session_key=session_key,
                        )
                        vision_runtime = dict(runtime_kwargs or {})
                        vision_runtime["model"] = turn_model
                    except Exception:
                        logger.debug(
                            "vision enrichment: session runtime resolution failed",
                            exc_info=True,
                        )

                    from agent.auxiliary_client import scoped_runtime_main

                    with scoped_runtime_main(vision_runtime):
                        message_text = await self._enrich_message_with_vision(
                            message_text,
                            image_paths,
                        )

            if audio_paths:
                message_text, _successful_transcripts = await self._enrich_message_with_transcription(
                    message_text,
                    audio_paths,
                )
                # Echo each successful transcript back to the user immediately
                # when configured. Lets users verify STT quality in real-time,
                # while allowing quiet STT for users who only want the agent to
                # receive the transcription.
                if _successful_transcripts and self._should_echo_stt_transcripts():
                    _echo_adapter = self._adapter_for_source(source)
                    _echo_meta = self._thread_metadata_for_source(source, self._reply_anchor_for_event(event))
                    if _echo_adapter:
                        for _tx in _successful_transcripts:
                            try:
                                await _echo_adapter.send(
                                    source.chat_id,
                                    f'🎙️ "{_tx}"',
                                    metadata=_echo_meta,
                                )
                            except Exception as _echo_exc:
                                logger.debug(
                                    "Transcript echo failed (non-fatal): %s", _echo_exc,
                                )
                # NOTE: Previously, when transcription failed (e.g. no STT
                # provider configured), the gateway also emitted a hardcoded
                # English notice via `_stt_adapter.send()`. That bypassed the
                # LLM and produced two replies — one pre-canned English clip
                # (which TTS then spoke aloud, in the wrong language) and one
                # correct, localized LLM reply from the enriched message text.
                # The enrichment step now leaves a single neutral marker in the
                # prompt, so the LLM produces one coherent reply in the user's
                # language. The hardcoded send has therefore been removed.

        if audio_file_paths:
            from tools.credential_files import to_agent_visible_cache_path as _to_agent_path
            for _apath in audio_file_paths:
                _basename = os.path.basename(_apath)
                _parts = _basename.split("_", 2)
                _display = _parts[2] if len(_parts) >= 3 else _basename
                _display = re.sub(r'[^\w.\- ]', '_', _display)
                _agent_path = _to_agent_path(_apath)
                _note = (
                    f"[The user sent an audio file attachment: '{_display}'. "
                    f"It is saved at: {_agent_path}. "
                    f"Its content is not inlined here. If the user's request involves "
                    f"what the audio contains, transcribe or process it yourself — for "
                    f"example by passing the path to a transcription or media tool — "
                    f"instead of asking the user to describe it. Only ask what to do "
                    f"with it if their intent is genuinely unclear.]"
                )
                message_text = f"{_note}\n\n{message_text}"

        if video_paths:
            from tools.credential_files import to_agent_visible_cache_path as _to_agent_path
            for _vpath in video_paths:
                _basename = os.path.basename(_vpath)
                _parts = _basename.split("_", 2)
                _display = _parts[2] if len(_parts) >= 3 else _basename
                _display = re.sub(r'[^\w.\- ]', '_', _display)
                _agent_path = _to_agent_path(_vpath)
                _note = (
                    f"[The user sent a video attachment: '{_display}'. "
                    f"It is saved at: {_agent_path}. "
                    f"Its content is not inlined here. If the user's request involves "
                    f"what the video contains, inspect or process it yourself — for "
                    f"example by passing the path to a video analysis or media tool — "
                    f"instead of asking the user to describe it. Only ask what to do "
                    f"with it if their intent is genuinely unclear.]"
                )
                message_text = f"{_note}\n\n{message_text}"

        if event.media_urls:
            import mimetypes as _mimetypes
            from tools.credential_files import to_agent_visible_cache_path

            _TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".log", ".json", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg"}
            for i, path in enumerate(event.media_urls):
                # Per-attachment document handling. Skip anything already routed
                # as image / audio / video by the buckets above — only genuine
                # non-media files get a path-pointing context note. This makes a
                # document mixed into a PHOTO/VOICE message (whole-message type
                # != DOCUMENT) still reach the agent as a readable cached file,
                # instead of being silently dropped because the message-level
                # type wasn't DOCUMENT.
                if (
                    _event_media_is_image(event, i)
                    or _event_media_is_audio(event, i)
                    or _event_media_is_video(event, i)
                ):
                    continue
                mtype = event.media_types[i] if i < len(event.media_types) else ""
                if mtype in {"", "application/octet-stream"}:
                    _ext = os.path.splitext(path)[1].lower()
                    if _ext in _TEXT_EXTENSIONS:
                        mtype = "text/plain"
                    else:
                        guessed, _ = _mimetypes.guess_type(path)
                        if guessed:
                            mtype = guessed
                        else:
                            mtype = "application/octet-stream"
                # Any accepted file gets a path-pointing context note — we accept
                # all file types now, so a non-text/non-application MIME (font/*,
                # model/*, etc.) must still tell the agent the file exists.

                basename = os.path.basename(path)
                parts = basename.split("_", 2)
                display_name = parts[2] if len(parts) >= 3 else basename
                display_name = re.sub(r'[^\w.\- ]', '_', display_name)

                # Translate host cache path to in-container path if running under Docker backend.
                # This ensures the agent receives a path it can open inside its sandbox, as the
                # cache directories are auto-mounted at /root/.hermes/cache/* by get_cache_directory_mounts().
                agent_path = to_agent_visible_cache_path(path)

                context_note = _build_document_context_note(display_name, agent_path, mtype)
                message_text = f"{context_note}\n\n{message_text}"

        # Discord: surface the triggering message id per-turn on the user
        # message rather than in the cached system prompt. message_id changes
        # every turn, so baking it into build_session_context_prompt() would
        # bust the agent-cache signature and rebuild the AIAgent every message
        # (destroying prompt caching). The static IDs block points the agent
        # here; the volatile id rides the per-turn user content.
        if (
            source is not None
            and getattr(source, "platform", None) == Platform.DISCORD
            and getattr(event, "message_id", None)
        ):
            from gateway.session import _discord_tools_loaded as _disc_tools_loaded
            if _disc_tools_loaded():
                message_text = (
                    f"[Triggering message id: `{event.message_id}` — use as "
                    f"`message_id` for reply/react/pin via the discord tools.]\n\n"
                    f"{message_text}"
                )

        if getattr(event, "reply_to_text", None) and event.reply_to_message_id:
            # Always inject the reply-to pointer — even when the quoted text
            # already appears in history. The prefix isn't deduplication, it's
            # disambiguation: it tells the agent *which* prior message the user
            # is referencing. History can contain the same or similar text
            # multiple times, and without an explicit pointer the agent has to
            # guess (or answer for both subjects). Token overhead is minimal.
            reply_snippet = event.reply_to_text[:500]
            if getattr(event, "reply_to_is_own_message", False):
                message_text = (
                    f'[Replying to your previous message: "{reply_snippet}"]\n\n'
                    f"{message_text}"
                )
            else:
                message_text = f'[Replying to: "{reply_snippet}"]\n\n{message_text}'

        if "@" in message_text:
            try:
                from agent.context_references import preprocess_context_references_async
                from agent.model_metadata import get_model_context_length_async

                _msg_cwd = os.environ.get("TERMINAL_CWD", os.path.expanduser("~"))
                _msg_config_ctx = None
                _msg_cfg = None
                _msg_model_cfg = {}
                _msg_custom_providers = []
                try:
                    _msg_cfg = _load_gateway_config()
                    _msg_model_cfg = _msg_cfg.get("model", {})
                    if isinstance(_msg_model_cfg, dict):
                        _msg_raw_ctx = _msg_model_cfg.get("context_length")
                        if _msg_raw_ctx is not None:
                            _msg_config_ctx = int(_msg_raw_ctx)
                    try:
                        from hermes_cli.config import get_compatible_custom_providers

                        _msg_custom_providers = get_compatible_custom_providers(_msg_cfg)
                    except Exception:
                        _msg_custom_providers = _msg_cfg.get("custom_providers") or []
                except Exception:
                    pass
                # Resolve the session's actual model/provider/base_url the
                # same way the hygiene compression block does (~11080).
                # GatewayRunner has no self._model/self._base_url attrs
                # (that was copy-pasted from HermesCLI, which does carry
                # self.model/self.base_url), so using them here always raised
                # AttributeError, silently caught below, meaning this feature
                # never ran.
                _msg_model, _msg_runtime = self._resolve_session_agent_runtime(
                    source=source,
                    session_key=session_key,
                    user_config=_msg_cfg,
                )
                _msg_base_url = _msg_runtime.get("base_url") or ""
                # A global model.context_length belongs to the configured
                # model, not a session /model or channel override. Prefer a
                # matching per-custom-provider model limit when available.
                _msg_configured_model = (
                    _msg_model_cfg.get("default") or _msg_model_cfg.get("model")
                    if isinstance(_msg_model_cfg, dict)
                    else _msg_model_cfg
                )
                if _msg_model != _msg_configured_model:
                    _msg_config_ctx = None
                if _msg_config_ctx is not None and isinstance(_msg_model_cfg, dict):
                    try:
                        from hermes_cli.route_identity import should_clear_context_pin_async

                        if await should_clear_context_pin_async(
                            None,  # model match already checked above
                            None,
                            _msg_model_cfg.get("base_url"),
                            _msg_base_url,
                            _msg_model_cfg.get("provider"),
                            _msg_runtime.get("provider"),
                        ):
                            _msg_config_ctx = None
                    except Exception:
                        _msg_config_ctx = None
                if _msg_custom_providers and _msg_base_url:
                    try:
                        from hermes_cli.config import get_custom_provider_context_length

                        _msg_custom_ctx = get_custom_provider_context_length(
                            model=_msg_model,
                            base_url=_msg_base_url,
                            custom_providers=_msg_custom_providers,
                        )
                        if _msg_custom_ctx:
                            _msg_config_ctx = _msg_custom_ctx
                    except Exception:
                        pass
                _msg_ctx_len = await get_model_context_length_async(
                    _msg_model,
                    base_url=_msg_base_url,
                    api_key=_msg_runtime.get("api_key") or "",
                    config_context_length=_msg_config_ctx,
                    provider=_msg_runtime.get("provider") or "",
                    custom_providers=_msg_custom_providers,
                )
                _ctx_result = await preprocess_context_references_async(
                    message_text,
                    cwd=_msg_cwd,
                    context_length=_msg_ctx_len,
                    allowed_root=_msg_cwd,
                )
                if _ctx_result.blocked:
                    _adapter = self._adapter_for_source(source)
                    if _adapter:
                        await _adapter.send(
                            source.chat_id,
                            "\n".join(_ctx_result.warnings) or "Context injection refused.",
                        )
                    return None
                if _ctx_result.expanded:
                    message_text = _ctx_result.message
            except Exception as exc:
                logger.warning("@ context reference expansion failed: %s", exc)
                logger.debug("@ context reference expansion failure detail", exc_info=True)

        return message_text

    async def _prepare_profile_scoped_inbound_message_text(
        self,
        *,
        event: MessageEvent,
        source: SessionSource,
        history: List[Dict[str, Any]],
        session_key: Optional[str] = None,
    ) -> Optional[str]:
        """Run inbound preprocessing under the routed profile when multiplexed."""
        from gateway.run import _profile_runtime_scope
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            with _profile_runtime_scope(self._resolve_profile_home_for_source(source)):
                return await self._prepare_inbound_message_text(
                    event=event,
                    source=source,
                    history=history,
                    session_key=session_key,
                )
        return await self._prepare_inbound_message_text(
            event=event,
            source=source,
            history=history,
            session_key=session_key,
        )

    async def _prepare_clarify_reply_text(self, event) -> str:
        """Return raw text or successful voice transcripts for a clarify reply."""
        if not self._pending_event_audio_paths(event):
            return (event.text or "").strip()

        _, successful_transcripts = await self._transcribe_pending_audio_event_once(
            event, "",
        )
        return "\n\n".join(
            transcript.strip()
            for transcript in successful_transcripts
            if transcript.strip()
        )

    def _consume_pending_native_image_paths(self, session_key: str) -> List[str]:
        state = self._peek_session_state(session_key)
        if state is None or not state.persistent.native_image_paths:
            return []
        paths = list(state.persistent.native_image_paths)
        state.persistent.native_image_paths = []
        return paths

    def _should_echo_stt_transcripts(self) -> bool:
        """Return whether inbound voice/STT transcripts should be echoed to chat."""
        return bool(getattr(self.config, "stt_echo_transcripts", True))

    async def _deliver_media_from_response(
        self,
        response: str,
        event: MessageEvent,
        adapter,
    ) -> None:
        """Extract explicit MEDIA: tags from a response and deliver them.

        Called after streaming has already sent the text to the user, so the
        text itself is already delivered — this only handles file attachments
        that the normal _process_message_background path would have caught.

        Unlike the non-streaming path in ``gateway/platforms/base.py`` (which
        also auto-detects bare local paths via ``extract_local_files``), this
        post-stream rescan is EXPLICIT-ONLY. The visible reply has already
        been streamed verbatim, so a bare path string here was either (a)
        already shown to the user as text, or (b) stale tool/inspected
        content that was never part of the intended visible reply. Promoting
        such paths into uploads after the fact sent files the model never
        asked to deliver (#20834). Only ``MEDIA:`` directives — the explicit
        attachment contract — trigger post-stream uploads.
        """
        from pathlib import Path
        from urllib.parse import quote as _quote

        try:
            # Capture [[as_document]] before extract_media strips it, so the
            # dispatch partition below can route image-extension files
            # through send_document (preserving bytes) instead of
            # send_multiple_images (Telegram sendPhoto recompresses to ~1280px).
            force_document_attachments = "[[as_document]]" in response

            from gateway.platforms.base import BasePlatformAdapter, should_send_media_as_audio

            media_files, cleaned = adapter.extract_media(response)
            media_files = BasePlatformAdapter.filter_media_delivery_paths(media_files)
            # Do NOT deduplicate explicit MEDIA tags against prior turns here
            # (#73771). This rescan is already EXPLICIT-ONLY (see docstring):
            # a MEDIA: directive in the final streamed reply is the model
            # deliberately attaching a file — including a user-requested
            # resend. Stale auto-appended tags are deduped upstream in
            # _collect_auto_append_media_tags with history_media_paths.
            # Mirrors the same filter removal on the non-streaming path in
            # gateway/platforms/base.py.
            # Strip image URLs from the cleaned text for parity with the
            # non-streaming chain, but do NOT run extract_local_files here:
            # post-stream delivery is explicit-only (#20834). Bare local paths
            # in an already-streamed reply are text the user has seen (or
            # stale inspected content), not an attachment request.
            adapter.extract_images(cleaned)

            _thread_meta = self._thread_metadata_for_source(event.source, self._reply_anchor_for_event(event))

            _VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.3gp'}
            _IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}

            # Partition out images so they can be sent as a single batch
            # (e.g. Signal's multi-attachment RPC). When [[as_document]] was
            # set, image-extension files skip the photo path and route to
            # send_document below — preserving original bytes.
            image_paths: list = []
            non_image_media: list = []
            for media_path, is_voice in media_files:
                ext = Path(media_path).suffix.lower()
                if (ext in _IMAGE_EXTS
                        and not is_voice
                        and not force_document_attachments):
                    image_paths.append(media_path)
                else:
                    non_image_media.append((media_path, is_voice))

            if image_paths:
                try:
                    images = [(f"file://{_quote(p)}", "") for p in image_paths]
                    await adapter.send_multiple_images(
                        chat_id=event.source.chat_id,
                        images=images,
                        metadata=_thread_meta,
                    )
                except Exception as e:
                    logger.warning("[%s] Post-stream image batch delivery failed: %s", adapter.name, e)

            for media_path, is_voice in non_image_media:
                try:
                    ext = Path(media_path).suffix.lower()
                    if should_send_media_as_audio(event.source.platform, ext, is_voice=is_voice):
                        await adapter.send_voice(
                            chat_id=event.source.chat_id,
                            audio_path=media_path,
                            metadata=_thread_meta,
                        )
                    elif ext in _VIDEO_EXTS:
                        await adapter.send_video(
                            chat_id=event.source.chat_id,
                            video_path=media_path,
                            metadata=_thread_meta,
                        )
                    else:
                        await adapter.send_document(
                            chat_id=event.source.chat_id,
                            file_path=media_path,
                            metadata=_thread_meta,
                        )
                except Exception as e:
                    logger.warning("[%s] Post-stream media delivery failed: %s", adapter.name, e)

        except Exception as e:
            logger.warning("Post-stream media extraction failed: %s", e)

    def _decide_image_input_mode(
        self,
        *,
        source: Optional[SessionSource] = None,
        session_key: Optional[str] = None,
        user_config: Optional[dict] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> str:
        """Resolve image-input routing for the effective model this turn.

        Returns ``"native"`` (attach pixels on the user turn) or ``"text"``
        (pre-analyze with vision_analyze and prepend the description). See
        agent/image_routing.py for the full decision table.

        Gateway sessions can have /model overrides that live outside
        config.yaml. Image preprocessing runs before AIAgent sets the
        auxiliary_client runtime globals, so resolve the same per-session
        runtime bundle the upcoming agent turn will use instead of consulting
        only the persisted default model.
        """
        try:
            from agent.image_routing import decide_image_input_mode
            from agent.auxiliary_client import _read_main_model, _read_main_provider
            from hermes_cli.config import load_config

            cfg = user_config if isinstance(user_config, dict) else load_config()
            resolved_provider = (provider or "").strip()
            resolved_model = (model or "").strip()
            resolved_requested_provider = ""

            needs_session_runtime = not resolved_provider or not resolved_model
            has_session_identity = source is not None or session_key
            if needs_session_runtime and has_session_identity:
                try:
                    turn_model, runtime_kwargs = self._resolve_session_agent_runtime(
                        source=source,
                        session_key=session_key,
                        user_config=cfg,
                    )
                    if not resolved_model and isinstance(turn_model, str):
                        resolved_model = turn_model.strip()
                    runtime_provider = runtime_kwargs.get("provider") if isinstance(runtime_kwargs, dict) else None
                    runtime_requested_provider = (
                        runtime_kwargs.get("requested_provider")
                        if isinstance(runtime_kwargs, dict)
                        else None
                    )
                    if not resolved_provider and isinstance(runtime_provider, str):
                        resolved_provider = runtime_provider.strip()
                    if isinstance(runtime_requested_provider, str):
                        resolved_requested_provider = runtime_requested_provider.strip()
                except Exception as exc:
                    logger.debug(
                        "image_routing: session runtime resolution failed, falling back to config — %s",
                        exc,
                    )

            if not resolved_provider:
                resolved_provider = _read_main_provider()
            if not resolved_model:
                resolved_model = _read_main_model()

            return decide_image_input_mode(
                resolved_provider,
                resolved_model,
                cfg,
                requested_provider=resolved_requested_provider,
            )
        except Exception as exc:
            logger.debug("image_routing: decision failed, falling back to text — %s", exc)
            return "text"

    async def _enrich_message_with_vision(
        self,
        user_text: str,
        image_paths: List[str],
    ) -> str:
        """
        Auto-analyze user-attached images with the vision tool and prepend
        the descriptions to the message text.

        Each image is analyzed with a general-purpose prompt.  The resulting
        description *and* the local cache path are injected so the model can:
          1. Immediately understand what the user sent (no extra tool call).
          2. Re-examine the image with vision_analyze if it needs more detail.

        Args:
            user_text:   The user's original caption / message text.
            image_paths: List of local file paths to cached images.

        Returns:
            The enriched message string with vision descriptions prepended.
        """
        from tools.vision_tools import vision_analyze_tool
        from agent.memory_manager import sanitize_context

        analysis_prompt = (
            "Describe everything visible in this image in thorough detail. "
            "Include any text, code, data, objects, people, layout, colors, "
            "and any other notable visual information."
        )

        enriched_parts = []
        for path in image_paths:
            try:
                logger.debug("Auto-analyzing user image: %s", path)
                result_json = await vision_analyze_tool(
                    image_url=path,
                    user_prompt=analysis_prompt,
                )
                result = json.loads(result_json)
                if result.get("success"):
                    description = result.get("analysis", "")
                    description = sanitize_context(description)
                    enriched_parts.append(
                        f"[The user sent an image~ Here's what I can see:\n{description}]\n"
                        f"[If you need a closer look, use vision_analyze with "
                        f"image_url: {path} ~]"
                    )
                else:
                    enriched_parts.append(
                        "[The user sent an image but I couldn't quite see it "
                        "this time (>_<) You can try looking at it yourself "
                        f"with vision_analyze using image_url: {path}]"
                    )
            except Exception as e:
                logger.error("Vision auto-analysis error: %s", e)
                enriched_parts.append(
                    f"[The user sent an image but something went wrong when I "
                    f"tried to look at it~ You can try examining it yourself "
                    f"with vision_analyze using image_url: {path}]"
                )

        # Combine: vision descriptions first, then the user's original text
        if enriched_parts:
            prefix = "\n\n".join(enriched_parts)
            if user_text:
                return f"{prefix}\n\n{user_text}"
            return prefix
        return user_text

    async def _enrich_message_with_transcription(
        self,
        user_text: str,
        audio_paths: List[str],
    ) -> tuple[str, List[str]]:
        """
        Auto-transcribe user voice/audio messages using the configured STT provider
        and prepend the transcript to the message text.

        Args:
            user_text:   The user's original caption / message text.
            audio_paths: List of local file paths to cached audio files.

        Returns:
            A tuple of ``(enriched_text, successful_transcripts)``:
              - ``enriched_text``: the message string with transcription wrappers
                prepended (same as before).
              - ``successful_transcripts``: the raw transcript strings for audio
                clips that were successfully transcribed, in input order. Empty
                list if every clip failed or STT is disabled. Callers can use
                this to echo transcripts back to the user before the agent loop.
        """
        from gateway.run import _probe_audio_duration
        seen = set()
        audio_paths = [p for p in audio_paths if p not in seen and not seen.add(p)]
        if not getattr(self.config, "stt_enabled", True):
            notes = []
            for path in audio_paths:
                abs_path = os.path.abspath(path)
                duration_str = await _probe_audio_duration(abs_path)
                if duration_str:
                    notes.append(
                        f"[The user sent a voice message: {abs_path} (duration: {duration_str})]"
                    )
                else:
                    notes.append(f"[The user sent a voice message: {abs_path}]")
            if not notes:
                return user_text, []
            prefix = "\n\n".join(notes)
            _placeholder = "(The user sent a message with no text content)"
            if user_text and user_text.strip() == _placeholder:
                return prefix, []
            if user_text:
                return f"{prefix}\n\n{user_text}", []
            return prefix, []

        try:
            from tools.transcription_tools import (
                transcribe_audio,
                transcribe_audio_local_fallback,
            )
        except ModuleNotFoundError as e:
            logger.error("Transcription module unavailable: %s", e)
            unavailable_note = "[voice message could not be transcribed]"
            _placeholder = "(The user sent a message with no text content)"
            if user_text and user_text.strip() == _placeholder:
                return unavailable_note, []
            if user_text:
                return f"{unavailable_note}\n\n{user_text}", []
            return unavailable_note, []

        enriched_parts = []
        successful_transcripts: List[str] = []
        for path in audio_paths:
            try:
                logger.debug("Transcribing user voice: %s", path)
                result = await asyncio.to_thread(transcribe_audio, path)
                if not result.get("success"):
                    fallback = await asyncio.to_thread(
                        transcribe_audio_local_fallback,
                        path,
                    )
                    if fallback.get("success"):
                        logger.info(
                            "Configured STT failed for %s; recovered with local STT",
                            path,
                        )
                        result = fallback
                if result["success"]:
                    transcript = result["transcript"]
                    # Speech-to-text can return success=True with an empty or
                    # whitespace-only transcript on silence, cut-off, or
                    # inaudible audio. Emitting empty quotes ('""') makes the
                    # agent reply to nothing and can loop, so that case gets a
                    # clear sentinel note instead (#41603).
                    if not (transcript or "").strip():
                        enriched_parts.append(
                            "[The user sent a voice message but it came through "
                            "empty or inaudible — speech-to-text returned no "
                            "words. Do not guess at the content; ask the user "
                            "to resend or type it out.]"
                        )
                        continue
                    successful_transcripts.append(transcript)
                    # Pass the transcript through as a plain quoted line. The
                    # earlier wording ("The user sent a voice message~ Here's
                    # what they said: ...") read as a meta-instruction and made
                    # the LLM volunteer commentary about voice mode rather than
                    # reply to the content.
                    enriched_parts.append(f'"{transcript}"')
                else:
                    error = result.get("error", "unknown error")
                    # All failure branches: a single, minimal, neutral marker.
                    # Do NOT mention "no STT provider configured", "setup
                    # instructions", or the "hermes-agent-setup" skill, and do
                    # NOT claim a direct message was sent — those phrases get
                    # persisted in conversation history and poison every later
                    # turn, so the model keeps volunteering STT-setup advice
                    # even after transcription starts working. The cause is
                    # logged for operator diagnosis but kept out of the
                    # LLM-visible prompt.
                    logger.info("Voice transcription failed for %s: %s", path, error)
                    from tools.credential_files import to_agent_visible_cache_path

                    agent_path = to_agent_visible_cache_path(os.path.abspath(path))
                    enriched_parts.append(
                        "[voice message could not be transcribed automatically; "
                        f"the audio is available at: {agent_path}]"
                    )
            except Exception as e:
                logger.error("Transcription error: %s", e)
                from tools.credential_files import to_agent_visible_cache_path

                agent_path = to_agent_visible_cache_path(os.path.abspath(path))
                enriched_parts.append(
                    "[voice message could not be transcribed automatically; "
                    f"the audio is available at: {agent_path}]"
                )

        if enriched_parts:
            prefix = "\n\n".join(enriched_parts)
            # Strip the empty-content placeholder from the Discord adapter
            # when we successfully transcribed the audio — it's redundant.
            _placeholder = "(The user sent a message with no text content)"
            if user_text and user_text.strip() == _placeholder:
                return prefix, successful_transcripts
            if user_text:
                return f"{prefix}\n\n{user_text}", successful_transcripts
            return prefix, successful_transcripts
        return user_text, successful_transcripts

    def _pending_event_audio_paths(self, event) -> List[str]:
        """Return STT-eligible paths from a pending voice message."""
        from gateway.run import _event_media_is_stt_input
        audio_paths: List[str] = []
        media_urls = getattr(event, "media_urls", None) or []
        for i, path in enumerate(media_urls):
            if _event_media_is_stt_input(event, i):
                audio_paths.append(path)
        return audio_paths

    async def _transcribe_pending_audio_event_once(
        self,
        event,
        user_text: Optional[str] = None,
    ) -> tuple[str | None, List[str]]:
        """Transcribe a pending audio event once and cache the result on the event.

        Voice follow-ups can be inspected first by the interrupt monitor and
        later consumed by the pending-drain path.  Both need the same transcript,
        but only one STT call and one transcript echo should happen for the
        platform message.
        """
        if hasattr(event, "_gateway_pending_stt_text"):
            cached_text = getattr(event, "_gateway_pending_stt_text")
            cached_transcripts = getattr(event, "_gateway_pending_stt_transcripts", []) or []
            return cached_text, list(cached_transcripts)

        audio_paths = self._pending_event_audio_paths(event)
        if not audio_paths:
            return user_text if user_text is not None else (getattr(event, "text", None) or None), []

        text = user_text if user_text is not None else (getattr(event, "text", "") or "")
        enriched_text, successful_transcripts = await self._enrich_message_with_transcription(
            text,
            audio_paths,
        )
        setattr(event, "_gateway_pending_stt_text", enriched_text)
        setattr(event, "_gateway_pending_stt_transcripts", list(successful_transcripts))
        return enriched_text, successful_transcripts

    async def _echo_pending_stt_transcripts_once(
        self,
        event,
        adapter,
        source,
        transcripts: List[str],
        *,
        metadata=None,
        log_context: str = "Transcript",
    ) -> None:
        """Echo pending-event STT transcripts to the chat at most once.

        The already-echoed transcripts are tracked as a COUNT rather than a
        single boolean.  ``merge_pending_message_event`` can append a second
        voice note to an event whose first transcript was already echoed and
        invalidates the transcription cache; the re-run transcription then
        returns the earlier transcripts as a prefix of the new list, so
        echoing only the unsent tail suppresses the repeat while still
        surfacing the newly merged note.  A count rather than a set of seen
        values because two separate notes that transcribe identically are two
        distinct deliveries and both must be echoed.
        """
        if (
            not transcripts
            or not self._should_echo_stt_transcripts()
            or adapter is None
        ):
            return
        already_echoed = int(getattr(event, "_gateway_pending_stt_echoed", 0) or 0)
        unsent = transcripts[already_echoed:]
        setattr(event, "_gateway_pending_stt_echoed", already_echoed + len(unsent))
        for tx in unsent:
            try:
                await adapter.send(
                    source.chat_id,
                    f'🎙️ "{tx}"',
                    metadata=metadata,
                )
            except Exception as echo_exc:
                logger.debug("%s echo failed (non-fatal): %s", log_context, echo_exc)

    async def _transcribe_and_echo_pending_voice(
        self,
        event,
        adapter,
        source,
        text: str,
        *,
        log_context: str,
        metadata=_UNSET,
    ) -> tuple[str, List[str]]:
        """Transcribe a pending voice event and echo transcripts once.

        Unified helper for all interrupt/monitor/backup/drain paths that need
        to transcribe a pending voice event and echo the transcript to chat.
        Returns ``(enriched_text, transcripts)`` so the caller can feed the
        enriched text into ``agent.interrupt()`` or the pending-drain flow.

        If the event has no STT-eligible media, returns ``(text, [])`` unchanged.
        The caller is responsible for the ``_build_media_placeholder`` fallback
        when ``text`` is empty and the event has non-audio media.
        """
        if not self._pending_event_audio_paths(event):
            return text, []
        try:
            enriched_text, transcripts = await self._transcribe_pending_audio_event_once(
                event,
                text,
            )
            echo_meta = self._thread_metadata_for_source(
                source,
                self._reply_anchor_for_event(event),
            ) if metadata is _UNSET else metadata
            await self._echo_pending_stt_transcripts_once(
                event,
                adapter,
                source,
                transcripts,
                metadata=echo_meta,
                log_context=log_context,
            )
            return enriched_text or text, transcripts
        except Exception as trans_exc:
            logger.warning("%s transcription failed: %s", log_context, trans_exc)
            return text, []

