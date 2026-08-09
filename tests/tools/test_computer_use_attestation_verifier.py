from __future__ import annotations

import json
import os
import runpy
import shutil
import subprocess
import sys
import atexit
import time
import base64
import concurrent.futures
import hashlib
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "verify_cua_gateway_attestation.py"
_OWNED_DRIVER: subprocess.Popen | None = None


def _stop_owned_driver() -> None:
    global _OWNED_DRIVER
    process = _OWNED_DRIVER
    _OWNED_DRIVER = None
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _ensure_owned_driver() -> None:
    global _OWNED_DRIVER
    if _OWNED_DRIVER is not None and _OWNED_DRIVER.poll() is None:
        return
    from tools.computer_use.cua_backend import resolve_cua_driver_cmd

    driver = resolve_cua_driver_cmd()
    assert driver
    _OWNED_DRIVER = subprocess.Popen(
        [driver, "mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if _OWNED_DRIVER.poll() is not None:
            raise RuntimeError("test-owned cua-driver exited during startup")
        try:
            import psutil

            child = psutil.Process(_OWNED_DRIVER.pid)
            if child.ppid() == os.getpid() and "cua-driver" in child.name().lower():
                return
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        time.sleep(0.05)
    raise RuntimeError("test-owned cua-driver did not become visible")


atexit.register(_stop_owned_driver)


def _receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from tools.computer_use import tool as computer_use

    _ensure_owned_driver()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    receipt = computer_use.write_computer_use_runtime_attestation(
        require_active_cua=True
    )
    # Most verifier unit tests exercise content identity without independent
    # commit authority. Keep that fixture stable whether the developer's
    # source tree is dirty (pre-commit review) or clean (post-deploy review).
    if receipt.get("source_identity", {}).get("kind") == "git-clean":
        receipt["source_identity"]["kind"] = "dirty-attested-source"
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    return path


def _review_root(tmp_path: Path) -> Path:
    """Copy the exact producer bytes, including an uncommitted safe-local fix."""
    from tools.computer_use.tool import _CUA_ATTESTATION_MODULES

    review = tmp_path / "review"
    for relative in _CUA_ATTESTATION_MODULES:
        source = ROOT / relative
        destination = review / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return review


def _committed_review_root(tmp_path: Path) -> Path:
    from tools.computer_use.tool import _CUA_ATTESTATION_MODULES

    review = tmp_path / "review"
    subprocess.run(
        ["git", "clone", "--quiet", "--no-checkout", "--shared", str(ROOT), str(review)],
        check=True,
    )
    subprocess.run(
        ["git", "config", "core.autocrlf", "false"],
        cwd=review,
        check=True,
    )
    subprocess.run(
        ["git", "sparse-checkout", "set", "--no-cone", *_CUA_ATTESTATION_MODULES],
        cwd=review,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "--quiet", "--detach", "HEAD"],
        cwd=review,
        check=True,
    )
    # Materialize the exact current candidate bytes into an authentic clean Git
    # commit.  The production tree may be intentionally dirty during TDD, while
    # the positive verifier fixture must still exercise a real commit/tree.
    for relative in _CUA_ATTESTATION_MODULES:
        destination = review / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    subprocess.run(["git", "add", "--", *_CUA_ATTESTATION_MODULES], cwd=review, check=True)
    subprocess.run(
        [
            "git", "-c", "user.name=Hermes Test", "-c",
            "user.email=hermes-test@example.invalid", "commit", "--quiet",
            "--allow-empty", "-m", "test: materialize current attestation candidate",
        ],
        cwd=review,
        check=True,
    )
    return review


def _rebind_receipt_to_committed_review(receipt: Path, review: Path) -> str:
    """Make a receipt describe the independently checked-out review bytes."""
    data = json.loads(receipt.read_text(encoding="utf-8"))
    verifier = runpy.run_path(str(SCRIPT))
    compiled = {}
    for relative, row in data["modules"].items():
        source = review / relative
        raw = source.read_bytes()
        row.update(source_path=str(source.resolve()), size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        found = {}
        verifier["_codes"](
            compile(raw, str(source.resolve()), "exec", dont_inherit=True, optimize=sys.flags.optimize), found,
        )
        compiled[relative] = found
    for identity, row in data["callables"].items():
        code = compiled[row["source_relative_path"]][row["qualname"]]
        row["first_line"] = code.co_firstlineno
        row["code_sha256"] = verifier["_fingerprint"](code)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=review, text=True).strip()
    source = data["source_identity"]
    source["kind"] = "git-clean"
    source["repository"] = {
        "vcs": "git", "root": str(review.resolve()), "head_commit": head,
        "head_tree": subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=review, text=True).strip(),
    }
    for relative, row in source["attested_paths"].items():
        row["matches_head"] = True
        row["worktree_sha256"] = data["modules"][relative]["sha256"]
        row["head_blob"] = subprocess.check_output(
            ["git", "rev-parse", f"HEAD:{relative}"], cwd=review, text=True,
        ).strip()
    receipt.write_text(json.dumps(data), encoding="utf-8")
    return head


