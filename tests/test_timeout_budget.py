"""Contract for the shared subprocess-deadline scaler."""

from __future__ import annotations

import pytest

from tests.timeout_budget import SCALE_ENV_VAR, scaled, timeout_scale


def test_scale_defaults_to_one_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SCALE_ENV_VAR, raising=False)

    assert timeout_scale() == 1.0
    assert scaled(60) == 60


def test_scale_multiplies_incidental_deadlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SCALE_ENV_VAR, "4")

    assert timeout_scale() == 4.0
    assert scaled(60) == 240


@pytest.mark.parametrize("raw", ("", "abc", "1,5", "nan", "inf"))
def test_unparseable_scale_falls_back_to_one(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """A typo must not silently disarm — or explode — every safety net."""
    monkeypatch.setenv(SCALE_ENV_VAR, raw)

    assert timeout_scale() == 1.0


@pytest.mark.parametrize("raw", ("0", "0.1", "-3"))
def test_scale_below_one_is_clamped(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """Shortening a safety net is never the intent of this knob."""
    monkeypatch.setenv(SCALE_ENV_VAR, raw)

    assert timeout_scale() == 1.0
    assert scaled(60) == 60


def test_scale_is_read_per_call_not_cached_at_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SCALE_ENV_VAR, "2")
    assert scaled(10) == 20
    monkeypatch.setenv(SCALE_ENV_VAR, "3")
    assert scaled(10) == 30
