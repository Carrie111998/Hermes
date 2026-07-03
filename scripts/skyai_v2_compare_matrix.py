#!/usr/bin/env python3
"""Run a DEV-only SkyAI v2 vs current PROD SkyAI comparison matrix.

The script calls the v2 canary gateway's ``/qa/compare`` endpoint. It is a
read-only QA helper: no customer, order, voucher, payment, Git, Render, Shopify,
or Discord mutations.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "https://skyai-v2-dev-ingress-lo4jl44wdq-ey.a.run.app"
DEFAULT_SCENARIOS_PATH = (
    Path(__file__).resolve().parents[1]
    / "plugins"
    / "skyai_customer"
    / "fixtures"
    / "compare_scenarios.json"
)

CompareCaller = Callable[[str, dict[str, Any], float, str], dict[str, Any]]


def load_scenarios(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("compare_scenarios_must_be_list")
    scenarios: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("compare_scenario_must_be_object")
        scenario_id = str(item.get("id") or "").strip()
        message = str(item.get("message") or "").strip()
        if not scenario_id or not message:
            raise ValueError("compare_scenario_requires_id_and_message")
        scenarios.append(
            {
                "id": scenario_id,
                "message": message,
                "focus": str(item.get("focus") or "").strip(),
                "history": item.get("history") if isinstance(item.get("history"), list) else [],
            }
        )
    return scenarios


def build_compare_payload(scenario: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    payload = {
        "conversation_id": f"skyai-v2-compare-{run_id}-{scenario['id']}"[:128],
        "message": scenario["message"],
        "surface": "skyai_v2_compare_matrix",
    }
    if scenario.get("history"):
        payload["history"] = scenario["history"]
    return payload


def call_compare(base_url: str, payload: dict[str, Any], timeout: float, bearer_token: str = "") -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "SkyAI-v2-Compare-Matrix/0.1"}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    request = Request(
        f"{base_url.rstrip('/')}/qa/compare",
        data=body,
        method="POST",
        headers=headers,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return {
            "status": "error",
            "error": "http_error",
            "http_status": exc.code,
            "reason": exc.read().decode("utf-8", errors="replace")[:1000],
        }
    except URLError as exc:
        return {"status": "error", "error": "url_error", "reason": str(exc)[:500]}


def summarize_compare_response(scenario: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    dev = response.get("dev_v2") if isinstance(response.get("dev_v2"), dict) else {}
    prod = response.get("prod_current") if isinstance(response.get("prod_current"), dict) else {}
    cards = response.get("cards_compare") if isinstance(response.get("cards_compare"), dict) else {}
    return {
        "id": scenario["id"],
        "focus": scenario.get("focus", ""),
        "status": response.get("status"),
        "dev_status": dev.get("status"),
        "prod_status": prod.get("status"),
        "dev_cards": dev.get("cards_count", 0),
        "prod_cards": prod.get("cards_count", 0),
        "shared_urls": cards.get("shared_urls", []),
        "only_dev_urls": cards.get("only_dev_urls", []),
        "only_prod_urls": cards.get("only_prod_urls", []),
        "dev_missing_price_count": cards.get("dev_missing_price_count"),
        "prod_missing_price_count": cards.get("prod_missing_price_count"),
        "dev_missing_image_count": cards.get("dev_missing_image_count"),
        "prod_missing_image_count": cards.get("prod_missing_image_count"),
        "dev_reply_preview": _preview(dev.get("reply")),
        "prod_reply_preview": _preview(prod.get("reply")),
    }


def run_matrix(
    scenarios: list[dict[str, Any]],
    *,
    base_url: str,
    timeout: float,
    bearer_token: str = "",
    caller: CompareCaller = call_compare,
    run_id: str | None = None,
    progress: bool = False,
) -> dict[str, Any]:
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    results = []
    for index, scenario in enumerate(scenarios, start=1):
        if progress:
            print(
                f"[{index}/{len(scenarios)}] {scenario['id']}...",
                file=sys.stderr,
                flush=True,
            )
        payload = build_compare_payload(scenario, run_id=run_id)
        response = caller(base_url, payload, timeout, bearer_token)
        if progress:
            summary = summarize_compare_response(scenario, response)
            print(
                f"[{index}/{len(scenarios)}] {scenario['id']} done: "
                f"status={summary['status']} dev_cards={summary['dev_cards']} "
                f"prod_cards={summary['prod_cards']}",
                file=sys.stderr,
                flush=True,
            )
        results.append(
            {
                "scenario": scenario,
                "payload": payload,
                "summary": summarize_compare_response(scenario, response),
                "response": response,
            }
        )
    return {
        "status": "ok",
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "scenario_count": len(scenarios),
        "results": results,
    }


def render_console_summary(report: dict[str, Any]) -> str:
    lines = [
        f"SkyAI v2 compare matrix: {report['scenario_count']} scenarios",
        f"base_url={report['base_url']}",
        "",
    ]
    for item in report["results"]:
        summary = item["summary"]
        lines.append(
            f"- {summary['id']}: status={summary['status']} "
            f"dev_cards={summary['dev_cards']} prod_cards={summary['prod_cards']} "
            f"shared_urls={len(summary['shared_urls'])}"
        )
        if summary["focus"]:
            lines.append(f"  focus: {summary['focus']}")
        if summary["dev_reply_preview"]:
            lines.append(f"  dev:  {summary['dev_reply_preview']}")
        if summary["prod_reply_preview"]:
            lines.append(f"  prod: {summary['prod_reply_preview']}")
    return "\n".join(lines)


def _preview(value: Any, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    return text[: limit - 1].rstrip() + "…" if len(text) > limit else text


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.getenv("SKYAI_V2_COMPARE_BASE_URL", DEFAULT_BASE_URL),
        help="SkyAI v2 canary base URL. Defaults to the DEV ingress.",
    )
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS_PATH)
    parser.add_argument("--out", type=Path, default=Path("skyai-v2-compare-matrix.json"))
    parser.add_argument("--limit", type=int, default=0, help="Optional max number of scenarios.")
    parser.add_argument("--timeout", type=float, default=150.0)
    parser.add_argument("--token-env", default="SKYAI_V2_CANARY_TOKEN")
    parser.add_argument("--quiet", action="store_true", help="Suppress per-scenario progress on stderr.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    scenarios = load_scenarios(args.scenarios)
    if args.limit > 0:
        scenarios = scenarios[: args.limit]
    token = os.getenv(args.token_env, "").strip()
    report = run_matrix(
        scenarios,
        base_url=args.base_url,
        timeout=args.timeout,
        bearer_token=token,
        progress=not args.quiet,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(render_console_summary(report))
    print(f"\nWrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