def _verify(
    receipt: Path, review: Path, deployed: Path = ROOT, *, expected_commit: str | None = None,
    verification_receipt: Path | None = None, text: bool = True,
) -> subprocess.CompletedProcess:
    command = [
        sys.executable,
        str(SCRIPT),
        "--receipt",
        str(receipt),
        "--review-root",
        str(review),
        "--deployed-root",
        str(deployed),
    ]
    if expected_commit is not None:
        command.extend(["--expected-commit", expected_commit])
    if verification_receipt is not None:
        command.extend(["--verification-receipt", str(verification_receipt)])
    return subprocess.run(
        command,
        cwd=ROOT,
        text=text,
        capture_output=True,
        check=False,
    )


def test_verifier_accepts_exact_reviewed_source(tmp_path, monkeypatch):
    receipt = _receipt(tmp_path, monkeypatch)
    review = _committed_review_root(tmp_path)
    expected_commit = _rebind_receipt_to_committed_review(receipt, review)
    result = _verify(receipt, review, review, expected_commit=expected_commit)
    assert result.returncode == 0, result.stderr
    assert "verified commit identity" in result.stdout


def test_verifier_revalidates_live_driver_set_after_content_verification(tmp_path, monkeypatch):
    receipt_path = _receipt(tmp_path, monkeypatch)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    review = _review_root(tmp_path)
    verifier = runpy.run_path(str(SCRIPT))
    original = verifier["_live_cua_driver_processes"]
    calls = 0

    def disappearing_driver_set(**kwargs):
        nonlocal calls
        calls += 1
        rows = original(**kwargs)
        return rows if calls == 1 else []

    verifier["verify"].__globals__["_live_cua_driver_processes"] = disappearing_driver_set
    with pytest.raises(verifier["VerificationError"], match="exact live process set"):
        verifier["verify"](receipt, review, ROOT, None)
    assert calls == 2


def test_receipt_and_verifier_bind_fixed_native_runtime_artifacts(tmp_path, monkeypatch):
    """Each critical native byte dependency is fixed, canonical, and rehashed."""
    import hashlib

    receipt = _receipt(tmp_path, monkeypatch)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    artifacts = data["native_runtime_artifacts"]
    assert set(artifacts) == {
        "python_runtime",
        "sqlite3_extension",
        "psutil_extension",
        "cua_driver_launcher",
        "cua_driver_executable",
    }
    for row in artifacts.values():
        path = Path(row["canonical_path"])
        raw = path.read_bytes()
        assert path == path.resolve()
        assert row["size"] == len(raw)
        assert row["sha256"] == hashlib.sha256(raw).hexdigest()

    result = _verify(receipt, _review_root(tmp_path))
    assert result.returncode == 0, result.stderr


def test_verifier_rejects_missing_native_runtime_artifact(tmp_path, monkeypatch):
    receipt = _receipt(tmp_path, monkeypatch)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["native_runtime_artifacts"].pop("sqlite3_extension")
    receipt.write_text(json.dumps(data), encoding="utf-8")

    result = _verify(receipt, _review_root(tmp_path))
    assert result.returncode != 0
    assert "fixed native runtime artifact policy" in result.stderr


