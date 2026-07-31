"""Tests for GatewayStreamConsumer._clean_for_display — secret redaction.

Regression tests for the streaming-path secret redaction gap.
The streaming path must redact secrets and strip tool-trace banners
in every chunk, including finalized split-message chunks that are
never edited again.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig, _hold_back_credential_prefix_tail


_BOUNDARY_SECRET = "sk-abc123def456ghi789jkl012mno345pqr678stu"


def _overflow_adapter() -> MagicMock:
    """Return an adapter that records the real streaming send/edit paths."""
    adapter = MagicMock()
    adapter.MAX_MESSAGE_LENGTH = 601  # run() clamps its safe streaming limit to 500.
    adapter.REQUIRES_EDIT_FINALIZE = False
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="sent"))
    adapter.edit_message = AsyncMock(return_value=SimpleNamespace(success=True, message_id="edited"))
    return adapter


def _delivered_text(adapter: MagicMock) -> str:
    """Reconstruct visible text in platform delivery order."""
    sent = [call.kwargs["content"] for call in adapter.send.await_args_list]
    edited = [call.kwargs["content"] for call in adapter.edit_message.await_args_list]
    return "".join((*edited, *sent))


def _boundary_crossing_response() -> str:
    """Place a redactable token across the consumer's 500-character split."""
    return "x" * 496 + " " + _BOUNDARY_SECRET + "\ncomplete"


class TestCleanForDisplaySecretRedaction:
    """Verify _clean_for_display redacts secrets and strips banners."""

    def test_media_tags_still_stripped(self):
        """Existing behavior: MEDIA: tags are removed."""
        text = "Hello MEDIA:/path/to/file.png world"
        result = GatewayStreamConsumer._clean_for_display(text)
        assert "MEDIA:" not in result
        assert "Hello" in result
        assert "world" in result

    def test_audio_as_voice_still_stripped(self):
        """Existing behavior: [[audio_as_voice]] directives are removed."""
        text = "Hello [[audio_as_voice]] world"
        result = GatewayStreamConsumer._clean_for_display(text)
        assert "[[audio_as_voice]]" not in result

    def test_normal_text_preserved(self):
        """Normal text without secrets passes through unchanged."""
        text = "Hello world, this is a normal response."
        result = GatewayStreamConsumer._clean_for_display(text)
        assert result == text

    def test_api_key_redacted(self):
        """API keys in streamed text must be redacted."""
        text = f"Here is your key: {_BOUNDARY_SECRET}"
        result = GatewayStreamConsumer._clean_for_display(text)
        # The redactor preserves first 6 + last 4 chars for long tokens
        assert _BOUNDARY_SECRET not in result
        assert "sk-abc" in result  # prefix preserved
        assert "8stu" in result  # suffix preserved

    def test_tool_trace_banner_stripped(self):
        """Tool-trace banners in streamed text must be stripped."""
        text = "Done.\n⚠️ 🛠️ `search repos (agent)` failed"
        result = GatewayStreamConsumer._clean_for_display(text)
        assert result == "Done."
        assert "failed" not in result

    def test_empty_text_returns_empty(self):
        """Empty text returns empty string."""
        result = GatewayStreamConsumer._clean_for_display("")
        assert result == ""

    def test_whitespace_only_returns_empty(self):
        """Whitespace-only text returns empty string after rstrip."""
        result = GatewayStreamConsumer._clean_for_display("   \n\n  ")
        assert result == ""


class TestOverflowSecretRedaction:
    """Overflow must sanitize the complete buffer before splitting it."""

    def test_first_message_overflow_cannot_reconstruct_cross_boundary_secret(self):
        """The no-message overflow branch never delivers reconstructable fragments."""
        adapter = _overflow_adapter()
        consumer = GatewayStreamConsumer(
            adapter,
            "chat_1",
            StreamConsumerConfig(buffer_threshold=1),
        )
        consumer.on_delta(_boundary_crossing_response())
        consumer.finish()

        asyncio.run(consumer.run())

        assert _BOUNDARY_SECRET not in _delivered_text(adapter)

    def test_existing_message_overflow_cannot_reconstruct_cross_boundary_secret(self):
        """The existing-message overflow branch never delivers reconstructable fragments."""
        adapter = _overflow_adapter()
        consumer = GatewayStreamConsumer(
            adapter,
            "chat_1",
            StreamConsumerConfig(buffer_threshold=1),
        )
        consumer._message_id = "preview"
        consumer._already_sent = True
        consumer.on_delta(_boundary_crossing_response())
        consumer.finish()

        asyncio.run(consumer.run())

        assert _BOUNDARY_SECRET not in _delivered_text(adapter)


