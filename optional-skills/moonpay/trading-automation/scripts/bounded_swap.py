#!/usr/bin/env python3
"""Create and execute bounded MoonPay swap automations."""

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
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


AUTHORIZATION_ACK = "I AUTHORIZE THIS BOUNDED SCHEDULE"
STRATEGIES = ("dca", "limit-buy", "stop-loss")


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
    return root / "moonpay" / "bounded-swap.json"


def create_mandate(
    *,
    strategy: str,
    wallet: str,
    chain: str,
    from_token: str,
    to_token: str,
    amount_per_run: str,
    max_total_amount: str,
    max_runs: int,
    expires_at: str,
    min_interval_seconds: int,
    authorization_id: str,
    trigger_price: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or _utc_now()
    if strategy not in STRATEGIES:
        raise ValueError(f"strategy must be one of: {', '.join(STRATEGIES)}")
    if not all(
        value.strip()
        for value in (wallet, chain, from_token, to_token, authorization_id)
    ):
        raise ValueError("wallet, chain, tokens, and authorization id are required")
    per_run = _positive_decimal(amount_per_run, "per-run amount")
    total_cap = _positive_decimal(max_total_amount, "total cap")
    if max_runs <= 0:
        raise ValueError("max runs must be greater than zero")
    if per_run * max_runs > total_cap:
        raise ValueError("per-run amount multiplied by max runs exceeds total cap")
    if min_interval_seconds < 60:
        raise ValueError("minimum interval must be at least 60 seconds")
    expiry = _parse_time(expires_at)
    if expiry <= current:
        raise ValueError("expiry must be in the future")

    parsed_trigger: str | None = None
    if strategy != "dca":
        if trigger_price is None:
            raise ValueError("price-triggered strategies require a trigger price")
        parsed_trigger = str(_positive_decimal(trigger_price, "trigger price"))

    return {
        "schema_version": 1,
        "strategy": strategy,
        "authorization_id": authorization_id,
        "authorized_at": current.isoformat(),
        "expires_at": expiry.isoformat(),
        "wallet": wallet,
        "chain": chain,
        "from_token": from_token,
        "to_token": to_token,
        "amount_per_run": str(per_run),
        "max_total_amount": str(total_cap),
        "max_runs": max_runs,
        "min_interval_seconds": min_interval_seconds,
        "trigger_price": parsed_trigger,
        "executed_runs": 0,
        "total_spent": "0",
        "last_executed_at": None,
        "last_transaction_hash": None,
    }


def is_triggered(strategy: str, current_price: Decimal, trigger_price: Decimal) -> bool:
    if strategy == "limit-buy":
        return current_price <= trigger_price
    if strategy == "stop-loss":
        return current_price <= trigger_price
    raise ValueError("DCA has no price trigger")


def validate_next_run(mandate: dict[str, Any], now: datetime | None = None) -> Decimal:
    current = now or _utc_now()
    if mandate.get("strategy") not in STRATEGIES:
        raise ValueError("state has an unsupported strategy")
    if not mandate.get("authorization_id") or not mandate.get("authorized_at"):
        raise ValueError("state has no recorded user authorization")
    if _parse_time(str(mandate["expires_at"])) <= current:
        raise ValueError("authorization has expired")
    if int(mandate["executed_runs"]) >= int(mandate["max_runs"]):
        raise ValueError("authorized run count is exhausted")

    per_run = _positive_decimal(str(mandate["amount_per_run"]), "per-run amount")
    spent = Decimal(str(mandate["total_spent"]))
    total_cap = _positive_decimal(str(mandate["max_total_amount"]), "total cap")
    if spent + per_run > total_cap:
        raise ValueError("next run would exceed the authorized total cap")

    last_executed = mandate.get("last_executed_at")
    if last_executed:
        elapsed = (current - _parse_time(str(last_executed))).total_seconds()
        if elapsed < int(mandate["min_interval_seconds"]):
            raise ValueError("minimum authorized interval has not elapsed")
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


def _price_for_strategy(mandate: dict[str, Any], mp_path: str) -> Decimal:
    token = (
        mandate["to_token"]
        if mandate["strategy"] == "limit-buy"
        else mandate["from_token"]
    )
    payload = _run_mp(
        mp_path,
        ["token", "retrieve", "--token", str(token), "--chain", str(mandate["chain"])],
    )
    value = payload.get("marketData", {}).get("price")
    return _positive_decimal(str(value), "current price")


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
    current = now or _utc_now()
    mandate = _load_json(state_path)
    amount = validate_next_run(mandate, now=current)
    preview = {
        "authorization_id": mandate["authorization_id"],
        "strategy": mandate["strategy"],
        "wallet": mandate["wallet"],
        "chain": mandate["chain"],
        "from_token": mandate["from_token"],
        "to_token": mandate["to_token"],
        "amount": str(amount),
        "run": int(mandate["executed_runs"]) + 1,
        "max_runs": int(mandate["max_runs"]),
    }
    if dry_run:
        return {"status": "dry-run", **preview}

    if mandate["strategy"] != "dca":
        current_price = _price_for_strategy(mandate, mp_path)
        trigger_price = Decimal(str(mandate["trigger_price"]))
        if not is_triggered(str(mandate["strategy"]), current_price, trigger_price):
            return {
                "status": "waiting",
                "current_price": str(current_price),
                "trigger_price": str(trigger_price),
                **preview,
            }

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
            str(mandate["to_token"]),
        ],
    )
    transaction_hash = result.get("transactionHash") or result.get("hash")
    mandate["executed_runs"] = int(mandate["executed_runs"]) + 1
    mandate["total_spent"] = str(Decimal(str(mandate["total_spent"])) + amount)
    mandate["last_executed_at"] = current.isoformat()
    mandate["last_transaction_hash"] = transaction_hash
    _save_json(state_path, mandate)
    return {"status": "executed", "transaction_hash": transaction_hash, **preview}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser(
        "create", help="record a bounded user-authorized mandate"
    )
    create.add_argument("--strategy", choices=STRATEGIES, required=True)
    create.add_argument("--wallet", default="main")
    create.add_argument("--chain", required=True)
    create.add_argument("--from-token", required=True)
    create.add_argument("--to-token", required=True)
    create.add_argument("--amount-per-run", required=True)
    create.add_argument("--max-total-amount", required=True)
    create.add_argument("--max-runs", required=True, type=int)
    create.add_argument("--expires-at", required=True)
    create.add_argument("--min-interval-seconds", type=int, default=3600)
    create.add_argument("--authorization-id", required=True)
    create.add_argument("--trigger-price")
    create.add_argument("--confirm-bounded-spend", required=True)
    create.add_argument("--state", type=Path, default=default_state_path())

    run = subparsers.add_parser(
        "run", help="evaluate and execute the next authorized swap"
    )
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
                strategy=args.strategy,
                wallet=args.wallet,
                chain=args.chain,
                from_token=args.from_token,
                to_token=args.to_token,
                amount_per_run=args.amount_per_run,
                max_total_amount=args.max_total_amount,
                max_runs=args.max_runs,
                expires_at=args.expires_at,
                min_interval_seconds=args.min_interval_seconds,
                authorization_id=args.authorization_id,
                trigger_price=args.trigger_price,
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
