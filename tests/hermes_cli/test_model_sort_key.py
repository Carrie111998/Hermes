"""Behavior tests for model-family version sorting."""

import pytest

from hermes_cli.model_switch import _model_sort_key


@pytest.mark.parametrize("version_prefix", ["k", "K"])
def test_letter_prefixed_versions_sort_numerically(version_prefix):
    prefix = "moonshotai/kimi"
    older = f"{prefix}-{version_prefix}2.6"
    newer = f"{prefix}-{version_prefix}3"

    assert sorted(
        [older, newer],
        key=lambda model: _model_sort_key(model, prefix),
    ) == [newer, older]


def test_v_prefixed_version_sorting_is_preserved():
    prefix = "mimo"

    assert sorted(
        ["mimo-v2.5", "mimo-v3"],
        key=lambda model: _model_sort_key(model, prefix),
    ) == ["mimo-v3", "mimo-v2.5"]