class _RecordingSendAdapter:
    """Adapter that records sends/edits with distinct message_ids.

    Each send returns a unique message_id so the test can model replacement
    edits properly: the platform keeps only the LAST text for each message
    id, and a sealed finalize=True chunk is immutable.
    """

    def __init__(self, max_len: int = 601):
        self.MAX_MESSAGE_LENGTH = max_len
        self.REQUIRES_EDIT_FINALIZE = False
        self._counter = 0
        self._editable: "dict[str, str]" = {}
        self._editable_order: "list[str]" = []
        self.send = AsyncMock(side_effect=self._send)
        self.edit_message = AsyncMock(side_effect=self._edit)

    async def _send(self, **kw):
        self._counter += 1
        mid = f"s{self._counter}"
        self._editable[mid] = kw["content"]
        self._editable_order.append(mid)
        return SimpleNamespace(success=True, message_id=mid)

    async def _edit(self, **kw):
        if self._editable_order:
            target = self._editable_order[-1]
            self._editable[target] = kw["content"]
        return SimpleNamespace(success=True, message_id=f"e{self._counter}")

    def final_visible_text(self) -> str:
        """Reconstruct what a recipient sees: latest text per message id."""
        return "".join(self._editable[mid] for mid in self._editable_order)


class TestIncrementalStreamingHoldback:
    """P1 regression: incremental deltas must not seal a credential prefix
    before the suffix makes it recognizable.

    See PR review comment #5139200976 — the PR's one-shot fix is bypassable
    under real incremental streaming because the redactor is stateless and
    requires a minimum token length (sk-[A-Za-z0-9_-]{10,}).  A first delta
    ending at the platform split boundary in an unresolved credential prefix
    (" sk-" with <10 trailing chars) was sealed into an immutable
    finalize=True message before the next delta supplied the qualifying
    suffix.

    These tests drive the consumer concurrently: send delta 1, let it run,
    then send delta 2, and reconstruct the final visible state by message
    id (edits replace, sealed sends are immutable).
    """

    @pytest.mark.asyncio
    async def test_incremental_prefix_split_fresh_message(self):
        """Fresh-message branch: the first overflowing delta ending in ' sk-'
        must not seal the prefix; the holdback keeps it in the mutable tail
        until the suffix arrives, then redaction catches the complete token."""
        adapter = _RecordingSendAdapter()
        consumer = GatewayStreamConsumer(
            adapter, "chat_1",
            StreamConsumerConfig(buffer_threshold=1, edit_interval=0.0),
        )
        delta1 = "x" * 498 + " sk-"
        suffix = _BOUNDARY_SECRET[len("sk-"):]

        task = asyncio.create_task(consumer.run())
        consumer.on_delta(delta1)
        for _ in range(50):
            await asyncio.sleep(0.005)
            if consumer._queue.empty():
                break
        consumer.on_delta(suffix)
        consumer.finish()
        await task

        visible = adapter.final_visible_text()
        assert _BOUNDARY_SECRET not in visible, (
            "P1 bypass: incremental streaming reconstructed the raw credential "
            "across sealed messages"
        )

    @pytest.mark.asyncio
    async def test_incremental_prefix_split_existing_message(self):
        """Existing-message branch: same adversarial delta pattern but the
        consumer starts with an active preview message (_message_id set)."""
        adapter = _RecordingSendAdapter()
        consumer = GatewayStreamConsumer(
            adapter, "chat_1",
            StreamConsumerConfig(buffer_threshold=1, edit_interval=0.0),
        )
        consumer._message_id = "preview"
        consumer._already_sent = True

        delta1 = "x" * 498 + " sk-"
        suffix = _BOUNDARY_SECRET[len("sk-"):]

        task = asyncio.create_task(consumer.run())
        consumer.on_delta(delta1)
        for _ in range(50):
            await asyncio.sleep(0.005)
            if consumer._queue.empty():
                break
        consumer.on_delta(suffix)
        consumer.finish()
        await task

        visible = adapter.final_visible_text()
        assert _BOUNDARY_SECRET not in visible, (
            "P1 bypass: incremental streaming reconstructed the raw credential "
            "across sealed messages (existing-message branch)"
        )

    @pytest.mark.asyncio
    async def test_incremental_prefix_split_499_fresh(self):
        """Boundary at prefix_len=499: the split cuts inside ' sk-' leaving
        'sk-' in the tail.  Without holdback the tail would be sealed raw;
        with holdback it stays mutable until the suffix completes the token."""
        adapter = _RecordingSendAdapter()
        consumer = GatewayStreamConsumer(
            adapter, "chat_1",
            StreamConsumerConfig(buffer_threshold=1, edit_interval=0.0),
        )
        delta1 = "x" * 499 + " sk-"
        suffix = _BOUNDARY_SECRET[len("sk-"):]

        task = asyncio.create_task(consumer.run())
        consumer.on_delta(delta1)
        for _ in range(50):
            await asyncio.sleep(0.005)
            if consumer._queue.empty():
                break
        consumer.on_delta(suffix)
        consumer.finish()
        await task

        visible = adapter.final_visible_text()
        assert _BOUNDARY_SECRET not in visible, (
            "P1 bypass: incremental streaming reconstructed the raw credential "
            "at prefix_len=499 (fresh message branch)"
        )


