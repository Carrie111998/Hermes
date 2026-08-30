import ast
import os
from pathlib import Path

import pytest

from agent.model_classification import CHEAP_MODELS, FRONTIER_MODELS, model_class


def test_frontier_models_classified_as_frontier():
    for name in FRONTIER_MODELS:
        assert model_class(name) == "frontier"


def test_cheap_models_classified_as_benchmarked_cheap():
    for name in CHEAP_MODELS:
        assert model_class(name) == "benchmarked_cheap"


def test_unknown_model_returns_unknown():
    assert model_class("some-untracked-model") == "unknown"


def test_none_and_empty_return_unknown():
    assert model_class(None) == "unknown"
    assert model_class("") == "unknown"


def test_provider_prefixed_name_is_stripped():
    assert model_class("anthropic/claude-opus-5") == "frontier"
    assert model_class("opencode-go/glm-5.2") == "benchmarked_cheap"


def test_frontier_and_cheap_sets_are_disjoint():
    assert not (FRONTIER_MODELS & CHEAP_MODELS)


# --- drift guard against the offline evaluator's copy of the same sets --------
#
# The evaluator is an offline sandbox tool that must not import from this tree,
# so the sets are duplicated by hand. This test parses (never imports, never
# executes) the evaluator and fails if the two copies have diverged. It skips
# when the sandbox is not reachable, which is the normal case on any machine
# other than the one that produced it; override the path with
# HERMES_PER2_EVALUATOR_PATH.

_DEFAULT_EVALUATOR_PATH = (
    "E:/IGN/_sandbox/per2-mod-01-routing-policy-20260822/evaluator/evaluate.py"
)


def _parse_model_sets(path: Path) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id in ("FRONTIER_MODELS", "CHEAP_MODELS"):
            found[target.id] = {elt.value for elt in node.value.elts}
    return found


def test_membership_matches_offline_evaluator():
    path = Path(os.environ.get("HERMES_PER2_EVALUATOR_PATH", _DEFAULT_EVALUATOR_PATH))
    if not path.is_file():
        pytest.skip(f"PER2-MOD-01 evaluator not reachable at {path}")
    evaluator = _parse_model_sets(path)
    assert evaluator.get("FRONTIER_MODELS") == FRONTIER_MODELS
    assert evaluator.get("CHEAP_MODELS") == CHEAP_MODELS
