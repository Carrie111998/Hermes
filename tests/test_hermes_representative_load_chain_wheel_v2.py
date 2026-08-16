"""Installed-wheel external-cwd proof for the complete H4/H5/H6 v2 chain."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import venv
import zipfile

import pytest

from hermes_strict_no_send_preflight_v2 import _implementation_graph_sha256


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    "hermes_worker_containment_canonical_bytes_v2.py",
    "hermes_strict_no_send_preflight_v2.py",
    "hermes_strict_runtime_guard_v2.py",
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


VALID_CANDIDATE = _canonical(
    {
        "attempts": 1,
        "contract_version": "hermes.worker_containment.canonical_bytes.v2",
        "credential_mode": "external_owner_handoff_required",
        "fanout": 0,
        "immutable_revision_claimed": False,
        "jobs": 1,
        "max_cost_usd_microdollars": 250_000,
        "max_input_tokens": 32_768,
        "max_output_bytes": 524_288,
        "max_output_tokens": 8_192,
        "max_total_tokens": 40_960,
        "model_call_limit": 1,
        "model_id": "glm-5.2",
        "provider_id": "zai",
        "provider_internal_revision": "unknown",
        "provider_internal_revision_owner_accepted": True,
        "provider_request_limit": 1,
        "repository_mount": False,
        "retry_count": 0,
        "tool_allowlist": [],
        "wall_clock_seconds": 900,
    }
)


def _environment_document(home: Path) -> bytes:
    return _canonical(
        {
            "contract_version": "hermes.clean_environment.canonical_bytes.v2",
            "environment": {
                "HOME": str(home),
                "LANG": "C.UTF-8",
                "TZ": "UTC",
            },
        }
    )


def _envelope(home: Path, graph_sha256: str) -> bytes:
    return _canonical(
        {
            "candidate_document_b64": base64.b64encode(VALID_CANDIDATE).decode("ascii"),
            "contract_version": "hermes.strict_no_send_preflight.input.v2",
            "environment_document_b64": base64.b64encode(
                _environment_document(home)
            ).decode("ascii"),
            "expected_implementation_graph_sha256": graph_sha256,
        }
    )


def _safe_env(home: Path, path: str) -> dict[str, str]:
    environment = {
        "HOME": str(home),
        "HERMES_HOME": str(home / ".hermes"),
        "LANG": "C.UTF-8",
        "PATH": path,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "TZ": "UTC",
    }
    for name in ("COMSPEC", "PATHEXT", "SYSTEMROOT", "TEMP", "TMP", "WINDIR"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def _extract_archive(archive_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            path = PurePosixPath(member.filename)
            assert not path.is_absolute()
            assert ".." not in path.parts
        archive.extractall(destination)


def _run_installed(
    python: Path,
    outside: Path,
    home: Path,
    code: str,
    stdin: bytes,
    scripts_dir: Path,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [str(python), "-I", "-c", code],
        cwd=outside,
        env=_safe_env(home, str(scripts_dir)),
        input=stdin,
        capture_output=True,
        check=False,
        timeout=60,
    )


@pytest.mark.integration
def test_installed_wheel_runs_v2_chain_from_external_cwd(tmp_path: Path):
    runner_path = os.pathsep.join(
        (str(Path(sys.executable).resolve().parent), os.environ.get("PATH", ""))
    )
    cache_query = subprocess.run(
        ["uv", "cache", "dir"],
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert cache_query.returncode == 0, cache_query.stderr
    uv_cache_dir = cache_query.stdout.decode("utf-8", errors="strict").strip()
    assert Path(uv_cache_dir).is_absolute()
    assert Path(uv_cache_dir).is_dir()
    archive_path = tmp_path / "source.zip"
    archive_home = tmp_path / "archive-home"
    archive_home.mkdir()
    archived = subprocess.run(
        ["git", "archive", "--format=zip", "--output", str(archive_path), "HEAD"],
        cwd=REPO_ROOT,
        env=_safe_env(archive_home, runner_path),
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert archived.returncode == 0, archived.stderr

    source_root = tmp_path / "source"
    source_root.mkdir()
    _extract_archive(archive_path, source_root)
    for relative_path in (*MODULES, "pyproject.toml"):
        target = source_root / relative_path
        target.write_bytes((REPO_ROOT / relative_path).read_bytes())

    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    build_home = tmp_path / "build-home"
    build_home.mkdir()
    build_environment = _safe_env(build_home, runner_path)
    # setup.py intentionally rejects distributable artifacts unless the
    # repository's explicit packaging-build marker is present.  This test
    # builds only an ephemeral, offline proof wheel from the trusted archive
    # overlay; the marker is not forwarded to the installed runtime process.
    build_environment["HERMES_NIX_BUILD"] = "1"
    # The clean build HOME intentionally has no dependency cache.  Reuse only
    # uv's already-populated package cache while keeping --offline; this path
    # is not forwarded to any H4/H5/H6 runtime subprocess.
    build_environment["UV_CACHE_DIR"] = uv_cache_dir
    built = subprocess.run(
        [
            "uv",
            "build",
            "--wheel",
            "--offline",
            "--out-dir",
            str(wheel_dir),
            str(source_root),
        ],
        cwd=tmp_path,
        env=build_environment,
        capture_output=True,
        check=False,
        timeout=300,
    )
    assert built.returncode == 0, built.stderr
    wheels = sorted(wheel_dir.glob("hermes_agent-*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as wheel:
        for module_name in MODULES:
            assert wheel.namelist().count(module_name) == 1
            assert wheel.read(module_name) == (REPO_ROOT / module_name).read_bytes()

    venv_dir = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
    scripts_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    venv_python = scripts_dir / ("python.exe" if os.name == "nt" else "python")
    install_home = tmp_path / "install-home"
    install_home.mkdir()
    installed = subprocess.run(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-index",
            str(wheels[0]),
        ],
        cwd=tmp_path,
        env=_safe_env(install_home, str(scripts_dir)),
        capture_output=True,
        check=False,
        timeout=300,
    )
    assert installed.returncode == 0, installed.stderr

    outside = tmp_path / "outside"
    outside.mkdir()
    run_home = Path(os.path.realpath(tmp_path / "run-home"))
    run_home.mkdir()
    (run_home / ".hermes").mkdir()
    graph_sha256 = _implementation_graph_sha256()[0]

    h4_code = (
        "import json,sys\n"
        "from hermes_worker_containment_canonical_bytes_v2 import "
        "assess_worker_containment_canonical_bytes_v2 as assess\n"
        f"candidate={VALID_CANDIDATE!r}\n"
        f"environment={_environment_document(run_home)!r}\n"
        "result=assess(candidate,environment)\n"
        "assert json.loads(result)['status']=='canonical_containment_inputs_verified_contract_only'\n"
        "sys.stdout.buffer.write(result)\n"
    )
    h4 = _run_installed(venv_python, outside, run_home, h4_code, b"", scripts_dir)
    assert h4.returncode == 0, h4.stderr
    assert h4.stderr == b""

    h5 = _run_installed(
        venv_python,
        outside,
        run_home,
        "from hermes_strict_no_send_preflight_v2 import main; raise SystemExit(main())",
        _envelope(run_home, graph_sha256),
        scripts_dir,
    )
    assert h5.returncode == 0, h5.stderr
    assert h5.stderr == b""
    assert h5.stdout.endswith(b"\n")
    h5_receipt = json.loads(h5.stdout)
    assert h5_receipt["status"] == "hermes_strict_no_send_preflight_verified_contract_only"
    assert h5_receipt["job_count"] == 0
    assert h5_receipt["model_call_count"] == 0
    assert h5_receipt["provider_request_count"] == 0
    assert h5_receipt["actual_output_bytes"] == 0
    assert h5_receipt["actual_cost_usd_microdollars"] == 0
    assert h5_receipt["safe_to_dispatch"] is False

    h5_sha256 = hashlib.sha256(h5.stdout).hexdigest()
    proof_input = _canonical(
        {
            "contract_version": "hermes.strict_runtime_guard.proof.input.v2",
            "expected_h5_receipt_sha256": h5_sha256,
            "h5_receipt_b64": base64.b64encode(h5.stdout).decode("ascii"),
        }
    )
    h6 = _run_installed(
        venv_python,
        outside,
        run_home,
        "from hermes_strict_runtime_guard_v2 import main; raise SystemExit(main())",
        proof_input,
        scripts_dir,
    )
    assert h6.returncode == 0, h6.stderr
    assert h6.stderr == b""
    h6_receipt = json.loads(h6.stdout)
    assert h6_receipt["status"] == "hermes_strict_runtime_guard_mechanics_verified_no_send"
    assert h6_receipt["job_count"] == 0
    assert h6_receipt["model_call_count"] == 0
    assert h6_receipt["provider_request_count"] == 0
    assert h6_receipt["actual_output_bytes"] == 0
    assert h6_receipt["actual_cost_usd_microdollars"] == 0
    assert h6_receipt["safe_to_dispatch"] is False
