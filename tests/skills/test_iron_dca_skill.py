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
    / "iron-dca"
    / "scripts"
    / "iron_dca.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("moonpay_iron_dca_skill", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_create_mandate_records_hard_spending_bounds():
    mod = load_module()
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)

    mandate = mod.create_mandate(
        deposit_amount="500",
        days=7,
        target_token="So111",
        wallet="main",
        chain="solana",
        expires_at=(now + timedelta(days=14)).isoformat(),
        authorization_id="confirmed-1",
        now=now,
    )

    assert mandate["authorization_id"] == "confirmed-1"
    assert mandate["max_total_amount"] == "500"
    assert mandate["max_runs"] == 7
    assert Decimal(mandate["max_amount_per_run"]) <= Decimal("500") / 7
    assert mandate["executed_runs"] == 0


def test_validate_next_run_rejects_expired_or_exhausted_mandate():
    mod = load_module()
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    mandate = mod.create_mandate(
        deposit_amount="10",
        days=2,
        target_token="So111",
        wallet="main",
        chain="solana",
        expires_at=(now + timedelta(hours=1)).isoformat(),
        authorization_id="confirmed-2",
        now=now,
    )

    with pytest.raises(ValueError, match="expired"):
        mod.validate_next_run(mandate, now=now + timedelta(hours=2))

    mandate["expires_at"] = (now + timedelta(days=1)).isoformat()
    mandate["executed_runs"] = 2
    with pytest.raises(ValueError, match="exhausted"):
        mod.validate_next_run(mandate, now=now)


def test_dry_run_does_not_mutate_state_or_call_mp(tmp_path):
    mod = load_module()
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    state = tmp_path / "iron.json"
    mandate = mod.create_mandate(
        deposit_amount="20",
        days=4,
        target_token="So111",
        wallet="main",
        chain="solana",
        expires_at=(now + timedelta(days=1)).isoformat(),
        authorization_id="confirmed-3",
        now=now,
    )
    state.write_text(json.dumps(mandate), encoding="utf-8")

    result = mod.execute_next_run(state, mp_path="missing-mp", dry_run=True, now=now)

    assert result["status"] == "dry-run"
    assert result["amount"] == "5.000000"
    assert json.loads(state.read_text(encoding="utf-8"))["executed_runs"] == 0


def test_successful_run_updates_authorization_state(tmp_path):
    mod = load_module()
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    state = tmp_path / "iron.json"
    mandate = mod.create_mandate(
        deposit_amount="20",
        days=4,
        target_token="So111",
        wallet="main",
        chain="solana",
        expires_at=(now + timedelta(days=1)).isoformat(),
        authorization_id="confirmed-5",
        now=now,
    )
    state.write_text(json.dumps(mandate), encoding="utf-8")

    with patch.object(
        mod,
        "_run_mp",
        side_effect=[
            {
                "items": [
                    {
                        "address": mod.USDC_SOLANA,
                        "balance": {"amount": "100"},
                    }
                ]
            },
            {"transactionHash": "tx-123"},
        ],
    ):
        result = mod.execute_next_run(state, mp_path="mp", now=now)

    updated = json.loads(state.read_text(encoding="utf-8"))
    assert result["status"] == "executed"
    assert result["transaction_hash"] == "tx-123"
    assert updated["executed_runs"] == 1
    assert updated["total_deployed"] == "5.000000"


def test_main_requires_exact_bounded_spend_acknowledgement(tmp_path, capsys):
    mod = load_module()
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()

    result = mod.main([
        "create",
        "--deposit-amount",
        "10",
        "--days",
        "2",
        "--target-token",
        "So111",
        "--expires-at",
        future,
        "--authorization-id",
        "confirmed-4",
        "--confirm-bounded-spend",
        "yes",
        "--state",
        str(tmp_path / "state.json"),
    ])

    assert result == 2
    assert "acknowledgement" in capsys.readouterr().err
