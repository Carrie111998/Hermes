"""Seam identity for context_compressor_media extract (LB4).

Part of #78645 + #78647.
"""

from agent import context_compressor as cc
from agent import context_compressor_media as cm


def test_all_members_resolve_is_identical_through_godfile():
    members = [
        "_IMAGE_PART_TYPES",
        "_append_text_to_content",
        "_content_has_images",
        "_image_part_label",
        "_is_image_part",
        "_strip_historical_media",
        "_strip_image_parts_from_parts",
        "_strip_images_from_content",
        "_truncate_tool_call_args_json",
    ]
    for m in members:
        assert getattr(cc, m) is getattr(cm, m), f"{m} not is-identical"


def test_no_duplicate_defs_in_godfile():
    from pathlib import Path

    src = Path(cc.__file__).read_text(encoding="utf-8")
    for name in [
        "_append_text_to_content",
        "_strip_image_parts_from_parts",
        "_truncate_tool_call_args_json",
        "_is_image_part",
        "_content_has_images",
        "_strip_images_from_content",
        "_strip_historical_media",
        "_image_part_label",
    ]:
        assert src.count(f"def {name}") == 0, f"duplicate def {name} left in godfile"
    assert src.count("_IMAGE_PART_TYPES = ") == 0
    assert "context_compressor_media" in src


def test_behavior_smoke():
    # append text
    assert cc._append_text_to_content("a", "b") == "ab"
    assert cc._append_text_to_content("a", "b", prepend=True) == "ba"
    assert cc._append_text_to_content(None, "b") == "b"
    # image detection
    assert cc._is_image_part({"type": "image_url", "image_url": {"url": "x"}})
    assert not cc._is_image_part({"type": "text", "text": "x"})
    assert cc._content_has_images([{"type": "image", "image": "x"}])
    assert not cc._content_has_images([{"type": "text", "text": "x"}])
    # strip images from content (replaces with placeholder text part)
    parts = [{"type": "text", "text": "keep"}, {"type": "image_url", "image_url": {"url": "x"}}]
    stripped = cc._strip_image_parts_from_parts(parts)
    assert stripped is not None and len(stripped) == 2
    assert stripped[0]["text"] == "keep"
    assert "removed to save context" in stripped[1]["text"]
    # truncate tool args
    out = cc._truncate_tool_call_args_json('{"a": "' + "x" * 500 + '"}')
    assert len(out) < 300
    # historical media strip
    msgs = [{"role": "user", "content": [{"type": "image", "image": "x"}, {"type": "text", "text": "t"}]}]
    pruned = cc._strip_historical_media(msgs)
    assert pruned == msgs  # recent messages not stripped


def test_import_orders_no_cycle():
    import importlib

    import agent.context_compressor_media as a
    import agent.context_compressor as b

    importlib.reload(a)
    importlib.reload(b)
    assert b._append_text_to_content is a._append_text_to_content
