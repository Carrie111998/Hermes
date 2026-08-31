#!/usr/bin/env python3
"""JAY OS v1 parallel bootstrap driver.

Runs ten read-only inventory workers concurrently and stores evidence snapshots.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import pathlib
import platform
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = ROOT / "jay-os-v1" / "evidence"
OUT.mkdir(parents=True, exist_ok=True)


def run(cmd: list[str], timeout: int = 12) -> dict:
    try:
        p = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
        return {"cmd": cmd, "returncode": p.returncode, "stdout": p.stdout[-12000:], "stderr": p.stderr[-4000:]}
    except Exception as exc:  # noqa: BLE001
        return {"cmd": cmd, "error": type(exc).__name__, "message": str(exc)}


def exists(cmd: str) -> str | None:
    return shutil.which(cmd)


def a_network() -> dict:
    return {"tailscale": run(["tailscale", "status"]) if exists("tailscale") else None,
            "ping": {h: run(["ping", "-c", "1", "-W", "1000", h], 3) for h in ["m1", "rtx", "m5"]}}


def b_machine() -> dict:
    return {"hostname": socket.gethostname(), "platform": platform.platform(), "machine": platform.machine(),
            "disk": run(["df", "-h", "/"]), "memory": run(["sysctl", "-n", "hw.memsize"]) if platform.system()=="Darwin" else run(["free", "-m"])}


def c_repo_config() -> dict:
    return {"repo": str(ROOT), "branch": run(["git", "-C", str(ROOT), "branch", "--show-current"]),
            "head": run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"]),
            "status": run(["git", "-C", str(ROOT), "status", "--short"]),
            "hermes_version": run(["hermes", "--version"]) if exists("hermes") else None}


def d_onememory() -> dict:
    env = pathlib.Path.home() / ".hermes" / ".env"
    keys = []
    if env.exists():
        for line in env.read_text(errors="ignore").splitlines():
            if "=" in line and any(s in line.upper() for s in ["ONE", "VX", "MCP", "MASTRA"]):
                keys.append(line.split("=", 1)[0])
    return {"env_keys_present_redacted": sorted(set(keys)), "mcp_test_vx": run(["hermes", "mcp", "test", "vx"], 30) if exists("hermes") else None}


def e_github() -> dict:
    return {"gh_auth": run(["gh", "auth", "status"], 20) if exists("gh") else None,
            "remotes": run(["git", "-C", str(ROOT), "remote", "-v"]),
            "ci_files": [str(p.relative_to(ROOT)) for p in ROOT.glob(".github/workflows/*")]}


def f_standard_agent_framework() -> dict:
    hits=[]
    for p in ROOT.glob("**/package.json"):
        if any(part in {"node_modules", ".git"} for part in p.parts):
            continue
        s=p.read_text(errors="ignore").lower()
        if "mastra" in s:
            hits.append(str(p.relative_to(ROOT)))
    return {"node": run(["node", "--version"]) if exists("node") else None,
            "npm": run(["npm", "--version"]) if exists("npm") else None,
            "pnpm": run(["pnpm", "--version"]) if exists("pnpm") else None,
            "mastra_package_hits": hits}


def g_voice() -> dict:
    return {"ollama": run(["ollama", "list"], 8) if exists("ollama") else None,
            "rtx_ping": run(["ping", "-c", "1", "-W", "1000", "rtx"], 3),
            "voice_policy": "RTX voice preempts batch; do not saturate GPU with batch jobs."}


def h_testing_release() -> dict:
    return {"pyproject": (ROOT/"pyproject.toml").exists(), "package_json": (ROOT/"package.json").exists(),
            "tests_dir": (ROOT/"tests").exists(), "pytest_collect_help": run(["python3", "-m", "pytest", "--version"], 20)}


def i_security() -> dict:
    return {"policy": ["no human login/OTP", "service identities", "short-lived scoped tokens", "least privilege"],
            "secret_file_present": (pathlib.Path.home()/".hermes"/".env").exists(),
            "git_tracked_env": run(["git", "-C", str(ROOT), "ls-files", "*.env", ".env", "**/.env"])}


def j_business_intake() -> dict:
    return {"ledger": "GitHub for engineering; Linear expected for tasks if configured", "linear_env_keys": sorted(k for k in os.environ if "LINEAR" in k),
            "messaging_targets": run(["hermes", "send", "--list"], 20) if exists("hermes") else None}


WORKERS = {
    "A_tailscale_network": a_network,
    "B_machine_resource": b_machine,
    "C_jay_repo_config": c_repo_config,
    "D_onememory_auth_api_capability": d_onememory,
    "E_github_ci_cd_repo": e_github,
    "F_standard_agent_framework": f_standard_agent_framework,
    "G_rtx_qwen3_tts_voice": g_voice,
    "H_testing_release_infra": h_testing_release,
    "I_security_adversarial": i_security,
    "J_business_work_intake": j_business_intake,
}


def main() -> int:
    started = time.time()
    results = {}
    with cf.ThreadPoolExecutor(max_workers=len(WORKERS)) as ex:
        futs = {ex.submit(fn): name for name, fn in WORKERS.items()}
        for fut in cf.as_completed(futs):
            name = futs[fut]
            try:
                data = fut.result()
            except Exception as exc:  # noqa: BLE001
                data = {"error": type(exc).__name__, "message": str(exc)}
            results[name] = data
            (OUT / f"{name}.json").write_text(json.dumps({"worker": name, "timestamp_utc": datetime.now(timezone.utc).isoformat(), "data": data}, indent=2, sort_keys=True))
    summary = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "elapsed_seconds": round(time.time()-started, 2), "workers": sorted(results)}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
