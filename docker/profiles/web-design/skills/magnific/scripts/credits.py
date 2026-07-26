#!/usr/bin/env python3
"""Audit de consommation de credits Magnific (Analytics API, aucun credit consomme).

Usage:
    python3 credits.py                 # conso agregee par jour et par outil
    python3 credits.py --by-model      # detail par modele quand l'API le fournit
    python3 credits.py --keys          # liste les cles API de l'equipe

Endpoints (REST only, absents du MCP) :
    POST /v1/analytics/team-credit-usage
    GET  /v1/analytics/team-api-keys

Requiert MAGNIFIC_API_KEY.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict

BASE = os.environ.get("MAGNIFIC_API_BASE", "https://api.magnific.com/v1")


def api_key() -> str:
    key = os.environ.get("MAGNIFIC_API_KEY")
    if not key:
        sys.exit("MAGNIFIC_API_KEY absente de l'environnement.")
    return key


def request(method: str, path: str, payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=body,
        method=method,
        headers={"x-magnific-api-key": api_key(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        sys.exit(f"HTTP {exc.code} sur {method} {path}\n{exc.read().decode(errors='replace')[:400]}")


def show_keys() -> None:
    for row in request("GET", "/analytics/team-api-keys").get("data", []):
        print(f"{row.get('display_name','?'):<24} {row.get('status','?'):<10} "
              f"cree {row.get('created_at','?')}  id={row.get('api_key_id','?')}")


def show_usage(by_model: bool) -> None:
    days = request("POST", "/analytics/team-credit-usage", {}).get("data", [])
    total = 0
    per_tool: dict[str, int] = defaultdict(int)

    for day in days:
        date = str(day.get("date", ""))[:10]
        for item in day.get("consumptions", []):
            tool = item.get("tool", "?")
            credits = item.get("user_credits", 0) or 0
            uses = item.get("user_uses", 0) or 0
            total += credits
            per_tool[tool] += credits
            print(f"{date}  {tool:<48} uses={uses:<5} credits={credits}")
            if by_model:
                for usage in item.get("user_usages", []):
                    print(f"{'':12}  └─ {usage.get('project_name','?')} / "
                          f"{usage.get('user_email','?')}: "
                          f"{usage.get('user_uses',0)} uses, "
                          f"{usage.get('user_credits',0)} credits")

    print("\n--- par outil ---")
    for tool, credits in sorted(per_tool.items(), key=lambda kv: -kv[1]):
        print(f"{tool:<48} {credits}")
    print(f"\nTOTAL sur la periode retournee : {total} credits")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--by-model", action="store_true")
    parser.add_argument("--keys", action="store_true")
    args = parser.parse_args()
    if args.keys:
        show_keys()
    else:
        show_usage(args.by_model)


if __name__ == "__main__":
    main()
