#!/usr/bin/env python3
"""Reproduce Atlas Opus streaming and Agent-tool compatibility failures.

The probe has three stages:

1. ``direct_stream`` sends a minimal streaming request without tools.
2. ``minimal_tools`` repeats it with Ultra Studio's dotted tool names.
3. ``run_orchestrator`` (opt-in via ``--full``) exercises the real
   Run Orchestrator -> Hermes path with the configured tool definitions.

Every Atlas request is single-attempt. The full-path probe cancels a run after
its first logged API failure or the configured observation limit, so this
diagnostic cannot silently spend retries on another model.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from dotenv import dotenv_values


DEFAULT_MODELS = (
    "anthropic/claude-opus-4.6",
    "anthropic/claude-opus-4.7",
    "anthropic/claude-opus-4.8",
)
DEFAULT_TOOL_NAMES = (
    "ask_user_question",
    "platform.prompt_compile",
    "media.model_catalog",
    "media.estimate_cost",
    "media.generate_image",
    "media.generate_video",
)
TERMINAL_RUN_STATES = {"completed", "failed", "canceled", "cancelled", "timeout"}


@dataclass
class ProbeResult:
    stage: str
    model: str
    ok: bool
    elapsed_seconds: float
    http_status: int = 0
    first_event_seconds: float | None = None
    finish_reason: str = ""
    content: str = ""
    error: str = ""
    run_id: str = ""
    run_status: str = ""


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def build_minimal_tools(tool_names: Iterable[str]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": "Compatibility probe tool. Do not call it.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        }
        for name in tool_names
    ]


def parse_sse_lines(lines: Iterable[str], *, started_at: float) -> tuple[float | None, str, str]:
    first_event_at: float | None = None
    content: list[str] = []
    finish_reason = ""
    for line in lines:
        if not line.startswith("data:"):
            continue
        if first_event_at is None:
            first_event_at = time.monotonic() - started_at
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            body = json.loads(data)
        except (TypeError, json.JSONDecodeError):
            continue
        choices = body.get("choices") if isinstance(body, dict) else None
        choice = choices[0] if isinstance(choices, list) and choices else {}
        delta = choice.get("delta") if isinstance(choice, dict) else {}
        if isinstance(delta, dict) and delta.get("content"):
            content.append(str(delta["content"]))
        if isinstance(choice, dict) and choice.get("finish_reason"):
            finish_reason = str(choice["finish_reason"])
    return first_event_at, "".join(content), finish_reason


def load_atlas_api_key(env_file: Path) -> str:
    api_key = str(os.environ.get("ATLAS_API_KEY") or "").strip()
    if not api_key and env_file.exists():
        api_key = str(dotenv_values(env_file).get("ATLAS_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("ATLAS_API_KEY is not set and was not found in the Hermes env file")
    return api_key


def probe_atlas_stream(
    *,
    api_key: str,
    base_url: str,
    model: str,
    timeout_seconds: float,
    tools: list[dict[str, Any]] | None,
) -> ProbeResult:
    stage = "minimal_tools" if tools else "direct_stream"
    started = time.monotonic()
    try:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": "Reply exactly: MODEL_TEST_OK. Do not call tools."}],
            "max_tokens": 32,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
        with httpx.Client(timeout=httpx.Timeout(timeout_seconds, connect=10.0)) as client:
            with client.stream(
                "POST",
                base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + api_key},
                json=payload,
            ) as response:
                if response.status_code >= 300:
                    preview = response.read().decode("utf-8", "replace")[:500]
                    return ProbeResult(
                        stage=stage,
                        model=model,
                        ok=False,
                        elapsed_seconds=round(time.monotonic() - started, 2),
                        http_status=response.status_code,
                        error=preview,
                    )
                first_event, content, finish_reason = parse_sse_lines(
                    response.iter_lines(),
                    started_at=started,
                )
                return ProbeResult(
                    stage=stage,
                    model=model,
                    ok=content.strip() == "MODEL_TEST_OK",
                    elapsed_seconds=round(time.monotonic() - started, 2),
                    http_status=response.status_code,
                    first_event_seconds=round(first_event, 2) if first_event is not None else None,
                    finish_reason=finish_reason,
                    content=content[:160],
                    error="" if content else "stream completed without assistant content",
                )
    except Exception as exc:
        return ProbeResult(
            stage=stage,
            model=model,
            ok=False,
            elapsed_seconds=round(time.monotonic() - started, 2),
            error=f"{type(exc).__name__}: {str(exc)[:300]}",
        )


class RunOrSigner:
    def __init__(self, signing_key: bytes) -> None:
        if len(signing_key) not in (32, 64):
            raise ValueError("Run Orchestrator signing key must be a 32-byte seed or 64-byte private key")
        self._private_key = Ed25519PrivateKey.from_private_bytes(signing_key[:32])

    @classmethod
    def from_environment_or_keychain(cls) -> "RunOrSigner":
        encoded = str(os.environ.get("PANEL_BFF_RUNOR_SIGNING_KEY") or "").strip()
        if not encoded and sys.platform == "darwin":
            result = subprocess.run(
                [
                    "/usr/bin/security",
                    "find-generic-password",
                    "-s",
                    "com.ultrastudio.panel-bff.runor-signing",
                    "-a",
                    "local",
                    "-w",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            encoded = result.stdout.strip()
        if not encoded:
            raise RuntimeError("PANEL_BFF_RUNOR_SIGNING_KEY is unavailable")
        padding = "=" * (-len(encoded) % 4)
        try:
            raw = base64.b64decode(encoded + padding, validate=True)
        except (binascii.Error, ValueError):
            raw = base64.urlsafe_b64decode(encoded + padding)
        return cls(raw)

    def issue(self, scopes: Iterable[str]) -> str:
        now = int(time.time())
        payload = {
            "iss": "panel-bff",
            "aud": "agent-orchestrator",
            "azp": "panel-bff",
            "sub": "opus-compat-probe",
            "iat": now,
            "nbf": now - 5,
            "exp": now + 120,
            "scopes": sorted(set(scopes)),
            "principal": {
                "user_id": "opus-compat-probe",
                "tenant_id": "local-tenant",
                "workspace_id": "local-workspace",
                "project_id": "opus-compat-probe",
            },
        }
        header = _encode(b'{"alg":"EdDSA","typ":"JWT"}')
        unsigned = header + "." + _encode(json.dumps(payload, separators=(",", ":")).encode())
        return unsigned + "." + _encode(self._private_key.sign(unsigned.encode()))


class HermesLogWatcher:
    def __init__(self, path: Path) -> None:
        self._handle = path.open("r", encoding="utf-8", errors="replace") if path.exists() else None
        if self._handle:
            self._handle.seek(0, 2)

    def close(self) -> None:
        if self._handle:
            self._handle.close()

    def first_api_failure(self, run_id: str) -> str:
        if not self._handle:
            return ""
        new_lines = self._handle.read()
        for line in new_lines.splitlines():
            if run_id not in line or "API call failed (attempt 1/3)" not in line:
                continue
            error_type = re.search(r"error_type=([^ ]+)", line)
            model = re.search(r"model=([^ ]+)", line)
            return "first_api_failure:" + (error_type.group(1) if error_type else "unknown") + (
                ":model=" + model.group(1) if model else ""
            )
        return ""


def _auth_headers(signer: RunOrSigner, scopes: Iterable[str]) -> dict[str, str]:
    return {"Authorization": "Bearer " + signer.issue(scopes), "Content-Type": "application/json"}


def probe_run_orchestrator(
    *,
    runor_url: str,
    runtime: str,
    model: str,
    tool_names: Iterable[str],
    timeout_seconds: float,
    signer: RunOrSigner,
    log_path: Path,
) -> ProbeResult:
    started = time.monotonic()
    run_id = ""
    watcher = HermesLogWatcher(log_path)
    try:
        with httpx.Client(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            thread_response = client.post(
                runor_url.rstrip("/") + "/v1/threads",
                headers=_auth_headers(signer, ["run:create"]),
                json={},
            )
            thread_response.raise_for_status()
            thread_id = str(thread_response.json().get("thread_id") or "")
            run_response = client.post(
                runor_url.rstrip("/") + f"/v1/threads/{thread_id}/runs",
                headers=_auth_headers(signer, ["run:create"]),
                json={
                    "runtime": runtime,
                    "model": model,
                    "input": {
                        "messages": [{"role": "user", "content": "Reply exactly: MODEL_TEST_OK"}],
                        "tool_names": list(tool_names),
                    },
                },
            )
            run_response.raise_for_status()
            run_id = str(run_response.json().get("run_id") or "")
            if not run_id:
                raise RuntimeError("Run Orchestrator response did not include run_id")

            cancel_reason = ""
            state: dict[str, Any] = {}
            while time.monotonic() - started <= timeout_seconds:
                state_response = client.get(
                    runor_url.rstrip("/") + f"/v1/runs/{run_id}",
                    headers=_auth_headers(signer, ["run:read"]),
                )
                state_response.raise_for_status()
                state = state_response.json()
                run_status = str(state.get("status") or "")
                if run_status in TERMINAL_RUN_STATES:
                    break
                cancel_reason = watcher.first_api_failure(run_id)
                if cancel_reason:
                    break
                time.sleep(1)
            else:
                cancel_reason = f"probe_limit_{timeout_seconds:g}s"

            run_status = str(state.get("status") or "")
            if cancel_reason and run_status not in TERMINAL_RUN_STATES:
                client.post(
                    runor_url.rstrip("/") + f"/v1/runs/{run_id}/cancel",
                    headers=_auth_headers(signer, ["run:interrupt"]),
                    json={},
                ).raise_for_status()
                run_status = "canceled"

            return ProbeResult(
                stage="run_orchestrator",
                model=model,
                ok=run_status == "completed",
                elapsed_seconds=round(time.monotonic() - started, 2),
                http_status=run_response.status_code,
                content=str(state.get("output") or "")[:160],
                error=cancel_reason or str(state.get("error") or "")[:300],
                run_id=run_id,
                run_status=run_status,
            )
    except Exception as exc:
        return ProbeResult(
            stage="run_orchestrator",
            model=model,
            ok=False,
            elapsed_seconds=round(time.monotonic() - started, 2),
            error=f"{type(exc).__name__}: {str(exc)[:300]}",
            run_id=run_id,
        )
    finally:
        watcher.close()


def parse_models(raw: str) -> tuple[str, ...]:
    models = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not models:
        raise argparse.ArgumentTypeError("at least one model is required")
    return models


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-live", action="store_true", help="required safety gate for paid live requests")
    parser.add_argument("--full", action="store_true", help="also run the real Run Orchestrator -> Hermes path")
    parser.add_argument("--models", type=parse_models, default=DEFAULT_MODELS, help="comma-separated model IDs")
    parser.add_argument("--atlas-url", default="https://api.atlascloud.ai/v1")
    parser.add_argument("--runor-url", default="http://127.0.0.1:8093")
    parser.add_argument("--runtime", default="hermes")
    parser.add_argument("--atlas-timeout", type=float, default=135.0)
    parser.add_argument("--full-timeout", type=float, default=135.0)
    parser.add_argument("--env-file", type=Path, default=Path.home() / ".hermes" / ".env")
    parser.add_argument("--hermes-log", type=Path, default=Path.home() / ".hermes" / "logs" / "agent.log")
    parser.add_argument("--output", type=Path, help="optional JSON result path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.confirm_live:
        print("Refusing paid live requests without --confirm-live", file=sys.stderr)
        return 2

    api_key = load_atlas_api_key(args.env_file)
    minimal_tools = build_minimal_tools(DEFAULT_TOOL_NAMES)
    results: list[ProbeResult] = []
    jobs = [
        (model, tools)
        for tools in (None, minimal_tools)
        for model in args.models
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(args.models)) as pool:
        futures = [
            pool.submit(
                probe_atlas_stream,
                api_key=api_key,
                base_url=args.atlas_url,
                model=model,
                timeout_seconds=args.atlas_timeout,
                tools=tools,
            )
            for model, tools in jobs
        ]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            print(json.dumps(asdict(result), ensure_ascii=False), flush=True)

    if args.full:
        signer = RunOrSigner.from_environment_or_keychain()
        for model in args.models:
            result = probe_run_orchestrator(
                runor_url=args.runor_url,
                runtime=args.runtime,
                model=model,
                tool_names=DEFAULT_TOOL_NAMES,
                timeout_seconds=args.full_timeout,
                signer=signer,
                log_path=args.hermes_log,
            )
            results.append(result)
            print(json.dumps(asdict(result), ensure_ascii=False), flush=True)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
