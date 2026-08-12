"""Focused seam tests for CL-R4-1 response content normalization."""

from __future__ import annotations

import ast
import inspect
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
PIN = "ee4bb75b532e932a1055d9a710802a7435163b6a"
GOLDEN_SHA = "af3d33d2a9e213f9f41b1ebec05785359a5455e523ae38578f0b4c0cb3ef6b68"


def test_normalize_assistant_content_cases_and_identity():
    from agent.response_normalization import normalize_assistant_content

    cases = [
        ("plain", "plain"),
        (None, None),
        ({"text": "preferred", "content": "ignored"}, "preferred"),
        ({"content": "fallback"}, "fallback"),
        ({"other": 1}, json.dumps({"other": 1})),
        (["a", {"type": "text", "text": "b"}, {"type": "image", "text": 3}, {"text": 4}, {"type": "image"}], "a\nb\n3\n4"),
        ([{"type": "image"}, 3, None], ""),
        (42, "42"),
    ]
    for raw, expected in cases:
        message = SimpleNamespace(content=raw)
        original = message
        assert normalize_assistant_content(message) is None
        assert message is original
        assert message.content == expected


def test_normalize_assistant_content_preserves_exceptions_and_mutation_timing(monkeypatch):
    from agent import response_normalization

    message = SimpleNamespace(content={"other": object()})
    with pytest.raises(TypeError):
        response_normalization.normalize_assistant_content(message)
    assert isinstance(message.content, dict)

    message = SimpleNamespace(content=[{"type": "text", "text": None}])
    with pytest.raises(TypeError):
        response_normalization.normalize_assistant_content(message)
    assert message.content == [{"type": "text", "text": None}]

    message = SimpleNamespace(content="already string")
    response_normalization.normalize_assistant_content(message)
    assert message.content.strip() == "already string"


def test_call_site_is_inside_try_after_normalize_before_lifecycle_hook():
    import agent.conversation_loop as conversation_loop

    source = inspect.getsource(conversation_loop.run_conversation)
    assert "normalize_assistant_content(assistant_message)" in source
    assert source.index("normalize_response") < source.index("normalize_assistant_content(assistant_message)")
    assert source.index("normalize_assistant_content(assistant_message)") < source.index('has_hook("post_api_request")')

    tree = ast.parse(inspect.getsource(conversation_loop.run_conversation))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "normalize_assistant_content"
    ]
    assert len(calls) == 1
    try_nodes = [node for node in ast.walk(tree) if isinstance(node, ast.Try)]
    assert any(call in ast.walk(try_node) for try_node in try_nodes for call in calls)


def test_run_conversation_identity_and_monkeypatch_surfaces_remain_original():
    import agent.conversation_loop as conversation_loop
    from agent import model_metadata

    assert conversation_loop.run_conversation.__module__ == "agent.conversation_loop"
    for name in ("save_context_length", "estimate_request_tokens_rough", "estimate_messages_tokens_rough", "conversation_history_after_compression"):
        assert hasattr(conversation_loop, name)
    assert conversation_loop.save_context_length is model_metadata.save_context_length
    assert not hasattr(conversation_loop, "normalize_assistant_content")


def test_new_module_has_only_cheap_json_import_and_no_import_side_effects():
    source = Path(ROOT, "agent", "response_normalization.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert [(type(node).__name__, ast.unparse(node)) for node in imports] == [("Import", "import json")]
    assert [node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))] == ["normalize_assistant_content"]

    probe = "import agent.response_normalization as m; print(m.normalize_assistant_content.__module__)"
    result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "agent.response_normalization"
    assert result.stderr == ""


def test_pinned_window_golden_receipt_is_stable():
    result = subprocess.run(
        ["git", "show", f"{PIN}:agent/conversation_loop.py"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    import hashlib

    window = b"\n".join(result.stdout.splitlines()[6081:6101]) + b"\n"
    assert hashlib.sha256(window).hexdigest() == GOLDEN_SHA
    assert len(window) == 1236
