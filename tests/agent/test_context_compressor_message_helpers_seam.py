"""Seam contracts for the message-marker helper extraction."""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import agent.context_compressor as owner
import pytest


ROOT = Path(__file__).parents[2]
EXPECTED_BYTES = 3204
EXPECTED_SHA256 = "8c6860876e9a511927f9eecffce5bb878179d590218df284edd48c3cf3adc31d"
HELPER_NAMES = {
    "_fresh_compaction_message_copy",
    "_template_visible_role",
    "_strip_persistence_markers",
}


@pytest.fixture()
def extracted():
    import agent.context_compressor_message_helpers as helpers

    return helpers


def test_leaf_source_is_cycle_free_exact_move_and_only_definition_owner(extracted):
    source = inspect.getsource(extracted)
    helper_tree = ast.parse(source)
    owner_tree = ast.parse(
        (ROOT / "agent" / "context_compressor.py").read_text(encoding="utf-8")
    )

    imported_modules = {
        alias.name
        for node in ast.walk(helper_tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "agent.context_compressor" not in imported_modules

    owner_definitions = {
        node.name
        for node in owner_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    helper_definitions = {
        node.name
        for node in helper_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert HELPER_NAMES.isdisjoint(owner_definitions)
    assert HELPER_NAMES <= helper_definitions

    helper_bytes = (
        ROOT / "agent" / "context_compressor_message_helpers.py"
    ).read_bytes()
    approved = helper_bytes[helper_bytes.index(b"def _fresh_compaction_message_copy") :]
    assert len(approved) == EXPECTED_BYTES
    assert hashlib.sha256(approved).hexdigest() == EXPECTED_SHA256


def test_original_module_reexports_extracted_function_objects(extracted):
    for name in (
        "_fresh_compaction_message_copy",
        "_template_visible_role",
        "_strip_persistence_markers",
    ):
        owner_value = getattr(owner, name)
        extracted_value = getattr(extracted, name)
        assert owner_value is extracted_value
        assert owner_value.__module__ == "agent.context_compressor_message_helpers"


def test_original_module_reexports_marker_constant(extracted):
    assert owner._DB_PERSISTED_MARKER == extracted._DB_PERSISTED_MARKER
    assert owner._DB_PERSISTED_MARKER == "_db_persisted"


def test_fresh_copy_is_distinct_and_preserves_nested_identity(extracted):
    nested = {"tool_calls": [{"id": "call-1"}]}
    source = {
        "role": "assistant",
        "content": "done",
        "metadata": nested,
        "_db_persisted": True,
    }

    copied = extracted._fresh_compaction_message_copy(source)

    assert copied is not source
    assert copied["metadata"] is nested
    assert "_db_persisted" not in copied
    assert source["_db_persisted"] is True


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (None, None),
        ("not a message", None),
        ({"role": "tool", "content": "result"}, None),
        ({"role": "assistant", "tool_calls": [{"id": "c"}]}, None),
        ({"role": "assistant", "tool_calls": []}, "assistant"),
        ({"role": "user"}, "user"),
        ({"role": "system"}, "system"),
    ],
)
def test_template_visible_role_contract(extracted, message, expected):
    assert extracted._template_visible_role(message) == expected


def test_strip_markers_mutates_dicts_only_and_preserves_nested_data(extracted):
    nested = {"keep": [1, 2, 3]}
    first = {"role": "user", "metadata": nested, "_db_persisted": True}
    second = {"role": "assistant", "content": "ok"}
    messages = [first, "foreign-row", second]

    result = extracted._strip_persistence_markers(messages)

    assert result is None
    assert "_db_persisted" not in first
    assert first["metadata"] is nested
    assert messages[1] == "foreign-row"
    assert second == {"role": "assistant", "content": "ok"}


def test_original_module_patch_authority_survives_extraction(monkeypatch):
    sentinel = {"role": "user", "content": "patched"}
    patched = lambda _message: sentinel  # noqa: E731

    monkeypatch.setattr(owner, "_fresh_compaction_message_copy", patched)

    assert owner._fresh_compaction_message_copy({"role": "user"}) is sentinel


def test_import_orders_are_cycle_free():
    snippets = (
        "import agent.context_compressor_message_helpers as h; "
        "import agent.context_compressor as o",
        "import agent.context_compressor as o; "
        "import agent.context_compressor_message_helpers as h",
    )
    check = (
        "; import json; print(json.dumps({"
        "'fresh': o._fresh_compaction_message_copy is h._fresh_compaction_message_copy, "
        "'role': o._template_visible_role is h._template_visible_role, "
        "'strip': o._strip_persistence_markers is h._strip_persistence_markers, "
        "'marker': o._DB_PERSISTED_MARKER == h._DB_PERSISTED_MARKER}))"
    )

    for snippet in snippets:
        completed = subprocess.run(
            [sys.executable, "-c", snippet + check],
            check=True,
            capture_output=True,
            text=True,
        )
        assert json.loads(completed.stdout) == {
            "fresh": True,
            "role": True,
            "strip": True,
            "marker": True,
        }


def test_compress_resolves_fresh_copy_through_original_patch_surface():
    with patch(
        "agent.context_compressor.get_model_context_length", return_value=100000
    ):
        compressor = owner.ContextCompressor(
            model="test/model",
            threshold_percent=0.85,
            protect_first_n=2,
            protect_last_n=2,
            quiet_mode=True,
        )
        _ = compressor.context_length

    messages = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"m{index}",
            "_db_persisted": True,
        }
        for index in range(10)
    ]
    leaking_copy = Mock(side_effect=lambda message: message.copy())

    with (
        patch.object(owner, "_fresh_compaction_message_copy", leaking_copy),
        patch(
            "agent.context_compressor.call_llm", side_effect=RuntimeError("no provider")
        ),
    ):
        result = compressor.compress(messages)

    assert leaking_copy.call_count > 0
    assert all("_db_persisted" not in message for message in result)