class TestHoldbackHelper:
    """Unit tests for _hold_back_credential_prefix_tail."""

    def test_no_credential_prefix_no_holdback(self):
        """Normal text has no credential-prefix candidate -> full seal."""
        head, tail = _hold_back_credential_prefix_tail("Hello world normal prose.")
        assert tail == ""
        assert head == "Hello world normal prose."

    def test_trailing_sk_prefix_held_back(self):
        """A trailing ' sk-' is the start of an OpenAI key -> held back."""
        head, tail = _hold_back_credential_prefix_tail("x" * 498 + " sk-")
        assert tail == "sk-"
        assert head == "x" * 498 + " "

    def test_trailing_short_sk_token_held_back(self):
        """Short partial credential prefix (' sk-abc', 5 chars below the
        {10,} minimum) is still held back because a future delta could
        extend it into a recognizable credential."""
        head, tail = _hold_back_credential_prefix_tail("filler sk-abc")
        assert tail == "sk-abc"
        assert head == "filler "

    def test_finalize_releases_everything(self):
        """On got_done (finalize=True) there's no future delta, so the
        holdback is a no-op — the caller handles the complete buffer."""
        head, tail = _hold_back_credential_prefix_tail("x" * 498 + " sk-", finalize=True)
        assert tail == ""
        assert head == "x" * 498 + " sk-"

    def test_ghp_prefix_held_back(self):
        """GitHub PAT prefix 'ghp_' is also a credential seed."""
        head, tail = _hold_back_credential_prefix_tail("text ghp_")
        assert tail == "ghp_"
        assert head == "text "

    def test_not_triggered_by_token_continuation(self):
        """'Xsk-abc' (preceded by a letter) is part of a longer token,
        not a credential boundary -> not held back."""
        head, tail = _hold_back_credential_prefix_tail("Xsk-abc")
        assert tail == ""
        assert head == "Xsk-abc"

    def test_codex_gaaaa_equals_sign_held_back(self):
        """Codex tokens (gAAAA...) accept '=' inside their continuation.

        The streaming holdback continuation alphabet must include '=' so that
        a partial Codex token ending in '=' is not sealed into an immutable
        message before a later delta completes it (review comment from
        @egilewski on #56040).
        """
        head, tail = _hold_back_credential_prefix_tail("filler gAAAAabc=")
        assert tail == "gAAAAabc="
        assert head == "filler "

    def test_empty_text(self):
        """Empty text -> empty head and tail."""
        head, tail = _hold_back_credential_prefix_tail("")
        assert head == ""
        assert tail == ""
