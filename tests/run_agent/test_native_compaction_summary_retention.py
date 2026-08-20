"""Tests for native compaction retaining compression summaries preceding a checkpoint."""

from agent.native_compaction import prune_pre_checkpoint_items


def test_prune_pre_checkpoint_items_retains_assistant_summary():
    """Compression summaries with role='assistant' or marked metadata must not be discarded by pre-checkpoint pruning."""
    items = [
        {"role": "user", "content": "Goal: build the system"},
        {"role": "assistant", "content": "Working on it..."},
        {
            "role": "assistant",
            "content": "Summary of previous conversation:\n- Set up database\n- Built API",
            "_compressed_summary": True,
        },
        {"type": "compaction", "encrypted_content": "dummy_checkpoint_blob"},
        {"role": "user", "content": "Now add frontend"},
        {"role": "assistant", "content": "Frontend added."},
    ]

    pruned = prune_pre_checkpoint_items(items, retained_user_token_budget=1000)

    # The checkpoint must be first
    assert pruned[0]["type"] == "compaction"
    
    # Both the early user goal and the compression summary must be retained
    roles_or_types = [m.get("type") or m.get("role") for m in pruned]
    assert "compaction" in roles_or_types
    
    # Check that the summary survives in the pruned output
    summaries = [
        m for m in pruned
        if "Summary of previous conversation" in str(m.get("content", ""))
    ]
    assert len(summaries) == 1
    assert summaries[0]["role"] == "assistant"


def test_prune_pre_checkpoint_items_normal_messages_pruned():
    """Ordinary assistant messages before the checkpoint are still pruned."""
    items = [
        {"role": "user", "content": "U1"},
        {"role": "assistant", "content": "Normal assistant chatter to be pruned"},
        {"type": "compaction", "encrypted_content": "blob"},
        {"role": "user", "content": "U2"},
    ]

    pruned = prune_pre_checkpoint_items(items, retained_user_token_budget=1000)
    contents = [m.get("content") for m in pruned]
    assert "Normal assistant chatter to be pruned" not in contents


def test_prune_pre_checkpoint_items_retains_metadata_flagged_summaries():
    """Assistant messages marked with _is_compression_summary or _hermes_compressed_summary must be retained."""
    items = [
        {"role": "user", "content": "Goal: build system"},
        {
            "role": "assistant",
            "content": "Custom structured summary 1",
            "_is_compression_summary": True,
        },
        {
            "role": "assistant",
            "content": "Custom structured summary 2",
            "_hermes_compressed_summary": True,
        },
        {"type": "compaction", "encrypted_content": "blob"},
        {"role": "user", "content": "Next step"},
    ]

    pruned = prune_pre_checkpoint_items(items, retained_user_token_budget=1000)
    contents = [m.get("content") for m in pruned]
    assert "Custom structured summary 1" in contents
    assert "Custom structured summary 2" in contents
