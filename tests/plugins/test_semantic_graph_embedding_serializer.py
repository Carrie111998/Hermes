from __future__ import annotations

import hashlib

import pytest

from plugins.semantic_graph.embedding.serializer import (
    QUERY_INSTRUCTION,
    serialize_embedding_node,
    serialize_embedding_query,
    source_text_hash,
)


def test_query_adds_qwen_instruction_prefix() -> None:
    assert serialize_embedding_query("What language do I prefer?") == (
        f"Instruct: {QUERY_INSTRUCTION}\nQuery:What language do I prefer?"
    )


def test_query_truncates_overlong_input() -> None:
    result = serialize_embedding_query("x " * 2500)
    assert result.startswith(f"Instruct: {QUERY_INSTRUCTION}\nQuery:")
    assert len(result.split("\nQuery:", 1)[1]) <= 4000


def test_query_removes_nul_and_control_characters() -> None:
    result = serialize_embedding_query("  alpha\x00\n\tbeta\x7f  ")
    assert result.endswith("Query:alpha beta")


def test_query_rejects_empty_after_cleaning() -> None:
    with pytest.raises(ValueError, match="query must not be empty"):
        serialize_embedding_query("\x00 \t")


def test_node_matches_canonical_example_exactly() -> None:
    node = {
        "node_type": "Preference",
        "subtype": "development.frontend.language",
        "label": "Frontend language",
        "summary": "The user prefers TypeScript for frontend development.",
        "identity_key": "preference.frontend.language",
    }
    assert serialize_embedding_node(node) == "\n".join(
        [
            "Type: Preference",
            "Subtype: development.frontend.language",
            "Label: Frontend language",
            "Summary: The user prefers TypeScript for frontend development.",
            "Identity: preference.frontend.language",
        ]
    )


def test_node_ignores_extra_trust_and_secret_fields() -> None:
    node = {
        "node_type": "Preference",
        "label": "Frontend language",
        "summary": "TypeScript",
        "status": "accepted",
        "authority": "user",
        "confidence": 1.0,
        "run_id": "run-secret-123",
        "api_key": "sk-no-leak-123",
        "metadata": {"password": "do-not-embed"},
    }
    result = serialize_embedding_node(node)
    assert "accepted" not in result
    assert "run-secret-123" not in result
    assert "sk-no-leak-123" not in result
    assert "do-not-embed" not in result
    assert result.count("\n") == 4


def test_node_keeps_five_lines_when_optional_fields_are_missing() -> None:
    result = serialize_embedding_node(
        {"node_type": "Fact", "label": "A fact"}
    )
    assert result == "\n".join(
        [
            "Type: Fact",
            "Subtype: ",
            "Label: A fact",
            "Summary: ",
            "Identity: ",
        ]
    )


@pytest.mark.parametrize(
    "node",
    [
        {"label": "missing type"},
        {"node_type": "Fact"},
        {"node_type": "", "label": "label"},
        {"node_type": "Fact", "label": ""},
        {"node_type": "Fact", "label": None},
    ],
)
def test_node_requires_non_empty_type_and_label(node: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        serialize_embedding_node(node)


def test_node_normalizes_multiline_and_control_text() -> None:
    result = serialize_embedding_node(
        {
            "node_type": " Fact ",
            "subtype": "a\r\nb\x00\t",
            "label": " Label ",
            "summary": "line one\nline two\x7f",
            "identity_key": " id ",
        }
    )
    assert result == "\n".join(
        [
            "Type: Fact",
            "Subtype: a b",
            "Label: Label",
            "Summary: line one line two",
            "Identity: id",
        ]
    )
    assert result.count("\n") == 4


def test_node_caps_each_field() -> None:
    result = serialize_embedding_node(
        {
            "node_type": "Fact",
            "label": "L " * 1500,
            "summary": "S " * 2500,
        }
    )
    lines = result.splitlines()
    assert len(lines[2].removeprefix("Label: ")) <= 2000
    assert len(lines[3].removeprefix("Summary: ")) <= 4000
    assert lines[2].removeprefix("Label: ").startswith("L L")
    assert lines[3].removeprefix("Summary: ").startswith("S S")


def test_source_hash_is_sha256_hex() -> None:
    value = source_text_hash("hello")
    assert value == hashlib.sha256(b"hello").hexdigest()
    assert len(value) == 64
    assert all(char in "0123456789abcdef" for char in value)


def test_source_hash_of_empty_is_known_constant() -> None:
    assert (
        source_text_hash("")
        == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_source_hash_covers_serialized_canonical_text() -> None:
    first = {"node_type": "Fact", "label": "A", "summary": "B"}
    second = {
        "node_type": "Fact",
        "label": "A",
        "summary": "B",
        "status": "accepted",
        "confidence": 0.99,
    }
    assert source_text_hash(serialize_embedding_node(first)) == source_text_hash(
        serialize_embedding_node(second)
    )
    assert source_text_hash(serialize_embedding_node(first)) != source_text_hash(
        serialize_embedding_node({**first, "summary": "C"})
    )