def test_verifier_rejects_native_runtime_path_substitution(tmp_path, monkeypatch):
    receipt = _receipt(tmp_path, monkeypatch)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["native_runtime_artifacts"]["cua_driver_executable"]["canonical_path"] = "C:/forged/cua-driver.exe"
    receipt.write_text(json.dumps(data), encoding="utf-8")

    result = _verify(receipt, _review_root(tmp_path))
    assert result.returncode != 0
    assert "native runtime artifact path mismatch" in result.stderr


def test_verifier_rejects_cua_driver_process_identity_or_ancestry_tamper(tmp_path, monkeypatch):
    receipt = _receipt(tmp_path, monkeypatch)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["cua_driver_processes"][0]["process_create_time"] += 10.0
    receipt.write_text(json.dumps(data), encoding="utf-8")
    result = _verify(receipt, _review_root(tmp_path))
    assert result.returncode != 0
    assert "receipt does not match exact live process set" in result.stderr

    receipt = _receipt(tmp_path, monkeypatch)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    row = data["cua_driver_processes"][0]
    if row["parent"] is None:
        row["parent"] = {
            "pid": 1,
            "process_create_time": 1.0,
            "executable": "C:/forged-parent.exe",
        }
    else:
        row["parent"]["pid"] = 0
    receipt.write_text(json.dumps(data), encoding="utf-8")
    result = _verify(receipt, _review_root(tmp_path / "parent"))
    assert result.returncode != 0
    assert "cua-driver gateway ancestry mismatch" in result.stderr


def test_verifier_rejects_real_owned_process_replacement(tmp_path, monkeypatch):
    """A same-command replacement PID cannot inherit a captured live identity."""
    receipt = _receipt(tmp_path, monkeypatch)
    original = json.loads(receipt.read_text(encoding="utf-8"))[
        "cua_driver_processes"
    ][0]
    _stop_owned_driver()
    _ensure_owned_driver()
    assert _OWNED_DRIVER is not None
    assert _OWNED_DRIVER.pid != original["pid"]

    result = _verify(receipt, _review_root(tmp_path))
    assert result.returncode != 0
    assert "receipt does not match exact live process set" in result.stderr


def test_producer_binds_only_exact_gateway_child_processes(monkeypatch):
    from tools.computer_use import tool as computer_use

    class Parent:
        pid = 4242

        @staticmethod
        def create_time():
            return 12.5

        @staticmethod
        def exe():
            return sys.executable

    class Process:
        def __init__(self, pid, ppid):
            self.pid = pid
            self._ppid = ppid
            self.info = {"pid": pid, "name": "cua-driver.exe"}

        def exe(self):
            return sys.executable

        def parent(self):
            return Parent() if self._ppid == Parent.pid else None

        def create_time(self):
            return 20.0 + self.pid

        def ppid(self):
            return self._ppid

        def cmdline(self):
            return [sys.executable, "mcp"]

    monkeypatch.setattr("psutil.Process", lambda pid: Parent())
    monkeypatch.setattr(
        "psutil.process_iter",
        lambda _attrs: [Process(101, Parent.pid), Process(202, 9999)],
    )
    rows = computer_use._live_cua_driver_processes(
        owner_pid=Parent.pid,
        owner_create_time=Parent.create_time(),
        owner_executable=Parent.exe(),
        require_active=True,
    )
    assert [row["pid"] for row in rows] == [101]
    assert rows[0]["parent"] == {
        "pid": Parent.pid,
        "process_create_time": Parent.create_time(),
        "executable": Parent.exe(),
    }


def test_verifier_rejects_subset_duplicate_and_unrelated_driver_rows():
    verifier = runpy.run_path(str(SCRIPT))
    executable = verifier["_native_file_identity"](Path(sys.executable))
    parent = {
        "pid": 4242,
        "process_create_time": 12.5,
        "executable": sys.executable,
    }
    one = {
        "pid": 101,
        "process_create_time": 20.0,
        "ppid": parent["pid"],
        "executable": executable,
        "parent": parent,
        "argv": [sys.executable, "mcp"],
    }
    two = {**one, "pid": 202, "process_create_time": 21.0}
    receipt = {
        "runtime_phase": "cua_active",
        "pid": parent["pid"],
        "process_create_time": parent["process_create_time"],
        "executable": parent["executable"],
        "cua_driver_processes": [one],
    }
    with pytest.raises(Exception, match="exact live process set"):
        verifier["_verify_cua_driver_processes"](receipt, [one, two])

    receipt["cua_driver_processes"] = [one, one]
    with pytest.raises(Exception, match="duplicate cua-driver"):
        verifier["_verify_cua_driver_processes"](receipt, [one])

    unrelated = {
        **one,
        "ppid": 9999,
        "parent": {**parent, "pid": 9999},
    }
    receipt["cua_driver_processes"] = [unrelated]
    with pytest.raises(Exception, match="gateway ancestry"):
        verifier["_verify_cua_driver_processes"](receipt, [unrelated])


