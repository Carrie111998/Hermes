from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "moonpay"
    / "trading-automation"
    / "scripts"
    / "bounded_swap.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location(
        "moonpay_bounded_swap_skill", SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_mandate(mod, now):
    return mod.create_mandate(
        strategy="dca",
        wallet="main",
        chain="solana",
        from_token="USDC",
        to_token="SOL",
        amount_per_run="5",
        max_total_amount="20",
        max_runs=4,
        expires_at=(now + timedelta(days=5)).isoformat(),
        min_interval_seconds=3600,
        authorization_id="confirmed-dca",
        now=now,
    )


def test_create_mandate_rejects_a_run_plan_above_total_cap():
    mod = load_module()
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="exceeds total cap"):
        mod.create_mandate(
            strategy="dca",
            wallet="main",
            chain="solana",
            from_token="USDC",
            to_token="SOL",
            amount_per_run="10",
            max_total_amount="20",
            max_runs=3,
            expires_at=(now + timedelta(days=5)).isoformat(),
            min_interval_seconds=3600,
            authorization_id="confirmed-dca",
            now=now,
        )


def test_price_triggers_are_conservative_at_the_boundary():
    mod = load_module()

    assert mod.is_triggered("limit-buy", Decimal("80"), Decimal("80")) is True
    assert mod.is_triggered("limit-buy", Decimal("81"), Decimal("80")) is False
    assert mod.is_triggered("stop-loss", Decimal("69"), Decimal("70")) is True
    assert mod.is_triggered("stop-loss", Decimal("71"), Decimal("70")) is False


def test_validate_next_run_enforces_minimum_interval():
    mod = load_module()
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    mandate = make_mandate(mod, now)
    mandate["last_executed_at"] = now.isoformat()

    with pytest.raises(ValueError, match="interval"):
        mod.validate_next_run(mandate, now=now + timedelta(minutes=30))

    assert mod.validate_next_run(mandate, now=now + timedelta(hours=1)) == Decimal("5")


def test_dry_run_does_not_mutate_state_or_call_mp(tmp_path):
    mod = load_module()
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    state = tmp_path / "bounded.json"
    state.write_text(json.dumps(make_mandate(mod, now)), encoding="utf-8")

    result = mod.execute_next_run(state, mp_path="missing-mp", dry_run=True, now=now)

    assert result["status"] == "dry-run"
    assert result["amount"] == "5"
    assert json.loads(state.read_text(encoding="utf-8"))["executed_runs"] == 0


def test_successful_dca_run_updates_caps_and_receipt(tmp_path):
    mod = load_module()
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    state = tmp_path / "bounded.json"
    state.write_text(json.dumps(make_mandate(mod, now)), encoding="utf-8")

    with patch.object(mod, "_run_mp", return_value={"transactionHash": "tx-456"}):
        result = mod.execute_next_run(state, mp_path="mp", now=now)

    updated = json.loads(state.read_text(encoding="utf-8"))
    assert result["status"] == "executed"
    assert result["transaction_hash"] == "tx-456"
    assert updated["executed_runs"] == 1
    assert updated["total_spent"] == "5"
    assert updated["last_executed_at"] == now.isoformat()


def test_price_strategy_requires_a_trigger():
    mod = load_module()
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="trigger price"):
        mod.create_mandate(
            strategy="limit-buy",
            wallet="main",
            chain="solana",
            from_token="USDC",
            to_token="SOL",
            amount_per_run="5",
            max_total_amount="5",
            max_runs=1,
            expires_at=(now + timedelta(days=1)).isoformat(),
            min_interval_seconds=300,
            authorization_id="confirmed-limit",
            now=now,
        )
