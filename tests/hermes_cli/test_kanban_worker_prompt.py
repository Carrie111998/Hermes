"""Tests for kanban_worker_prompt module."""

import pytest

from hermes_cli.kanban_worker_prompt import (
    FactoryCardContractError,
    assert_factory_card_contract,
    build_worker_spawn_prompt,
    parse_context_list,
    parse_manuals,
)


def test_parse_manuals_ordered():
    body = """
GOAL: x
MANUALS:
  - read_file: /tmp/AGENTS.md
  - skill_view: kanban-factory
  - skill_view: ocr-science
PROCEDURE: y
"""
    assert parse_manuals(body) == [
        ("read_file", "/tmp/AGENTS.md"),
        ("skill_view", "kanban-factory"),
        ("skill_view", "ocr-science"),
    ]


def test_parse_manuals_empty_without_section():
    assert parse_manuals("GOAL: x\n") == []
    assert parse_manuals(None) == []


def test_spawn_prompt_is_not_four_word_stub():
    prompt = build_worker_spawn_prompt("t_deadbeef", body=None, board="ocr")
    assert prompt.strip() != "work kanban task t_deadbeef"
    assert "t_deadbeef" in prompt
    assert "kanban_request_review" in prompt
    assert "do not kanban_complete" in prompt.lower() or "not kanban_complete" in prompt.lower()


def test_spawn_prompt_lists_manuals_without_pasting_bodies():
    body = """
MANUALS:
  - read_file: /tmp/AGENTS.md
  - skill_view: kanban-factory
"""
    prompt = build_worker_spawn_prompt("t_abc", body=body, board="ocr")
    assert "read_file" in prompt and "/tmp/AGENTS.md" in prompt
    assert "skill_view" in prompt and "kanban-factory" in prompt
    assert "You MUST use this before any creative work" not in prompt  # no skill body paste


def test_spawn_prompt_injects_agents_md_when_profile_home_exists(tmp_path):
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# law\n", encoding="utf-8")
    prompt = build_worker_spawn_prompt(
        "t_abc",
        body="GOAL: x\n",
        board="ocr",
        assignee="ocr",
        profile_home=str(tmp_path),
    )
    assert str(agents) in prompt
    assert "read_file" in prompt


def test_spawn_prompt_orders_board_native_context_before_manuals():
    body = """
GOAL: close the blocker
REFS:
  - /tmp/reference.txt
CORPUS:
  - /tmp/corpus.md
SESSIONS:
  - @session:default/20260827_204357_dfa73e
MANUALS:
  - read_file: /tmp/AGENTS.md
  - skill_view: kanban-factory
PROCEDURE: inspect the artifacts
DONE: test -f /tmp/done.txt
FAIL: handoff #3 and kanban_block
"""
    prompt = build_worker_spawn_prompt("t_ctx", body=body, board="ocr", assignee="ocr")
    assert "1. Read the task body / goal contract" in prompt
    assert "2. Check task attachments" in prompt
    assert "3. Check parent artifacts / upstream outputs" in prompt
    assert "4. Read curated corpus / handoff paths" in prompt
    assert "/tmp/corpus.md" in prompt
    assert "5. Check linked session references for audit/recovery" in prompt
    assert "@session:default/20260827_204357_dfa73e" in prompt
    assert "6. Load manuals / skills in order" in prompt


def test_parse_context_list_extracts_corpus_and_sessions():
    body = """
CORPUS:
  - /tmp/one.md
  - /tmp/two.md
SESSIONS:
  - @session:default/abc
  - @session:default/def
"""
    assert parse_context_list(body, "CORPUS") == ["/tmp/one.md", "/tmp/two.md"]
    assert parse_context_list(body, "SESSIONS") == ["@session:default/abc", "@session:default/def"]


# Task 3 tests (contract checker)
VALID = """
GOAL: write vectors.parquet
REFS:
  - /tmp/exists.txt
MANUALS:
  - skill_view: kanban-factory
PROCEDURE: 1. run bin/foo
DONE: test -f /tmp/vectors.parquet
FAIL: handoff #3 and kanban_block
"""


def test_contract_accepts_five_fields_plus_manuals():
    assert_factory_card_contract(VALID)  # no raise


def test_contract_rejects_wish():
    with pytest.raises(FactoryCardContractError) as ei:
        assert_factory_card_contract("please fix ocr")
    msg = str(ei.value)
    for field in ("GOAL", "REFS", "MANUALS", "PROCEDURE", "DONE", "FAIL"):
        assert field in msg
