"""Comprehensive tests for native compaction summary retention, token budgeting, and robustness."""

from agent.native_compaction import (
    prune_pre_checkpoint_items,
    _is_summary_item,
    _extract_item_text,
)


def test_is_summary_item_robustness():
    """Validates summary detection across metadata dicts, headers, and flags."""
    # Top level flags
    assert _is_summary_item({"_compressed_summary": True}) is True
    assert _is_summary_item({"_is_compression_summary": True}) is True
    assert _is_summary_item({"_hermes_compressed_summary": True}) is True
    assert _is_summary_item({"_my_custom_summary_flag": True}) is True

    # Nested metadata dict
    assert _is_summary_item({"metadata": {"summary": True}}) is True
    assert _is_summary_item({"_metadata": {"is_summary": True}}) is True
    assert _is_summary_item({"metadata": {"_compressed_summary": True}}) is True
    assert _is_summary_item({"metadata": {"compression_v2": True}}) is True

    # Text headers
    assert _is_summary_item({"content": "summary of previous conversation: ..."}) is True
    assert _is_summary_item({"content": "Conversation Summary\nDone"}) is True
    assert _is_summary_item({"content": "Handoff from a previous context"}) is True
    assert _is_summary_item({"content": "## Summary of work"}) is True

    # Negative cases
    assert _is_summary_item({"role": "user", "content": "Just normal prompt"}) is False
    assert _is_summary_item(None) is False
    assert _is_summary_item(123) is False
    assert _is_summary_item({}) is False


def test_extract_item_text_variations():
    """Validates text extraction across string, multipart list, output_text, and malformed structures."""
    # String
    assert _extract_item_text({"content": "Hello world"}) == "Hello world"

    # Multipart list
    item_list = {
        "content": [
            {"type": "input_text", "text": "Part 1"},
            {"type": "text", "text": "Part 2"},
            {"type": "other", "output_text": "Part 3"},
        ]
    }
    assert _extract_item_text(item_list) == "Part 1 Part 2 Part 3"

    # output_text fallback
    assert _extract_item_text({"output_text": "Output fallback"}) == "Output fallback"

    # Malformed / Empty
    assert _extract_item_text({"content": None}) is None
    assert _extract_item_text({"content": []}) is None
    assert _extract_item_text(None) is None
    assert _extract_item_text("string_item") is None


def test_prune_pre_checkpoint_items_retains_summary_and_user_in_order():
    """Preserves chronological order of user messages and summary messages."""
    items = [
        {"role": "user", "content": "User Ask 1"},
        {"role": "assistant", "content": "Conversation Summary: Step 1 complete", "_compressed_summary": True},
        {"role": "user", "content": "User Ask 2"},
        {"role": "assistant", "content": "Normal chatter to prune"},
        {"type": "compaction", "encrypted_content": "blob_cp"},
        {"role": "user", "content": "User Ask 3"},
    ]

    pruned = prune_pre_checkpoint_items(items, retained_user_token_budget=1000)

    # Checkpoint comes first, followed by retained items in original sequence
    assert pruned[0]["type"] == "compaction"
    contents = [m.get("content") for m in pruned[1:]]
    assert contents == [
        "User Ask 1",
        "Conversation Summary: Step 1 complete",
        "User Ask 2",
        "User Ask 3",
    ]


def test_prune_pre_checkpoint_items_summary_budget_cap_and_truncation():
    """Enforces token budget limit and truncation for summaries."""
    long_summary = "Summary line " * 500  # ~6500 chars = ~1625 tokens
    items = [
        {"role": "assistant", "content": long_summary, "_compressed_summary": True},
        {"type": "compaction", "encrypted_content": "blob_cp"},
        {"role": "user", "content": "Ask"},
    ]

    # Restrict summary budget to 100 tokens (~400 chars)
    pruned = prune_pre_checkpoint_items(
        items,
        retained_summary_token_budget=100,
    )

    retained_sum = [m for m in pruned if m.get("_compressed_summary")][0]
    assert len(retained_sum["content"]) <= 400
    assert len(retained_sum["content"]) > 0


def test_prune_pre_checkpoint_items_enable_summary_retention_toggle():
    """Disabling summary retention drops pre-checkpoint summaries."""
    items = [
        {"role": "assistant", "content": "Conversation Summary: Old", "_compressed_summary": True},
        {"type": "compaction", "encrypted_content": "blob"},
        {"role": "user", "content": "New ask"},
    ]

    pruned_disabled = prune_pre_checkpoint_items(items, enable_summary_retention=False)
    contents = [m.get("content") for m in pruned_disabled]
    assert "Conversation Summary: Old" not in contents


def test_prune_pre_checkpoint_items_malformed_inputs_handled_safely():
    """Handles None, non-dict elements, empty lists, and invalid items without crashing."""
    assert prune_pre_checkpoint_items(None) is None
    assert prune_pre_checkpoint_items([]) == []

    items = [
        None,
        123,
        "raw_string",
        {"role": "user", "content": "Valid user ask"},
        {"type": "compaction", "encrypted_content": "blob"},
    ]
    pruned = prune_pre_checkpoint_items(items)
    assert len(pruned) == 2
    assert pruned[0]["type"] == "compaction"
    assert pruned[1]["content"] == "Valid user ask"
