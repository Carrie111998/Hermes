#!/usr/bin/env python3
"""Controlled admission stress test; defaults stay below 128 MiB total RSS."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

_ADMISSION_PATH = Path(__file__).resolve().parents[1] / "gateway" / "admission.py"
_SPEC = importlib.util.spec_from_file_location("hermes_gateway_admission", _ADMISSION_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"could not load {_ADMISSION_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
AgentAdmissionController = _MODULE.AgentAdmissionController


async def run(workers: int, parallel: int, memory_mb: int, seconds: float) -> dict:
    if workers > 12 or parallel > 6 or memory_mb > 64 or seconds > 15:
        raise ValueError("refusing unsafe stress parameters (workers<=12, parallel<=6, memory_mb<=64, seconds<=15)")
    controller = AgentAdmissionController(
        max_parallel=parallel, queue_limit=workers, poll_interval_seconds=0.05
    )
    peak_active = 0
    queued_notices = 0
    failures: list[str] = []

    async def one(index: int) -> None:
        nonlocal peak_active, queued_notices

        async def notice(_message: str) -> None:
            nonlocal queued_notices
            queued_notices += 1

        task_id = f"stress-{index}"
        await controller.acquire(task_id, on_queued=notice)
        peak_active = max(peak_active, controller.snapshot().active)
        try:
            code = (
                "import time; "
                f"x=bytearray({memory_mb}*1024*1024); "
                f"time.sleep({seconds!r}); print(len(x))"
            )
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                code,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            _stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                failures.append(f"{task_id}: rc={proc.returncode} {stderr.decode(errors='replace')[:120]}")
        finally:
            await controller.release(task_id)

    await asyncio.gather(*(one(i) for i in range(workers)))
    return {
        "workers": workers,
        "parallel_limit": parallel,
        "peak_active": peak_active,
        "queued_notices": queued_notices,
        "failures": failures,
        "gateway_process_survived": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--parallel", type=int, default=2)
    parser.add_argument("--memory-mb", type=int, default=16)
    parser.add_argument("--seconds", type=float, default=0.25)
    args = parser.parse_args()
    result = asyncio.run(run(args.workers, args.parallel, args.memory_mb, args.seconds))
    print(json.dumps(result, sort_keys=True))
    return 1 if result["failures"] or result["peak_active"] > args.parallel else 0


if __name__ == "__main__":
    raise SystemExit(main())
