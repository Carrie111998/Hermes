from __future__ import annotations

import ast
from pathlib import Path

SOURCE_PATH = (
    Path(__file__).parents[1]
    / "hermes_cli"
    / "lanes"
    / "impls"
    / "tihna.py"
)


def _tree() -> ast.Module:
    return ast.parse(SOURCE_PATH.read_text(encoding="utf-8"))


def _imported_modules() -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _called_names() -> set[str]:
    names: set[str] = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            names.add(node.func.id)
    return names


def _harness_calls() -> list[str]:
    calls = []
    for node in ast.walk(_tree()):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Name)
            and function.value.id == "harness"
        ):
            calls.append(function.attr)
    return calls


def test_tihna_module_calls_only_harness_methods_not_CS01_ingress_directly():
    assert "hermes_cli.programme.ingress" not in _imported_modules()
    assert "admit_new_turn" not in _called_names()
    assert "admit" not in _called_names()


def test_tihna_module_never_calls_retrying_write_txn_directly():
    assert "hermes_cli.sqlite_util" not in _imported_modules()
    assert "retrying_write_txn" not in _called_names()


def test_tihna_module_never_writes_cost_ledger_directly():
    assert "hermes_cli.cost.ledger" not in _imported_modules()
    assert "record_call" not in _called_names()


def test_tihna_module_never_writes_leaf_verdicts_directly():
    assert "hermes_cli.verdict" not in _imported_modules()
    assert "record_verdict" not in _called_names()


def test_tihna_module_never_calls_route_for_turn_directly():
    assert "hermes_cli.routing.facade" not in _imported_modules()
    assert "route_for_turn" not in _called_names()


def test_tihna_module_never_touches_HERMES_ROUTE_CONTEXT_JSON_directly():
    source = SOURCE_PATH.read_text(encoding="utf-8")
    assert "HERMES_ROUTE_CONTEXT_JSON" not in source
    assert "hermes_cli.routing.route_context" not in _imported_modules()


def test_tihna_module_uses_harness_call_llm_for_all_LLM_paths():
    assert _harness_calls().count("call_llm") == 2
    assert not {
        "call_llm",
        "async_call_llm",
        "completion",
        "responses",
    }.intersection(_called_names())


def test_tihna_module_uses_harness_publish_with_ledger_for_all_side_effects():
    assert _harness_calls().count("publish_with_ledger") == 1
    assert "requests" not in _imported_modules()
    assert "subprocess" not in _imported_modules()
