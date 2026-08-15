"""Strong cgroup-v2 scope identity and accounting."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from hermes_cli.kanban_store.types import ContractError


@dataclass(frozen=True, slots=True)
class CgroupV2Sample:
    cgroup_path: str
    boot_id: str
    pids: tuple[int, ...]
    cpu_usec: int
    io_bytes: int
    frozen: bool
    freeze_supported: bool


def _kv_file(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            values[parts[0]] = int(parts[1])
    return values


def sample_cgroup_v2(path: str | Path) -> CgroupV2Sample:
    root = Path(path).resolve(strict=True)
    if not (root / "cgroup.procs").exists():
        raise ContractError("not a cgroup-v2 scope")
    pids = tuple(sorted(int(value) for value in (root / "cgroup.procs").read_text().split()))
    cpu = _kv_file(root / "cpu.stat").get("usage_usec", 0)
    io_total = 0
    io_path = root / "io.stat"
    if io_path.exists():
        for line in io_path.read_text().splitlines():
            for item in line.split()[1:]:
                key, _, value = item.partition("=")
                if key in {"rbytes", "wbytes"} and value.isdigit():
                    io_total += int(value)
    freeze_path = root / "cgroup.freeze"
    freeze_supported = freeze_path.exists() and os.access(freeze_path, os.W_OK)
    frozen = freeze_path.exists() and freeze_path.read_text().strip() == "1"
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    return CgroupV2Sample(
        cgroup_path=str(root),
        boot_id=boot_id,
        pids=pids,
        cpu_usec=cpu,
        io_bytes=io_total,
        frozen=frozen,
        freeze_supported=freeze_supported,
    )


def freeze_cgroup(path: str | Path) -> str:
    root = Path(path).resolve(strict=True)
    target = root / "cgroup.freeze"
    if not target.exists() or not os.access(target, os.W_OK):
        raise ContractError("cgroup freeze is unavailable")
    target.write_text("1\n")
    if target.read_text().strip() != "1":
        raise ContractError("cgroup did not enter frozen state")
    sample = sample_cgroup_v2(root)
    return f"{sample.boot_id}:{sample.cgroup_path}"


def thaw_cgroup(path: str | Path, token: str) -> None:
    sample = sample_cgroup_v2(path)
    if token != f"{sample.boot_id}:{sample.cgroup_path}":
        raise ContractError("cgroup identity drifted before thaw")
    (Path(path) / "cgroup.freeze").write_text("0\n")