def test_git_clean_verification_requires_independent_expected_commit(tmp_path, monkeypatch):
    receipt = _receipt(tmp_path, monkeypatch)
    review = _committed_review_root(tmp_path)
    _rebind_receipt_to_committed_review(receipt, review)
    result = _verify(receipt, review, review)
    assert result.returncode != 0
    assert "expected commit is required" in result.stderr


def test_producer_fsyncs_required_receipts_before_success(tmp_path, monkeypatch):
    from tools.computer_use import tool as computer_use

    calls = []
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setattr(computer_use.os, "fsync", lambda fd: calls.append(fd))
    computer_use.write_computer_use_runtime_attestation()
    assert len(calls) >= 2


def test_atomic_attestation_fsync_failure_preserves_previous_receipt(
    tmp_path, monkeypatch
):
    from tools.computer_use import tool as computer_use

    destination = tmp_path / "receipt.json"
    destination.write_bytes(b"previous")
    monkeypatch.setattr(
        computer_use.os,
        "fsync",
        lambda _fd: (_ for _ in ()).throw(OSError("forced fsync failure")),
    )
    with pytest.raises(OSError, match="forced fsync failure"):
        computer_use._atomic_write_attestation(destination, b"replacement")
    assert destination.read_bytes() == b"previous"
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob(".*.tmp")) == []


def _run_windows_durable_publish_probe(monkeypatch, tmp_path, publisher, outcomes, error):
    import ctypes
    from tools.computer_use import tool as computer_use

    destination = tmp_path / f"{publisher}.json"
    destination.write_bytes(b"prior")
    calls = 0

    class FakeMoveFile:
        argtypes = None
        restype = None

        def __call__(self, source, target, _flags):
            nonlocal calls
            outcome = outcomes[min(calls, len(outcomes) - 1)]
            calls += 1
            if outcome:
                os.replace(source, target)
            return outcome

    fake_move = FakeMoveFile()
    fake_kernel = types.SimpleNamespace(MoveFileExW=fake_move)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: fake_kernel)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    if publisher == "producer":
        publish = lambda: computer_use._atomic_write_attestation(destination, b"new")
    else:
        verifier = runpy.run_path(str(SCRIPT))
        args = types.SimpleNamespace(
            receipt=tmp_path / "input.json",
            review_root=tmp_path / "review",
            deployed_root=tmp_path / "deployed",
            expected_commit=None,
        )
        publish = lambda: verifier["_write_verification_receipt"](
            destination, args, 0, b"new", b""
        )
    return publish, destination, lambda: calls


@pytest.mark.parametrize("publisher", ["producer", "verifier"])
@pytest.mark.parametrize("error", [5, 32])
def test_windows_durable_publish_retries_transient_contention(
    tmp_path, monkeypatch, publisher, error
):
    publish, destination, call_count = _run_windows_durable_publish_probe(
        monkeypatch, tmp_path, publisher, [False, False, True], error
    )
    publish()
    assert call_count() == 3
    assert destination.read_bytes() != b"prior"
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("publisher", ["producer", "verifier"])
@pytest.mark.parametrize("error", [5, 32])
def test_windows_durable_publish_exhaustion_preserves_prior_receipt(
    tmp_path, monkeypatch, publisher, error
):
    publish, destination, call_count = _run_windows_durable_publish_probe(
        monkeypatch, tmp_path, publisher, [False], error
    )
    with pytest.raises(OSError, match="MoveFileExW failed"):
        publish()
    assert call_count() == 50
    assert destination.read_bytes() == b"prior"
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("publisher", ["producer", "verifier"])
def test_windows_durable_publish_does_not_retry_nontransient_error(
    tmp_path, monkeypatch, publisher
):
    publish, destination, call_count = _run_windows_durable_publish_probe(
        monkeypatch, tmp_path, publisher, [False], 87
    )
    with pytest.raises(OSError, match="MoveFileExW failed"):
        publish()
    assert call_count() == 1
    assert destination.read_bytes() == b"prior"
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob(".*.tmp"))


