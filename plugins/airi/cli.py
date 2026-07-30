"""CLI for Project AIRI ↔ Hermes Agent process-worker bridge."""
from __future__ import annotations

import argparse
import json

from . import core


def register_cli(subparser: argparse.ArgumentParser) -> None:
    subs = subparser.add_subparsers(dest="airi_command")
    subs.add_parser(
        "status",
        help="Show AIRI worker health + AI-provider/TTS sync readiness (safe beside Desktop)",
    )
    sync = subs.add_parser(
        "sync",
        help="Wire Hermes AI core + TTS into AIRI (credentials merge + consciousness/speech CDP seed)",
    )
    sync.add_argument("--base-url", default="", help="Hermes OpenAI base URL (default :8642/v1/)")
    sync.add_argument("--model", default="", help="Model id exposed to AIRI (default hermes-agent)")
    start = subs.add_parser(
        "start",
        help=(
            "Start AIRI Electron as a Hermes process worker (concurrent with Hermes Desktop). "
            "Syncs provider/TTS, launches tamagotchi with isolated userData + CDP :9455, seeds credentials."
        ),
    )
    start.add_argument("--base-url", default="")
    start.add_argument("--model", default="")
    start.add_argument("--cdp-port", type=int, default=0, help="CDP port (default 9455; avoids Desktop :9333)")
    start.add_argument("--repo-root", default="")
    restart = subs.add_parser(
        "restart",
        help="Restart AIRI worker only (does not stop Hermes Desktop) and re-seed provider/TTS",
    )
    restart.add_argument("--base-url", default="")
    restart.add_argument("--model", default="")
    restart.add_argument("--cdp-port", type=int, default=0)
    restart.add_argument("--repo-root", default="")
    subs.add_parser("stop", help="Stop the Hermes-managed AIRI worker only (never kills Desktop)")
    cfg = subs.add_parser("configure", help="Write provider template only (subset of sync)")
    cfg.add_argument("--base-url", default="")
    cfg.add_argument("--model", default="")


def _print_text(text: str) -> int:
    print(text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return 0
    return 0 if payload.get("ok", True) else 1


def _values_from_args(args: argparse.Namespace) -> dict:
    values: dict = {}
    base = getattr(args, "base_url", "") or ""
    model = getattr(args, "model", "") or ""
    repo = getattr(args, "repo_root", "") or ""
    cdp = getattr(args, "cdp_port", 0) or 0
    if base:
        values["hermes_base_url"] = base
    if model:
        values["hermes_model"] = model
    if repo:
        values["repo_root"] = repo
    if cdp:
        values["cdp_port"] = cdp
    return values


def airi_command(args: argparse.Namespace) -> int:
    command = getattr(args, "airi_command", None) or "status"
    values = _values_from_args(args)
    if command == "status":
        return _print_text(core.status(values))
    if command == "sync":
        return _print_text(core.sync(values))
    if command == "configure":
        return _print_text(core.configure_hermes(values))
    if command == "start":
        return _print_text(core.start(values))
    if command == "restart":
        return _print_text(core.restart(values))
    if command == "stop":
        return _print_text(core.stop(values))
    return _print_text(core.status(values))
