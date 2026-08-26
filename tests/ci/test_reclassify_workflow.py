"""Tests for the workflow risk reclassification detector."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "reclassify_workflow.py"
_spec = importlib.util.spec_from_file_location("reclassify_workflow", _PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load reclassify_workflow.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
classify = _mod.classify


def test_tiny_auth_diff_adds_security_gates_without_replacing_route():
    result = classify(
        ["auth/session.py"],
        added=2,
        deleted=1,
        initial_route="direct",
    )

    assert result["sensitive"] is True
    assert result["route"] == "direct"
    assert result["small_by_size"] is True
    assert result["parallel_allowed"] is False
    assert result["required_readers"] == ["general_review", "downstream_security"]
    assert result["schedule"] == "readers_then_execution"
    assert "sensitive_path:auth/session.py" in result["reasons"]


def test_dependency_change_adds_gates_even_when_only_one_line():
    result = classify(
        ["package-lock.json"],
        added=1,
        deleted=0,
        initial_route="standard",
    )

    assert result["sensitive"] is True
    assert result["route"] == "standard"
    assert result["small_by_size"] is True
    assert result["parallel_allowed"] is False
    assert "dependency_or_lockfile:package-lock.json" in result["reasons"]


def test_clean_small_diff_allows_parallel_exception():
    result = classify(
        ["docs/typo.md", "agent/formatting.py"],
        added=8,
        deleted=3,
        initial_route="direct",
    )

    assert result["sensitive"] is False
    assert result["route"] == "direct"
    assert result["small_by_size"] is True
    assert result["parallel_allowed"] is True
    assert result["required_readers"] == ["general_review"]
    assert result["schedule"] == "parallel_review_launch_qa"


def test_clean_large_diff_waits_for_review():
    result = classify(
        [f"agent/module_{i}.py" for i in range(6)],
        added=120,
        deleted=90,
        initial_route="standard",
    )

    assert result["sensitive"] is False
    assert result["route"] == "standard"
    assert result["small_by_size"] is False
    assert result["parallel_allowed"] is False
    assert result["schedule"] == "readers_then_execution"


def test_permission_keyword_in_patch_promotes_clean_path():
    result = classify(
        ["agent/policy.py"],
        added=3,
        deleted=0,
        patch_text="+ required_permission = 'admin'\n",
    )

    assert result["sensitive"] is True
    assert "sensitive_content:permission" in result["reasons"]


def test_empty_diff_fails_closed_without_replacing_route():
    result = classify([], added=0, deleted=0, initial_route="direct")

    assert result["sensitive"] is True
    assert result["route"] == "direct"
    assert result["small_by_size"] is True
    assert result["parallel_allowed"] is False
    assert "empty_diff" in result["reasons"]


def test_guard_code_and_tests_are_self_protecting_sensitive_paths():
    result = classify(
        [
            "scripts/ci/reclassify_workflow.py",
            "tests/ci/test_reclassify_workflow.py",
        ],
        added=1,
        deleted=0,
        initial_route="direct",
    )

    assert result["sensitive"] is True
    assert result["parallel_allowed"] is False
    assert result["required_readers"] == ["general_review", "downstream_security"]
    assert "sensitive_path:scripts/ci/reclassify_workflow.py" in result["reasons"]
    assert "sensitive_path:tests/ci/test_reclassify_workflow.py" in result["reasons"]


def test_initial_route_is_required_and_closed_enum():
    for route in ("direct", "standard", "complete"):
        assert classify(["agent/x.py"], added=1, deleted=0, initial_route=route)["route"] == route