def test_atomic_attestation_concurrent_writers_retry_destination_contention(tmp_path):
    from tools.computer_use import tool as computer_use

    destination = tmp_path / "concurrent-producer.json"
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        futures = [
            pool.submit(
                computer_use._atomic_write_attestation,
                destination,
                json.dumps({"writer": index}).encode("utf-8"),
            )
            for index in range(32)
        ]
        for future in futures:
            future.result(timeout=15)

    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["writer"] in range(32)
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob(".*.tmp"))


def test_atomic_attestation_temp_name_is_bounded_for_long_archive_destination(tmp_path):
    from tools.computer_use import tool as computer_use

    destination = tmp_path / ("a" * 110 + ".json")
    computer_use._atomic_write_attestation(destination, b"bounded-temp")
    assert destination.read_bytes() == b"bounded-temp"
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob(".*.tmp"))


def test_runtime_attestation_archive_paths_are_unique_when_clock_collides(
    tmp_path, monkeypatch
):
    import datetime as real_datetime
    from tools.computer_use import tool as computer_use

    frozen = real_datetime.datetime(2026, 8, 9, 12, 0, tzinfo=real_datetime.timezone.utc)

    class FrozenDateTime:
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is not None else frozen.replace(tzinfo=None)

    fake_datetime = types.SimpleNamespace(
        datetime=FrozenDateTime,
        timezone=real_datetime.timezone,
    )
    paths = []
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setattr(
        computer_use,
        "_atomic_write_attestation",
        lambda path, _raw: paths.append(Path(path)),
    )
    # Warm every fixed callable import before substituting the datetime module.
    computer_use.write_computer_use_runtime_attestation()
    paths.clear()
    monkeypatch.setitem(sys.modules, "datetime", fake_datetime)

    computer_use.write_computer_use_runtime_attestation()
    computer_use.write_computer_use_runtime_attestation()

    archives = [path for path in paths if path.name != "cua_gateway_attestation.json"]
    assert len(archives) == 2
    assert len(set(archives)) == 2
    assert all(path.suffix == ".json" for path in archives)


def test_verifier_rejects_native_runtime_byte_mutation(tmp_path, monkeypatch):
    receipt = _receipt(tmp_path, monkeypatch)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["native_runtime_artifacts"]["psutil_extension"]["sha256"] = "0" * 64
    receipt.write_text(json.dumps(data), encoding="utf-8")

    result = _verify(receipt, _review_root(tmp_path))
    assert result.returncode != 0
    assert "native runtime artifact hash mismatch" in result.stderr


def test_verifier_writes_atomic_byte_exact_live_verification_receipt(tmp_path, monkeypatch):
    receipt = _receipt(tmp_path, monkeypatch)
    review = _review_root(tmp_path)
    verification_receipt = tmp_path / "live-verifier-receipt.json"
    command = [
        sys.executable, str(SCRIPT), "--receipt", str(receipt),
        "--review-root", str(review), "--deployed-root", str(ROOT),
        "--expected-commit", "4f818e9cf4f2ced855c0b73dee92a54a25b3df68",
        "--verification-receipt", str(verification_receipt),
    ]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, check=False)
    assert result.returncode != 0  # The producer is dirty, so a commit claim must fail closed.
    live = json.loads(verification_receipt.read_text(encoding="utf-8"))
    input_attestation = receipt.read_bytes()
    assert live["schema"] == 3
    assert live["input_attestation"] == {
        "path": str(receipt.resolve()),
        "length": len(input_attestation),
        "sha256": hashlib.sha256(input_attestation).hexdigest(),
    }
    assert live["invocation"]["interpreter"] == str(Path(sys.executable).resolve())
    assert live["invocation"]["verifier_path"] == str(SCRIPT.resolve())
    assert live["invocation"]["cwd"] == str(ROOT.resolve())
    assert live["invocation"]["argv"] == [
        str(Path(command[0]).resolve()),
        str(Path(command[1]).resolve()),
        *command[2:],
    ]
    assert live["invocation"]["receipt_path"] == str(receipt)
    assert live["invocation"]["review_root"] == str(review)
    assert live["invocation"]["deployed_root"] == str(ROOT)
    assert live["invocation"]["expected_commit"] == "4f818e9cf4f2ced855c0b73dee92a54a25b3df68"
    assert live["verifier_sha256"]
    assert live["result"]["exit_code"] == result.returncode
    for stream_name, actual in (("stdout", result.stdout), ("stderr", result.stderr)):
        stream = live["result"][stream_name]
        assert base64.b64decode(stream["base64"]) == actual
        assert stream["length"] == len(actual)
        assert stream["sha256"] == hashlib.sha256(actual).hexdigest()


