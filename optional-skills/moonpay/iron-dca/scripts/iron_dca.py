#!/usr/bin/env python3
"""Create and execute a bounded Iron fiat-to-DCA mandate."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Any


USDC_SOLANA = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
AUTHORIZATION_ACK = "I AUTHORIZE THIS BOUNDED SCHEDULE"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def _positive_decimal(value: str, field: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a decimal number") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{field} must be greater than zero")
    return parsed


def default_state_path() -> Path:
    root = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return root / "moonpay" / "iron-dca-state.json"


def create_mandate(
    *,
    deposit_amount: str,
    days: int,
    target_token: str,
    wallet: str,
    chain: str,
    expires_at: str,
    authorization_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or _utc_now()
    amount = _positive_decimal(deposit_amount, "deposit amount")
    if days <= 0:
        raise ValueError("days must be greater than zero")
    if not target_token.strip():
        raise ValueError("target token is required")
    if chain != "solana":
        raise ValueError("Iron DCA currently supports Solana only")
    if not authorization_id.strip():
        raise ValueError("authorization id is required")
    expiry = _parse_time(expires_at)
    if expiry <= current:
        raise ValueError("expiry must be in the future")

    chunk = (amount / Decimal(days)).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
    if chunk <= 0:
        raise ValueError("deposit is too small for the requested number of days")

    return {
        "schema_version": 1,
        "strategy": "iron-dca",
        "authorization_id": authorization_id,
        "authorized_at": current.isoformat(),
        "expires_at": expiry.isoformat(),
        "wallet": wallet,
        "chain": chain,
        "from_token": USDC_SOLANA,
        "target_token": target_token,
        "max_total_amount": str(amount),
        "max_amount_per_run": str(chunk),
        "max_runs": days,
        "executed_runs": 0,
        "total_deployed": "0",
        "last_transaction_hash": None,
    }


def validate_next_run(mandate: dict[str, Any], now: datetime | None = None) -> Decimal:
    current = now or _utc_now()
    if mandate.get("strategy") != "iron-dca":
        raise ValueError("state is not an Iron DCA mandate")
    if not mandate.get("authorization_id") or not mandate.get("authorized_at"):
        raise ValueError("state has no recorded user authorization")
    if _parse_time(str(mandate["expires_at"])) <= current:
        raise ValueError("authorization has expired")

    executed = int(mandate["executed_runs"])
    max_runs = int(mandate["max_runs"])
    if executed >= max_runs:
        raise ValueError("authorized run count is exhausted")

    per_run = _positive_decimal(str(mandate["max_amount_per_run"]), "per-run amount")
    total = Decimal(str(mandate["total_deployed"]))
    maximum = _positive_decimal(str(mandate["max_total_amount"]), "total cap")
    if total + per_run > maximum:
        raise ValueError("next run would exceed the authorized total cap")
    return per_run


def _save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("state file must contain a JSON object")
    return payload


@contextmanager
def _state_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _run_mp(mp_path: str, args: list[str]) -> dict[str, Any]:
    result = subprocess.run(
        [mp_path, "-f", "compact", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise ValueError("MoonPay CLI returned an unexpected response")
    return payload


def _find_usdc_balance(payload: dict[str, Any]) -> Decimal:
    for item in payload.get("items", []):
        if item.get("address") == USDC_SOLANA:
            balance = item.get("balance", {}).get("amount", "0")
            return Decimal(str(balance))
    return Decimal("0")


def execute_next_run(
    state_path: Path,
    *,
    mp_path: str,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    with _state_lock(state_path):
        return _execute_next_run_unlocked(
            state_path,
            mp_path=mp_path,
            dry_run=dry_run,
            now=now,
        )


def _execute_next_run_unlocked(
    state_path: Path,
    *,
    mp_path: str,
    dry_run: bool,
    now: datetime | None,
) -> dict[str, Any]:
    mandate = _load_json(state_path)
    amount = validate_next_run(mandate, now=now)

    preview = {
        "authorization_id": mandate["authorization_id"],
        "wallet": mandate["wallet"],
        "chain": mandate["chain"],
        "from_token": mandate["from_token"],
        "to_token": mandate["target_token"],
        "amount": str(amount),
        "run": int(mandate["executed_runs"]) + 1,
        "max_runs": int(mandate["max_runs"]),
    }
    if dry_run:
        return {"status": "dry-run", **preview}

    balances = _run_mp(
        mp_path,
        [
            "token",
            "balance",
            "list",
            "--wallet",
            str(mandate["wallet"]),
            "--chain",
            str(mandate["chain"]),
            "--json",
        ],
    )
    if _find_usdc_balance(balances) < amount:
        raise ValueError("insufficient USDC for the authorized run")

    result = _run_mp(
        mp_path,
        [
            "token",
            "swap",
            "--wallet",
            str(mandate["wallet"]),
            "--chain",
            str(mandate["chain"]),
            "--from-token",
            str(mandate["from_token"]),
            "--from-amount",
            str(amount),
            "--to-token",
            str(mandate["target_token"]),
            "--json",
        ],
    )
    transaction_hash = result.get("transactionHash") or result.get("hash")
    mandate["executed_runs"] = int(mandate["executed_runs"]) + 1
    mandate["total_deployed"] = str(Decimal(str(mandate["total_deployed"])) + amount)
    mandate["last_transaction_hash"] = transaction_hash
    mandate["last_executed_at"] = (now or _utc_now()).isoformat()
    _save_json(state_path, mandate)
    return {"status": "executed", "transaction_hash": transaction_hash, **preview}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser(
        "create", help="record a bounded user-authorized mandate"
    )
    create.add_argument("--deposit-amount", required=True)
    create.add_argument("--days", required=True, type=int)
    create.add_argument("--target-token", required=True)
    create.add_argument("--wallet", default="main")
    create.add_argument("--chain", default="solana")
    create.add_argument("--expires-at", required=True)
    create.add_argument("--authorization-id", required=True)
    create.add_argument("--confirm-bounded-spend", required=True)
    create.add_argument("--state", type=Path, default=default_state_path())

    run = subparsers.add_parser("run", help="execute the next authorized chunk")
    run.add_argument("--state", type=Path, default=default_state_path())
    run.add_argument("--mp-path")
    run.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "create":
            if args.confirm_bounded_spend != AUTHORIZATION_ACK:
                raise ValueError("bounded-spend acknowledgement does not match")
            mandate = create_mandate(
                deposit_amount=args.deposit_amount,
                days=args.days,
                target_token=args.target_token,
                wallet=args.wallet,
                chain=args.chain,
                expires_at=args.expires_at,
                authorization_id=args.authorization_id,
            )
            _save_json(args.state, mandate)
            print(
                json.dumps(
                    {"status": "created", "state": str(args.state), **mandate}, indent=2
                )
            )
            return 0

        mp_path = args.mp_path or shutil.which("mp")
        if not mp_path and not args.dry_run:
            raise ValueError("mp executable not found")
        result = execute_next_run(
            args.state,
            mp_path=mp_path or "mp",
            dry_run=args.dry_run,
        )
        print(json.dumps(result, indent=2))
        return 0
    except (
        ValueError,
        OSError,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
