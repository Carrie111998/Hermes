"""Fail-closed selection contract for the minimal core+dev lane."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path(__file__).with_name("minimal_lane_manifest.json")
_ALLOWED_LANES = {
    "optional_acp", "optional_anthropic", "optional_wecom", "optional_hindsight",
    "optional_fal", "optional_daytona", "optional_modal", "optional_parallel_web",
    "optional_wake_macos", "optional_computer_use_linux", "linux_systemd_s6",
    "gnu_cli_host", "wsl_and_audio_host", "nondeterministic_timing",
}
_NODE_RE = re.compile(r"^tests/[^:]+(?:::.*)+$")


def _baseline_test_paths() -> set[str]:
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "f3d5080c68f034d9bb42f93c39d393633580b5da", "--", "tests"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return {line for line in result.stdout.splitlines() if re.match(r"tests/test_.*\.py$|tests/.+/test_.*\.py$", line)}


def _collected_node_ids(path: str) -> set[str]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", path],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode not in {0, 5}:
        raise ValueError(f"cannot collect selectors for {path}: {result.stderr.strip() or result.stdout.strip()}")
    prefix = f"{path}::"
    return {
        line.strip().split(" ", 1)[0]
        for line in result.stdout.splitlines()
        if line.strip().startswith(prefix)
    }


def load_manifest() -> dict:
    try:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load selection manifest: {exc}") from exc
    if data.get("manifest_version") != "2.0":
        raise ValueError("unsupported test selection manifest version")
    if data.get("lane") != "minimal-core-dev":
        raise ValueError("manifest lane is not minimal-core-dev")
    if data.get("base_head") != "f3d5080c68f034d9bb42f93c39d393633580b5da":
        raise ValueError("manifest base HEAD does not match the frozen contract")
    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("manifest entries must be a non-empty list")
    paths = []
    selectors = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("manifest entry must be an object")
        path = entry.get("path")
        if not isinstance(path, str) or not path.startswith("tests/") or Path(path).is_absolute():
            raise ValueError(f"malformed manifest path: {path!r}")
        if path in paths:
            raise ValueError(f"duplicate manifest ownership: {path}")
        paths.append(path)
        if entry.get("lane") not in _ALLOWED_LANES:
            raise ValueError(f"unknown manifest lane: {entry.get('lane')!r}")
        if not isinstance(entry.get("rationale"), str) or not entry["rationale"].strip():
            raise ValueError(f"missing Phase 1 rationale for {path}")
        if entry.get("direct_rerun") != path or not (ROOT / path).is_file():
            raise ValueError(f"missing/stale direct-rerun selector for {path}")
        sels = entry.get("selectors")
        if not isinstance(sels, list) or not sels:
            raise ValueError(f"missing selectors for {path}")
        level = entry.get("selection_level")
        if level not in {"file", "node"}:
            raise ValueError(f"malformed selection level for {path}")
        for selector in sels:
            if not isinstance(selector, str) or not selector.startswith(path):
                raise ValueError(f"malformed selector for {path}: {selector!r}")
            if selector in selectors:
                raise ValueError(f"duplicate selector ownership: {selector}")
            selectors.append(selector)
            if level == "file" and selector != path:
                raise ValueError(f"file selection must use its direct file selector: {selector}")
            if level == "node" and not _NODE_RE.match(selector):
                raise ValueError(f"malformed node selector: {selector}")
            if level == "node" and selector not in _collected_node_ids(path):
                raise ValueError(f"manifest selector does not exist: {selector}")
    data["_entries_by_path"] = {entry["path"]: entry for entry in entries}
    return data


def excluded_paths() -> set[str]:
    return set(load_manifest()["_entries_by_path"])


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(f"selection audit cannot own outside-root path: {path}") from exc


def audit_file_ownership(files: list[Path]) -> dict:
    manifest = load_manifest()
    known = manifest["_entries_by_path"]
    core_paths = set(manifest.get("core_paths", []))
    baseline = _baseline_test_paths()
    rows = []
    for path in sorted(files):
        relative = _relative(path)
        entry = known.get(relative)
        if entry:
            owner = entry["lane"]
        elif relative in core_paths or relative in baseline:
            owner = "core+dev"
        else:
            raise ValueError(f"unowned discovered test path: {relative}")
        rows.append({"path": relative, "owner": owner})
    declared = set(known)
    discovered = {row["path"] for row in rows}
    missing = sorted(declared - discovered)
    if missing:
        # Missing declared entries are expected only when the caller selected a
        # narrower in-repository root; the default full tests/ audit is strict.
        if len(files) > 100:
            raise ValueError("declared exclusion entries not covered: " + ", ".join(missing))
    return {"manifest_version": manifest["manifest_version"], "files": rows, "unowned": []}


def select_minimal(files: list[Path]) -> list[Path]:
    audit_file_ownership(files)
    manifest = load_manifest()["_entries_by_path"]
    # File exclusions remove only that file. Node exclusions retain the file so
    # untagged/core nodes continue to run; the runner applies --deselect below.
    return [path for path in sorted(files) if not (
        (entry := manifest.get(_relative(path))) and entry["selection_level"] == "file"
    )]


def deselect_args(path: Path) -> list[str]:
    entry = load_manifest()["_entries_by_path"].get(_relative(path))
    if not entry or entry["selection_level"] == "file":
        return []
    return [arg for selector in entry["selectors"] for arg in ("--deselect", selector)]


if __name__ == "__main__":
    import sys
    roots = [ROOT / arg for arg in sys.argv[1:]] or [ROOT / "tests"]
    files = sorted(path for root in roots for path in (root.rglob("test_*.py") if root.is_dir() else [root]))
    print(json.dumps(audit_file_ownership(files), sort_keys=True, indent=2))
