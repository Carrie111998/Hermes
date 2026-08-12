"""Regression tests for ntfy adapter streaming behavior."""
from plugins.platforms.ntfy.adapter import NtfyAdapter


def test_ntfy_adapter_does_not_support_message_editing() -> None:
    """NtfyAdapter.SUPPORTS_MESSAGE_EDITING must be False.

    ntfy publishes immutable notifications — there is no edit API for an
    already-published message, so a streamed preview IS the final message.
    This attribute signals the gateway to suppress the streaming cursor
    instead of stranding a tofu square (▉) in the delivered text, and to
    stop one reply fragmenting across several notifications (#83352).
    """
    assert NtfyAdapter.SUPPORTS_MESSAGE_EDITING is False
