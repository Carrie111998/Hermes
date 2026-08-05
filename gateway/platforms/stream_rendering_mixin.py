"""Structured stream-event rendering for platform adapters.

Extracted verbatim from ``gateway/platforms/base.py`` (godfile decomposition
wave 1, shard s3, cluster c5: ``render_message_event``, ``format_tool_event``, ``format_tool_preview``).  The mixin is a base of
``BasePlatformAdapter``; heavy dependencies (``gateway.stream_events``, ``agent.display``) are
imported inside the method bodies exactly as the original did, so this
module never imports them at module scope.
"""

from __future__ import annotations

from typing import Any, Optional


class StreamRenderingMixin:
    # ── Structured stream-event rendering ────────────────────────────────
    #
    # These methods let an adapter decide *how* to present each structured
    # streaming event (see gateway/stream_events.py).  The default
    # implementations reproduce the historical behavior exactly: assistant
    # text/commentary/segment events delegate to the stream consumer, and
    # tool events render the same "emoji tool_name: preview" chrome the
    # gateway has always produced.  Adapters override these to be more native
    # to their platform (e.g. Telegram streaming a MarkdownV2 ```bash``` block
    # as a draft; iMessage eating tool chrome it cannot format).
    #
    # The contract is presentation-only: nothing rendered here is persisted to
    # conversation history.  History is owned by the agent; what an adapter
    # chooses to "eat" must never change the bytes the agent stored.

    def render_message_event(self, event: Any, sink: Any) -> None:
        """Render a MessageChunk / MessageStop / Commentary onto the sink.

        Default: map onto the stream consumer's existing primitives, preserving
        today's behavior 1:1.  ``sink`` is a GatewayStreamConsumer.
        """
        from gateway.stream_events import MessageChunk, MessageStop, Commentary

        if isinstance(event, MessageChunk):
            if event.text:
                sink.on_delta(event.text)
        elif isinstance(event, MessageStop):
            # An intermediate stop (text → tool → text) is a segment break;
            # the terminal stop is signalled by the gateway via finish(),
            # not here, so we only break segments on non-final stops.
            if not event.final:
                sink.on_segment_break()
        elif isinstance(event, Commentary):
            if event.text:
                sink.on_commentary(event.text)

    def format_tool_event(self, event: Any, *, mode: str = "all",
                          preview_max_len: int = 40) -> Optional[str]:
        """Return the rendered chrome for a ToolCallChunk, or None to eat it.

        Reproduces the gateway's historical tool-progress formatting: an emoji
        for the tool, the tool name, and a short argument preview (or the full
        args dict in ``verbose`` mode).  Adapters that cannot render tool chrome
        (no message editing, plain-text only) should override to return None so
        the event is dropped rather than spamming separate bubbles.

        ``mode`` is the resolved tool-progress mode ("all" / "new" / "verbose");
        ``preview_max_len`` mirrors the ``tool_preview_length`` config (0 means
        "no cap" in verbose mode).
        """
        from gateway.stream_events import ToolCallChunk
        if not isinstance(event, ToolCallChunk):
            return None

        from agent.display import get_tool_emoji
        emoji = get_tool_emoji(event.tool_name, default="⚙️")

        if mode == "verbose":
            if event.args:
                import json
                args_str = json.dumps(event.args, ensure_ascii=False, default=str)
                if preview_max_len > 0 and len(args_str) > preview_max_len:
                    args_str = args_str[:preview_max_len - 3] + "..."
                return f"{emoji} {event.tool_name}({list(event.args.keys())})\n{args_str}"
            if event.preview:
                return f"{emoji} {event.tool_name}: \"{event.preview}\""
            return f"{emoji} {event.tool_name}..."

        # "all" / "new": short preview, capped (default 40 to keep gateway
        # progress bubbles compact — they persist as permanent messages).
        preview = event.preview
        if preview:
            from agent.display import prepare_tool_preview

            cap = preview_max_len if preview_max_len > 0 else 40
            prepared = prepare_tool_preview(
                event.tool_name,
                event.args,
                fallback=preview,
                max_len=cap,
            )
            rendered = self.format_tool_preview(prepared)
            return f"{emoji} {event.tool_name}: \"{rendered}\""
        return f"{emoji} {event.tool_name}..."

    def format_tool_preview(self, preview: "ToolPreview") -> str:
        """Apply platform-native formatting to a compact tool preview.

        Most adapters only need the compact text. Rich-text adapters can use
        the preview's explicit metadata to preserve details such as a URL that
        was shortened for display.
        """
        return preview.text