def test_verification_receipt_uses_durable_replace_boundary(tmp_path, monkeypatch):
    verifier = runpy.run_path(str(SCRIPT))
    output = tmp_path / "durable-receipt.json"
    args = types.SimpleNamespace(
        receipt=tmp_path / "input.json",
        review_root=tmp_path / "review",
        deployed_root=tmp_path / "deployed",
        expected_commit=None,
    )
    calls = []

    def durable_replace(temporary, destination):
        calls.append((temporary, destination))
        temporary.replace(destination)

    writer = verifier["_write_verification_receipt"]
    monkeypatch.setitem(writer.__globals__, "_durable_replace", durable_replace)
    writer(output, args, 0, b"ok\n", b"")
    assert len(calls) == 1
    assert calls[0][1] == output
    assert output.is_file()


def test_verification_receipt_survives_input_removal_after_capture(tmp_path):
    verifier = runpy.run_path(str(SCRIPT))
    output = tmp_path / "final-result.json"
    receipt = tmp_path / "input.json"
    receipt.write_bytes(b'{"captured":true}')
    canonical_receipt = receipt.resolve(strict=True)
    captured_bytes = receipt.read_bytes()
    receipt.unlink()
    args = types.SimpleNamespace(
        receipt=receipt,
        review_root=tmp_path / "review",
        deployed_root=tmp_path / "deployed",
        expected_commit=None,
    )

    verifier["_write_verification_receipt"](
        output,
        args,
        0,
        b"ok\n",
        b"",
        captured_bytes,
        canonical_receipt,
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["input_attestation"] == {
        "path": str(canonical_receipt),
        "length": len(captured_bytes),
        "sha256": hashlib.sha256(captured_bytes).hexdigest(),
    }


def test_runtime_process_replacement_gate_is_fixed_attested_policy():
    from tools.computer_use import tool as computer_use

    verifier = runpy.run_path(str(SCRIPT))
    required = {
        "tools.computer_use.cua_backend:_CuaDriverSession.start",
        "tools.computer_use.cua_backend:_CuaDriverSession._attest_runtime_locked",
        "tools.computer_use.cua_backend:_CuaDriverSession._restart_session_locked",
        "tools.computer_use.cua_backend:_CuaDriverSession._call_tool_via_cli",
        "tools.computer_use.cua_backend:CuaDriverBackend.set_runtime_attestation_callback",
    }
    assert required <= set(computer_use._CUA_ATTESTATION_CALLABLES)
    assert required <= set(verifier["CALLABLES"])


def test_verification_receipt_concurrent_writers_use_unique_temp_files(tmp_path):
    verifier = runpy.run_path(str(SCRIPT))
    output = tmp_path / "concurrent-receipt.json"
    args = types.SimpleNamespace(
        receipt=tmp_path / "input.json",
        review_root=tmp_path / "review",
        deployed_root=tmp_path / "deployed",
        expected_commit=None,
    )
    writer = verifier["_write_verification_receipt"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        futures = [
            pool.submit(writer, output, args, index % 2, f"out-{index}".encode(), b"")
            for index in range(32)
        ]
        for future in futures:
            future.result(timeout=15)

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema"] == 3
    assert payload["result"]["exit_code"] in {0, 1}
    assert not list(tmp_path.glob("*.tmp"))


def test_verification_receipt_temp_name_is_bounded_for_long_destination(tmp_path):
    verifier = runpy.run_path(str(SCRIPT))
    short_parent = Path("C:/") / f"cv-{os.getpid()}-{time.time_ns()}"
    short_parent.mkdir(parents=True)
    output = short_parent / ("v" * 220 + ".json")
    vulnerable_temp = short_parent / (
        f".{output.name}.{os.getpid()}.1.{('0' * 32)}.tmp"
    )
    args = types.SimpleNamespace(
        receipt=tmp_path / "input.json",
        review_root=tmp_path / "review",
        deployed_root=tmp_path / "deployed",
        expected_commit=None,
    )
    try:
        assert len(vulnerable_temp.name) > 255
        with pytest.raises(OSError):
            vulnerable_temp.open("wb")
        verifier["_write_verification_receipt"](output, args, 0, b"ok\n", b"")
        assert json.loads(output.read_text(encoding="utf-8"))["schema"] == 3
        assert not list(short_parent.glob("*.tmp"))
        assert not list(short_parent.glob(".*.tmp"))
    finally:
        shutil.rmtree(short_parent, ignore_errors=True)


def test_verification_receipt_preserves_crlf_stream_bytes_without_text_normalization(tmp_path):
    verifier = runpy.run_path(str(SCRIPT))
    output = tmp_path / "crlf-receipt.json"
    args = types.SimpleNamespace(
        receipt=tmp_path / "input.json",
        review_root=tmp_path / "review",
        deployed_root=tmp_path / "deployed",
        expected_commit=None,
    )
    verifier["_write_verification_receipt"](output, args, 7, b"stdout\r\n", b"stderr\r\n")
    data = json.loads(output.read_text(encoding="utf-8"))
    assert base64.b64decode(data["result"]["stdout"]["base64"]) == b"stdout\r\n"
    assert base64.b64decode(data["result"]["stderr"]["base64"]) == b"stderr\r\n"


def test_verifier_rejects_tampered_attested_git_tree(tmp_path, monkeypatch):
    receipt = _receipt(tmp_path, monkeypatch)
    review = _committed_review_root(tmp_path)
    head = _rebind_receipt_to_committed_review(receipt, review)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["source_identity"]["repository"]["head_tree"] = "0" * 40
    receipt.write_text(json.dumps(data), encoding="utf-8")

    result = _verify(receipt, review, review, expected_commit=head)
    assert result.returncode != 0
    assert "review tree mismatch" in result.stderr


def test_verifier_recomputes_callable_fingerprint_from_reviewed_source(tmp_path, monkeypatch):
    receipt = _receipt(tmp_path, monkeypatch)
    review = _review_root(tmp_path)
    target = review / "tools/computer_use/cua_backend.py"
    target.write_text(target.read_text(encoding="utf-8").replace("def _run_input_action(", "def _run_input_action(\n        # reviewed-source mutation\n", 1), encoding="utf-8")
    result = _verify(receipt, review)
    assert result.returncode != 0
    assert "module hash mismatch" in result.stderr or "callable fingerprint mismatch" in result.stderr


def test_verifier_rejects_missing_fixed_cua_policy_entry(tmp_path, monkeypatch):
    receipt = _receipt(tmp_path, monkeypatch)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["callables"].pop("tools.computer_use.cua_backend:CuaDriverBackend._run_input_action")
    receipt.write_text(json.dumps(data), encoding="utf-8")
    result = _verify(receipt, _review_root(tmp_path))
    assert result.returncode != 0
    assert "fixed callable policy" in result.stderr


def test_verifier_rejects_python_semantic_mismatch(tmp_path, monkeypatch):
    receipt = _receipt(tmp_path, monkeypatch)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["runtime"]["python_version"] = [0, 0, 0]
    receipt.write_text(json.dumps(data), encoding="utf-8")
    result = _verify(receipt, _review_root(tmp_path))
    assert result.returncode != 0
    assert "runtime Python semantics mismatch" in result.stderr


def test_verifier_rejects_receipt_controlled_deployed_source_path(tmp_path, monkeypatch):
    receipt = _receipt(tmp_path, monkeypatch)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["modules"]["tools/computer_use/cua_backend.py"]["source_path"] = (
        "C:/forged/deployment/tools/computer_use/cua_backend.py"
    )
    receipt.write_text(json.dumps(data), encoding="utf-8")
    result = _verify(receipt, _review_root(tmp_path))
    assert result.returncode != 0
    assert "receipt deployed source path mismatch" in result.stderr


def test_verifier_rejects_deployed_only_source_mutation(tmp_path, monkeypatch):
    """The deployed root is an independent byte-identity boundary."""
    receipt = _receipt(tmp_path, monkeypatch)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    deployed = _review_root(tmp_path / "deployed")
    for relative, row in data["modules"].items():
        row["source_path"] = str((deployed / relative).resolve())
    target = deployed / "tools/approval.py"
    target.write_bytes(target.read_bytes() + b"\n# deployed-only mutation\n")
    receipt.write_text(json.dumps(data), encoding="utf-8")

    result = _verify(receipt, _review_root(tmp_path / "review"), deployed)
    assert result.returncode != 0
    assert "deployed module hash mismatch" in result.stderr


def test_verifier_rejects_live_launcher_or_parent_identity_mismatch(tmp_path, monkeypatch):
    receipt = _receipt(tmp_path, monkeypatch)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["launcher"] = "C:/forged/launcher.exe"
    receipt.write_text(json.dumps(data), encoding="utf-8")

    result = _verify(receipt, _review_root(tmp_path))
    assert result.returncode != 0
    assert "live process launcher mismatch" in result.stderr

    receipt = _receipt(tmp_path, monkeypatch)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["parent"]["pid"] = 0
    receipt.write_text(json.dumps(data), encoding="utf-8")
    result = _verify(receipt, _review_root(tmp_path / "parent"))
    assert result.returncode != 0
    assert "live parent process identity mismatch" in result.stderr


def test_verifier_rejects_forged_clean_claim_for_dirty_review_source(tmp_path, monkeypatch):
    import hashlib

    receipt = _receipt(tmp_path, monkeypatch)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    review = _committed_review_root(tmp_path)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=review, text=True).strip()
    for relative, row in data["modules"].items():
        raw = (review / relative).read_bytes()
        row.update(
            source_path=str((review / relative).resolve()),
            size=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
        )
    verifier = runpy.run_path(str(SCRIPT))
    compiled = {}
    for relative in data["modules"]:
        found = {}
        verifier["_codes"](
            compile(
                (review / relative).read_bytes(), str(review / relative), "exec",
                dont_inherit=True, optimize=sys.flags.optimize,
            ),
            found,
        )
        compiled[relative] = found
    for identity, row in data["callables"].items():
        code = compiled[row["source_relative_path"]][row["qualname"]]
        row["first_line"] = code.co_firstlineno
        row["code_sha256"] = verifier["_fingerprint"](code)
    target_relative = "tools/approval.py"
    target = review / target_relative
    target.write_text(target.read_text(encoding="utf-8") + "\n# forged dirty review bytes\n", encoding="utf-8")
    raw = target.read_bytes()
    data["modules"][target_relative]["size"] = len(raw)
    data["modules"][target_relative]["sha256"] = hashlib.sha256(raw).hexdigest()
    data["source_identity"]["kind"] = "git-clean"
    data["source_identity"]["repository"] = {
        "vcs": "git",
        "root": str(review),
        "head_commit": head,
        "head_tree": subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=review, text=True).strip(),
    }
    for relative, row in data["source_identity"]["attested_paths"].items():
        row["matches_head"] = True
        row["worktree_sha256"] = data["modules"][relative]["sha256"]
        row["head_blob"] = subprocess.check_output(
            ["git", "rev-parse", f"HEAD:{relative}"], cwd=review, text=True
        ).strip()
    receipt.write_text(json.dumps(data), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--receipt",
            str(receipt),
            "--review-root",
            str(review),
            "--deployed-root",
            str(review),
            "--expected-commit",
            head,
        ],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    assert result.returncode != 0
    assert "does not match clean HEAD" in result.stderr
