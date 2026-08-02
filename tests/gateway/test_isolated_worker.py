from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import gateway.isolated_worker as worker_module

from gateway.isolated_worker import (
    IsolatedWorkerClient,
    IsolatedWorkerServer,
    MAX_FRAME_BYTES,
    PROTOCOL,
    ProtocolError,
    ReadOnlyBind,
    REQUEST_SCHEMA,
    WorkerPolicy,
    canonical_lease_id,
    canonical_bytes,
    parse_request,
)


def _worker_policy(lease_base: Path, **overrides) -> WorkerPolicy:
    shell = Path("/bin/bash")
    values = {
        "expected_peer_uid": os.getuid(),
        "expected_peer_gid": os.getgid(),
        "socket_uid": os.getuid(),
        "socket_gid": os.getgid(),
        "lease_base": lease_base,
        "lease_uid": os.getuid(),
        "lease_gid": os.getgid(),
        "network_isolated": True,
        "bwrap_path": shell,
        "bwrap_sha256": hashlib.sha256(shell.read_bytes()).hexdigest(),
        "bwrap_uid": os.lstat(shell).st_uid,
        "shell": shell,
        "shell_sha256": hashlib.sha256(shell.read_bytes()).hexdigest(),
        "shell_uid": os.lstat(shell).st_uid,
    }
    values.update(overrides)
    return WorkerPolicy(**values)


@pytest.fixture
def worker(tmp_path: Path, monkeypatch):
    lease_ids = {
        "lease-alpha": canonical_lease_id("session-alpha"),
        "lease-bravo": canonical_lease_id("session-bravo"),
    }
    lease_base = tmp_path / "leases"
    lease_base.mkdir(mode=0o700)
    os.chown(lease_base, os.getuid(), os.getgid())
    os.chmod(lease_base, 0o700)
    roots = {
        name: lease_base / lease_id for name, lease_id in lease_ids.items()
    }
    bwrap_test_path = Path("/bin/bash")
    policy = WorkerPolicy(
        expected_peer_uid=os.getuid(),
        expected_peer_gid=os.getgid(),
        socket_uid=os.getuid(),
        socket_gid=os.getgid(),
        lease_base=lease_base,
        lease_uid=os.getuid(),
        lease_gid=os.getgid(),
        network_isolated=True,
        bwrap_path=bwrap_test_path,
        bwrap_sha256=hashlib.sha256(bwrap_test_path.read_bytes()).hexdigest(),
        shell_sha256=hashlib.sha256(Path("/bin/bash").read_bytes()).hexdigest(),
        maximum_timeout_seconds=3,
    )
    socket_root = Path(tempfile.mkdtemp(prefix="iw-", dir="/tmp"))
    socket_path = socket_root / "worker.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    os.chown(socket_path, -1, os.getgid())
    os.chmod(socket_path, 0o660)
    socket_state = os.lstat(socket_path)
    assert (
        socket_state.st_uid,
        socket_state.st_gid,
        socket_state.st_mode & 0o777,
    ) == (os.getuid(), os.getgid(), 0o660)
    listener.listen(8)
    stop = threading.Event()
    server = IsolatedWorkerServer(policy)
    # The production worker attests its exact private tmpfs from mountinfo.
    # Unit tests use an ordinary temporary directory, so inject the already
    # separately-tested topology fact at this boundary.
    monkeypatch.setattr(server, "_attest_quota_topology", lambda: None)
    monkeypatch.setattr(server, "_quota_sentinel", lambda: (1, 1, 1, 1))

    # macOS has neither Linux SO_PEERCRED nor Python getpeereid(). Production
    # remains fail-closed; this fixture supplies the kernel fact so protocol
    # and lifecycle behavior can be exercised cross-platform.
    monkeypatch.setattr(
        worker_module,
        "_peer_credentials",
        lambda _connection: (os.getuid(), os.getgid()),
    )

    # Unit-test process adapter only. The production method always executes
    # the exact digest-bound bwrap inode and has no raw fallback.
    def test_spawn(*, lease, virtual_cwd, command, environment):
        relative = virtual_cwd.relative_to(Path("/workspace"))
        return worker_module.subprocess.Popen(
            ["/bin/bash", "--noprofile", "--norc", "-c", command],
            cwd=lease.root / relative,
            env=dict(environment),
            stdin=worker_module.subprocess.PIPE,
            stdout=worker_module.subprocess.PIPE,
            stderr=worker_module.subprocess.PIPE,
            start_new_session=True,
        )

    monkeypatch.setattr(server, "_spawn_sandboxed", test_spawn)
    thread = threading.Thread(target=server.serve, args=(listener, stop), daemon=True)
    thread.start()
    ready_deadline = time.monotonic() + 2
    while not server._usage_reconciled and time.monotonic() < ready_deadline:
        time.sleep(0.005)
    assert server._usage_reconciled
    try:
        yield socket_path, roots, lease_ids, server
    finally:
        stop.set()
        thread.join(timeout=2)
        listener.close()
        server.close()
        shutil.rmtree(socket_root, ignore_errors=True)


def _client(socket_path: Path, lease_id: str) -> IsolatedWorkerClient:
    return IsolatedWorkerClient(
        socket_path,
        lease_id=lease_id,
        expected_server_uid=os.getuid(),
        expected_server_gid=os.getgid(),
        expected_socket_uid=os.getuid(),
        expected_socket_gid=os.getgid(),
    )


def _collect(client: IsolatedWorkerClient, session_id: str) -> tuple[str, str, dict]:
    stdout = bytearray()
    stderr = bytearray()
    deadline = time.monotonic() + 5
    final: dict = {}
    while time.monotonic() < deadline:
        final = dict(client.poll(session_id, wait_milliseconds=100))
        stdout.extend(base64.b64decode(final["stdout_b64"], validate=True))
        stderr.extend(base64.b64decode(final["stderr_b64"], validate=True))
        if (
            final["state"] != "running"
            and final["drained"]
            and final["complete"]
        ):
            break
    else:  # pragma: no cover - makes a hang fail with a useful assertion
        pytest.fail("worker job did not finish")
    return stdout.decode(), stderr.decode(), final


def _run(
    client: IsolatedWorkerClient,
    command: str,
    *,
    cwd: str = "/workspace",
    timeout_seconds: int = 3,
) -> dict:
    session_id = client.start(
        command,
        cwd=Path(cwd),
        timeout_seconds=timeout_seconds,
    )
    _stdout, _stderr, final = _collect(client, session_id)
    return dict(final["proof_receipt"])


def _request(**changes):
    value = {
        "schema": REQUEST_SCHEMA,
        "protocol": PROTOCOL,
        "request_id": uuid.uuid4().hex,
        "lease_id": canonical_lease_id("session-alpha"),
        "operation": "exec.start",
        "parameters": {
            "command": "true",
            "cwd": "/tmp",
            "stdin_b64": "",
            "timeout_seconds": 1,
        },
    }
    value.update(changes)
    return value


def test_quota_topology_attestation_decodes_exact_tmpfs_mountpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_base = tmp_path / "lease base"
    lease_base.mkdir(mode=0o700)
    os.chown(lease_base, os.getuid(), os.getgid())
    os.chmod(lease_base, 0o700)
    server = IsolatedWorkerServer(_worker_policy(lease_base))
    escaped = str(lease_base).replace("\\", "\\134").replace(" ", "\\040")
    opened = os.fstat(server._lease_base_fd)
    device = f"{os.major(opened.st_dev)}:{os.minor(opened.st_dev)}"
    selected_mount_id = 31
    mountinfo = (
        f"31 20 {device} / {escaped} rw,nosuid,nodev,relatime - "
        "tmpfs tmpfs rw,size=4096k,nr_inodes=10\n"
    )
    original_read_text = Path.read_text

    def read_text(path, *args, **kwargs):
        if str(path) == "/proc/self/mountinfo":
            return mountinfo
        if str(path) == f"/proc/self/fdinfo/{server._lease_base_fd}":
            return f"pos:\t0\nmnt_id:\t{selected_mount_id}\n"
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    filesystem = SimpleNamespace(
        f_blocks=1,
        f_frsize=4096,
        f_files=10,
    )
    monkeypatch.setattr(
        worker_module.os,
        "fstatvfs",
        lambda _fd: filesystem,
    )
    try:
        server._attest_quota_topology()

        selected_mount_id = 32
        mountinfo = (
            f"31 20 {device} / {escaped} rw,nosuid,nodev - "
            "tmpfs tmpfs rw\n"
            f"32 20 {device} / {escaped} rw,nosuid,nodev - "
            "ext4 /dev/test rw\n"
        )
        with pytest.raises(ProtocolError, match="quota_topology_not_tmpfs"):
            server._attest_quota_topology()

        mountinfo = (
            f"31 20 {device} / {escaped} rw,nosuid,nodev - "
            "tmpfs tmpfs rw\n"
            f"32 20 999999:999999 / {escaped} rw,nosuid,nodev - "
            "tmpfs tmpfs rw\n"
        )
        with pytest.raises(
            ProtocolError,
            match="quota_topology_device_mismatch",
        ):
            server._attest_quota_topology()

        selected_mount_id = 31
        mountinfo = (
            f"31 20 {device} / {escaped} rw,nosuid,nodev,relatime - "
            "tmpfs tmpfs rw,size=4096k,nr_inodes=10\n"
        )
        mountinfo = mountinfo.replace(" - tmpfs ", " - ext4 ")
        with pytest.raises(ProtocolError, match="quota_topology_not_tmpfs"):
            server._attest_quota_topology()

        mountinfo = mountinfo.replace(" - ext4 ", " - tmpfs ")
        mountinfo = mountinfo.replace(",nodev", "")
        with pytest.raises(
            ProtocolError,
            match="quota_topology_mount_flags_invalid",
        ):
            server._attest_quota_topology()

        mountinfo = mountinfo.replace(
            "rw,nosuid,relatime",
            "rw,nosuid,nodev,noexec,relatime",
        )
        with pytest.raises(
            ProtocolError,
            match="quota_topology_noexec_invalid",
        ):
            server._attest_quota_topology()

        mountinfo = mountinfo.replace(",noexec", "")
        filesystem.f_blocks = (
            server.policy.global_quota_bytes // filesystem.f_frsize
        ) + 1
        with pytest.raises(
            ProtocolError,
            match="quota_topology_byte_capacity_invalid",
        ):
            server._attest_quota_topology()

        filesystem.f_blocks = 1
        filesystem.f_files = server.policy.global_quota_entries + 2
        with pytest.raises(
            ProtocolError,
            match="quota_topology_inode_capacity_invalid",
        ):
            server._attest_quota_topology()
    finally:
        server.close()


def test_startup_reaps_then_reconciles_each_live_lease_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_base = tmp_path / "leases"
    lease_base.mkdir(mode=0o700)
    os.chown(lease_base, os.getuid(), os.getgid())
    os.chmod(lease_base, 0o700)
    live_id = canonical_lease_id("startup-live")
    stale_id = canonical_lease_id("startup-stale")
    for lease_id in (live_id, stale_id):
        root = lease_base / lease_id
        root.mkdir(mode=0o700)
        os.chown(root, os.getuid(), os.getgid())
        os.chmod(root, 0o700)
    (lease_base / live_id / "payload").write_text("live", encoding="utf-8")
    stale = time.time() - 60
    os.utime(lease_base / stale_id, (stale, stale))

    scans: list[str] = []
    discoveries = 0
    original_usage = IsolatedWorkerServer._lease_usage
    original_discovery = IsolatedWorkerServer._load_existing_leases_locked

    def counted(server, lease):
        scans.append(lease.lease_id)
        return original_usage(server, lease)

    def counted_discovery(server, now):
        nonlocal discoveries
        discoveries += 1
        return original_discovery(server, now)

    monkeypatch.setattr(IsolatedWorkerServer, "_lease_usage", counted)
    monkeypatch.setattr(
        IsolatedWorkerServer,
        "_load_existing_leases_locked",
        counted_discovery,
    )
    server = IsolatedWorkerServer(
        _worker_policy(lease_base, lease_ttl_seconds=1)
    )
    assert scans == []
    monkeypatch.setattr(server, "_attest_quota_topology", lambda: None)
    monkeypatch.setattr(server, "_quota_sentinel", lambda: (1, 1, 1, 1))

    socket_root = Path(tempfile.mkdtemp(prefix="iw-startup-", dir="/tmp"))
    socket_path = socket_root / "worker.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    os.chown(socket_path, os.getuid(), os.getgid())
    os.chmod(socket_path, 0o660)
    listener.listen(1)
    stop = threading.Event()
    stop.set()
    try:
        server.serve(listener, stop)
        assert discoveries == 1
        assert scans == [live_id]
        assert live_id in server._leases
        assert stale_id not in server._leases
        assert not (lease_base / stale_id).exists()
        assert server._global_usage_entries == 2
        assert server._global_usage_bytes == 4
    finally:
        listener.close()
        server.close()
        shutil.rmtree(socket_root, ignore_errors=True)


def test_restart_reconciliation_rejects_exact_aggregate_overage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_base = tmp_path / "leases"
    lease_base.mkdir(mode=0o700)
    os.chown(lease_base, os.getuid(), os.getgid())
    os.chmod(lease_base, 0o700)
    for name in ("restart-a", "restart-b"):
        root = lease_base / canonical_lease_id(name)
        root.mkdir(mode=0o700)
        os.chown(root, os.getuid(), os.getgid())
        os.chmod(root, 0o700)
        (root / "payload").write_bytes(b"x" * 3000)

    server = IsolatedWorkerServer(
        _worker_policy(
            lease_base,
            lease_quota_bytes=4096,
            global_quota_bytes=4096,
        )
    )
    monkeypatch.setattr(server, "_attest_quota_topology", lambda: None)
    monkeypatch.setattr(server, "_quota_sentinel", lambda: (1, 1, 1, 1))
    socket_root = Path(tempfile.mkdtemp(prefix="iw-overage-", dir="/tmp"))
    socket_path = socket_root / "worker.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    os.chown(socket_path, os.getuid(), os.getgid())
    os.chmod(socket_path, 0o660)
    listener.listen(1)
    stop = threading.Event()
    stop.set()
    try:
        with pytest.raises(ProtocolError, match="global_quota_exceeded"):
            server.serve(listener, stop)
        assert server._usage_reconciled
        assert server._global_usage_bytes == 6000
        assert server._global_usage_entries == 4
        assert all(
            lease.usage_state == worker_module._USAGE_EXACT_IDLE
            for lease in server._leases.values()
        )
    finally:
        listener.close()
        server.close()
        shutil.rmtree(socket_root, ignore_errors=True)


def test_protocol_requires_exact_canonical_bounded_frames() -> None:
    value = _request()
    assert parse_request(canonical_bytes(value))["protocol"] == PROTOCOL

    pretty = json.dumps(value, sort_keys=True).encode("ascii")
    assert pretty != canonical_bytes(value)
    with pytest.raises(ProtocolError, match="request_not_canonical"):
        parse_request(pretty)

    with pytest.raises(ProtocolError, match="request_fields_not_exact"):
        parse_request(canonical_bytes({**value, "unexpected": True}))

    with pytest.raises(ProtocolError, match="request_frame_invalid"):
        parse_request(b"x" * (MAX_FRAME_BYTES + 1))

    bad_params = dict(value)
    bad_params["parameters"] = {**value["parameters"], "env": {"TOKEN": "x"}}
    with pytest.raises(ProtocolError, match="request_parameters_fields_not_exact"):
        parse_request(canonical_bytes(bad_params))

    duplicate = canonical_bytes(value).replace(
        b'"protocol":"muncho.isolated-worker.v1"',
        b'"protocol":"muncho.isolated-worker.v1","protocol":"muncho.isolated-worker.v1"',
    )
    with pytest.raises(ProtocolError, match="request_json_duplicate_key"):
        parse_request(duplicate)

    nonfinite = canonical_bytes(value).replace(b'"timeout_seconds":1', b'"timeout_seconds":NaN')
    with pytest.raises(ProtocolError, match="request_json_invalid"):
        parse_request(nonfinite)


def test_default_spawn_is_exact_bwrap_only(worker, monkeypatch) -> None:
    _socket_path, roots, lease_ids, server = worker
    captured: dict = {}

    class DummyProcess:
        pass

    def capture(arguments, **kwargs):
        shell_bind_index = arguments.index("--ro-bind-fd")
        shell_descriptor = int(arguments[shell_bind_index + 1])
        shell_state = os.fstat(shell_descriptor)
        configured_shell_state = os.lstat(server.policy.shell)
        assert (shell_state.st_dev, shell_state.st_ino) == (
            configured_shell_state.st_dev,
            configured_shell_state.st_ino,
        )
        assert arguments[shell_bind_index + 2] == "/run/hermes-shell"
        assert shell_descriptor in kwargs["pass_fds"]
        captured["arguments"] = arguments
        captured["kwargs"] = kwargs
        return DummyProcess()

    monkeypatch.setattr(worker_module.subprocess, "Popen", capture)
    lease = server._ensure_lease(lease_ids["lease-alpha"])
    result = IsolatedWorkerServer._spawn_sandboxed(
        server,
        lease=lease,
        virtual_cwd=Path("/workspace"),
        command="printf safe",
        environment={
            "HOME": str(roots["lease-alpha"]),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "TMPDIR": str(roots["lease-alpha"]),
        },
    )
    assert isinstance(result, DummyProcess)
    arguments = captured["arguments"]
    assert arguments[0].startswith("/proc/self/fd/")
    assert arguments[1:6] == [
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--cap-drop",
        "ALL",
    ]
    bind_sources = [
        arguments[index + 1]
        for index, value in enumerate(arguments)
        if value == "--bind"
    ]
    assert len(bind_sources) == 1
    assert bind_sources[0].startswith("/proc/self/fd/")
    assert str(roots["lease-bravo"]) not in arguments
    assert "/run/credentials" not in arguments
    assert arguments[arguments.index("--dir") + 1] == "/run"
    assert "--tmpfs" in arguments and arguments[arguments.index("--tmpfs") + 1] == "/tmp"
    command_index = arguments.index("--") + 1
    assert arguments[command_index] == "/run/hermes-shell"
    assert str(server.policy.shell) not in arguments[command_index:]
    assert captured["kwargs"]["env"] == {}
    assert len(captured["kwargs"]["pass_fds"]) == 3


def test_bwrap_identity_or_digest_drift_fails_closed(tmp_path: Path) -> None:
    lease_base = tmp_path / "leases"
    lease_base.mkdir(mode=0o700)
    os.chown(lease_base, os.getuid(), os.getgid())
    os.chmod(lease_base, 0o700)
    with pytest.raises(ValueError, match="executable_digest_mismatch"):
        WorkerPolicy(
            expected_peer_uid=os.getuid(),
            expected_peer_gid=os.getgid(),
            socket_uid=os.getuid(),
            socket_gid=os.getgid(),
            lease_base=lease_base,
            lease_uid=os.getuid(),
            lease_gid=os.getgid(),
            network_isolated=True,
            bwrap_path=Path("/bin/bash"),
            bwrap_sha256="0" * 64,
            shell_sha256=hashlib.sha256(Path("/bin/bash").read_bytes()).hexdigest(),
        )


def test_two_leases_have_no_cwd_file_or_output_bleed(worker) -> None:
    socket_path, roots, lease_ids, _server = worker
    alpha = _client(socket_path, lease_ids["lease-alpha"])
    bravo = _client(socket_path, lease_ids["lease-bravo"])
    try:
        alpha_id = alpha.start(
            "printf alpha > marker; printf 'alpha:%s' \"$(pwd)\"",
            cwd=Path("/workspace"),
            timeout_seconds=2,
        )
        bravo_id = bravo.start(
            "printf bravo > marker; printf 'bravo:%s' \"$(pwd)\"",
            cwd=Path("/workspace"),
            timeout_seconds=2,
        )
        alpha_out, alpha_err, alpha_final = _collect(alpha, alpha_id)
        bravo_out, bravo_err, bravo_final = _collect(bravo, bravo_id)

        assert alpha_final["state"] == bravo_final["state"] == "exited"
        assert alpha_final["returncode"] == bravo_final["returncode"] == 0
        assert alpha_err == bravo_err == ""
        assert alpha_out == f"alpha:{roots['lease-alpha']}"
        assert bravo_out == f"bravo:{roots['lease-bravo']}"
        assert (roots["lease-alpha"] / "marker").read_text() == "alpha"
        assert (roots["lease-bravo"] / "marker").read_text() == "bravo"

        with pytest.raises(ProtocolError, match="cwd_invalid|cwd_outside_lease"):
            alpha.start(
                "true",
                cwd=Path("/workspace/../outside"),
                timeout_seconds=1,
            )
        with pytest.raises(ProtocolError, match="session_not_authorized"):
            bravo.poll(alpha_id)
    finally:
        alpha.close()
        bravo.close()


def test_worker_structural_epoch_ignores_test_like_command_semantics(
    worker,
) -> None:
    socket_path, _roots, lease_ids, _server = worker
    client = _client(socket_path, lease_ids["lease-alpha"])
    try:
        initial = client.proof_status()
        assert initial["edit_generation"] == initial["verified_generation"] == 0
        assert initial["status"] == "unverified"
        assert initial["verification"] is None

        _run(client, "mkdir repo")
        edited = _run(
            client,
            (
                "mkdir -p scripts; "
                "printf '[tool.pytest.ini_options]\\n' > pyproject.toml; "
                "printf 'value = 1\\n' > app.py; "
                "printf '#!/bin/bash\\nexit 0\\n' > scripts/run_tests.sh; "
                "chmod +x scripts/run_tests.sh"
            ),
            cwd="/workspace/repo",
        )
        assert edited["edit_generation"] > 0
        assert edited["verified_generation"] == 0
        assert edited["applicability"] == "unknown"
        assert edited["project_root"] == ""
        assert edited["verification"] is None
        pending_after_edit = edited["pending_paths"]
        assert pending_after_edit

        test_like = _run(
            client,
            "scripts/run_tests.sh",
            cwd="/workspace/repo",
        )
        assert test_like["mutation_detection"] == "unchanged"
        assert test_like["edit_generation"] == edited["edit_generation"]
        assert test_like["pending_paths"] == pending_after_edit
        assert test_like["verified_generation"] == 0
        assert test_like["status"] == "unverified"
        assert test_like["verification"] is None

        mutated = _run(
            client,
            "printf 'value = 2\\n' > app.py",
            cwd="/workspace/repo",
        )
        assert mutated["edit_generation"] > test_like["edit_generation"]
        assert mutated["verified_generation"] == 0
        assert mutated["status"] == "unverified"
        assert "/workspace/repo/app.py" in mutated["pending_paths"]

        _run(
            client,
            (
                "printf '#!/bin/bash\\nprintf \"value = 3\\\\n\" > app.py\\n"
                "exit 0\\n' > scripts/run_tests.sh; "
                "chmod +x scripts/run_tests.sh"
            ),
            cwd="/workspace/repo",
        )
        self_mutating = _run(
            client,
            "scripts/run_tests.sh",
            cwd="/workspace/repo",
        )
        assert self_mutating["verification"] is None
        assert self_mutating["mutation_detection"] == "changed"
        assert self_mutating["verified_generation"] == 0
        assert self_mutating["status"] == "unverified"
    finally:
        client.close()


def test_concurrent_commands_advance_only_structural_mutation_epoch(worker) -> None:
    socket_path, _roots, lease_ids, _server = worker
    client = _client(socket_path, lease_ids["lease-alpha"])
    try:
        _run(client, "mkdir repo")
        _run(
            client,
            (
                "mkdir -p scripts; "
                "printf '[tool.pytest.ini_options]\\n' > pyproject.toml; "
                "printf 'value = 1\\n' > app.py; "
                "printf '#!/bin/bash\\nsleep 0.4\\nexit 0\\n' "
                "> scripts/run_tests.sh; chmod +x scripts/run_tests.sh"
            ),
            cwd="/workspace/repo",
        )
        verifier = client.start(
            "scripts/run_tests.sh",
            cwd=Path("/workspace/repo"),
            timeout_seconds=2,
        )
        time.sleep(0.1)
        edit = client.start(
            "printf 'value = 2\\n' > app.py",
            cwd=Path("/workspace/repo"),
            timeout_seconds=2,
        )
        _edit_out, _edit_err, edit_final = _collect(client, edit)
        _verify_out, _verify_err, verify_final = _collect(client, verifier)

        # Active writable siblings make exact mutation comparison unavailable.
        # Record structural uncertainty without interpreting command text.
        assert edit_final["proof_receipt"]["mutation_detection"] == "unknown"
        assert edit_final["proof_receipt"]["status"] == "unverified"
        assert edit_final["proof_receipt"]["verification"] is None
        receipt = verify_final["proof_receipt"]
        assert receipt["verification"] is None
        assert receipt["verified_generation"] == 0
        assert receipt["edit_generation"] > 0
        assert receipt["status"] == "unverified"
    finally:
        client.close()


def test_nested_command_mutation_is_detected_without_project_classification(
    worker,
) -> None:
    socket_path, _roots, lease_ids, _server = worker
    client = _client(socket_path, lease_ids["lease-alpha"])
    try:
        _run(
            client,
            (
                "mkdir -p repo/scripts; "
                "printf 'value = 1\\n' > outside.py; "
                "printf '[tool.pytest.ini_options]\\n' > repo/pyproject.toml; "
                "printf '#!/bin/bash\\nprintf \"value = 2\\\\n\" "
                "> ../outside.py\\nexit 0\\n' > repo/scripts/run_tests.sh; "
                "chmod +x repo/scripts/run_tests.sh; "
                "git -C repo init -q; "
                "git -C repo add pyproject.toml scripts/run_tests.sh; "
                "git -C repo -c user.name='Proof Test' "
                "-c user.email=proof@example.invalid commit -qm initial"
            ),
        )
        before = client.proof_status()
        receipt = _run(
            client,
            "scripts/run_tests.sh",
            cwd="/workspace/repo",
        )

        assert receipt["verification"] is None
        assert receipt["mutation_detection"] == "changed"
        assert "/workspace/outside.py" in receipt["changed_paths"]
        assert receipt["edit_generation"] > before["edit_generation"]
        assert receipt["verified_generation"] == 0
        assert receipt["status"] == "unverified"
    finally:
        client.close()


def test_active_sibling_fences_structural_receipt_before_later_mutation(
    worker,
) -> None:
    socket_path, roots, lease_ids, _server = worker
    client = _client(socket_path, lease_ids["lease-alpha"])
    try:
        _run(client, "mkdir repo")
        _run(
            client,
            (
                "mkdir -p scripts; "
                "printf '[tool.pytest.ini_options]\\n' > pyproject.toml; "
                "printf 'value = 1\\n' > app.py; "
                "printf '#!/bin/bash\\nexit 0\\n' > scripts/run_tests.sh; "
                "chmod +x scripts/run_tests.sh"
            ),
            cwd="/workspace/repo",
        )
        unchanged = _run(
            client,
            "scripts/run_tests.sh",
            cwd="/workspace/repo",
        )
        assert unchanged["status"] == "unverified"
        assert unchanged["verification"] is None

        sibling = client.start(
            (
                "while [ ! -f mutate-now ]; do sleep 0.02; done; "
                "printf 'value = 2\\n' > app.py; rm mutate-now"
            ),
            cwd=Path("/workspace/repo"),
            timeout_seconds=3,
        )
        verifier = client.start(
            "scripts/run_tests.sh",
            cwd=Path("/workspace/repo"),
            timeout_seconds=2,
        )
        _stdout, _stderr, verifier_final = _collect(client, verifier)
        receipt = verifier_final["proof_receipt"]
        assert receipt["verification"] is None
        assert receipt["mutation_detection"] == "unknown"
        assert receipt["status"] == "unverified"
        assert receipt["verified_generation"] == 0
        assert receipt["edit_generation"] > unchanged["edit_generation"]

        (roots["lease-alpha"] / "repo" / "mutate-now").write_text(
            "go",
            encoding="utf-8",
        )
        _stdout, _stderr, sibling_final = _collect(client, sibling)
        assert sibling_final["state"] == "exited"
        assert (
            roots["lease-alpha"] / "repo" / "app.py"
        ).read_text(encoding="utf-8") == "value = 2\n"
    finally:
        client.close()


def test_structural_sidecar_reload_closes_out_of_band_crash_window(worker) -> None:
    socket_path, roots, lease_ids, server = worker
    client = _client(socket_path, lease_ids["lease-alpha"])
    try:
        _run(client, "mkdir repo")
        _run(
            client,
            (
                "mkdir -p scripts; "
                "printf '[tool.pytest.ini_options]\\n' > pyproject.toml; "
                "printf 'value = 1\\n' > app.py; "
                "printf '#!/bin/bash\\nexit 0\\n' > scripts/run_tests.sh; "
                "chmod +x scripts/run_tests.sh"
            ),
            cwd="/workspace/repo",
        )
        before = _run(
            client,
            "scripts/run_tests.sh",
            cwd="/workspace/repo",
        )
        assert before["status"] == "unverified"

        # Simulate a crash after material mutation but before command receipt
        # persistence, then force the next socket status read to reload disk.
        (roots["lease-alpha"] / "repo" / "app.py").write_text(
            "value = 99\n",
            encoding="utf-8",
        )
        lease = server._leases[lease_ids["lease-alpha"]]
        with lease.proof_lock:
            lease.proof_state = None
        recovered = client.proof_status()
        assert recovered["edit_generation"] > before["edit_generation"]
        assert recovered["verified_generation"] == 0
        assert recovered["status"] == "unverified"
        assert recovered["verification"] is None
        assert recovered["pending_paths"]
    finally:
        client.close()


def test_wrapper_parser_extracts_only_structural_nested_virtual_cwd() -> None:
    wrapped = (
        "source /workspace/.hermes-runtime/snap >/dev/null 2>&1 || true\n"
        "builtin cd -- /workspace/nested/repo || exit 126\n"
        "eval 'scripts/run_tests.sh'\n"
        "__hermes_ec=$?\n"
        "umask 077\n"
        "exit $__hermes_ec"
    )
    assert worker_module._executed_virtual_cwd(
        wrapped,
        Path("/workspace"),
    ) == Path("/workspace/nested/repo")

    injected = (
        "printf before\n"
        "eval 'scripts/run_tests.sh'\n"
        "__hermes_ec=$?\n"
        "printf after"
    )
    assert worker_module._executed_virtual_cwd(
        injected,
        Path("/workspace"),
    ) == Path("/workspace")


def test_worker_forwards_exact_allowlisted_environment_only(worker, monkeypatch) -> None:
    socket_path, roots, lease_ids, _server = worker
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-cross")
    monkeypatch.setenv("LD_PRELOAD", "/tmp/must-not-cross.so")
    client = _client(socket_path, lease_ids["lease-alpha"])
    try:
        session_id = client.start(
            "printf '%s|%s|%s' \"${OPENAI_API_KEY-unset}\" "
            "\"${LD_PRELOAD-unset}\" \"$PATH\"",
            cwd=Path("/workspace"),
            timeout_seconds=2,
        )
        stdout, stderr, final = _collect(client, session_id)
        assert final["returncode"] == 0
        assert stderr == ""
        assert stdout == "unset|unset|/usr/bin:/bin"
        assert "must-not-cross" not in stdout
    finally:
        client.close()


def test_timeout_and_cancel_are_session_bound(worker) -> None:
    socket_path, roots, lease_ids, _server = worker
    client = _client(socket_path, lease_ids["lease-alpha"])
    try:
        timed = client.start(
            "sleep 5", cwd=Path("/workspace"), timeout_seconds=1
        )
        _stdout, _stderr, timed_final = _collect(client, timed)
        assert timed_final["state"] == "timed_out"
        assert timed_final["returncode"] is not None

        cancelled = client.start(
            "sleep 5", cwd=Path("/workspace"), timeout_seconds=3
        )
        receipt = client.cancel(cancelled)
        assert receipt == {"session_id": cancelled, "state": "cancelled"}
        _stdout, _stderr, cancelled_final = _collect(client, cancelled)
        assert cancelled_final["state"] == "cancelled"
        assert cancelled_final["returncode"] is not None
    finally:
        client.close()


def test_completed_unpolled_jobs_keep_cross_connection_lease_reservations(
    worker,
) -> None:
    socket_path, _roots, lease_ids, server = worker
    first = _client(socket_path, lease_ids["lease-alpha"])
    second = _client(socket_path, lease_ids["lease-alpha"])
    sessions: list[str] = []
    lease = server._ensure_lease(lease_ids["lease-alpha"])
    try:
        for _index in range(server.policy.maximum_active_jobs_per_lease):
            sessions.append(
                first.start(
                    "printf retained",
                    cwd=Path("/workspace"),
                    timeout_seconds=2,
                )
            )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with lease.usage_lock:
                if not lease.active_executions:
                    break
            time.sleep(0.01)
        else:
            pytest.fail("completed jobs remained active")
        assert lease.jobs == server.policy.maximum_active_jobs_per_lease

        with pytest.raises(
            ProtocolError,
            match="lease_job_capacity_exhausted",
        ):
            second.start(
                "true",
                cwd=Path("/workspace"),
                timeout_seconds=1,
            )

        _collect(first, sessions.pop())
        admitted = second.start(
            "true",
            cwd=Path("/workspace"),
            timeout_seconds=1,
        )
        _stdout, _stderr, final = _collect(second, admitted)
        assert final["state"] == "exited"
        for session in sessions:
            _collect(first, session)
    finally:
        first.close()
        second.close()


def test_disconnect_kills_lease_job(worker) -> None:
    socket_path, roots, lease_ids, _server = worker
    client = _client(socket_path, lease_ids["lease-alpha"])
    session_id = client.start(
        "printf '%s' $$ > pid; sleep 30",
        cwd=Path("/workspace"),
        timeout_seconds=3,
    )
    assert session_id.startswith("job-")
    pid_path = roots["lease-alpha"] / "pid"
    deadline = time.monotonic() + 2
    while not pid_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    pid = int(pid_path.read_text())
    client.close()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail("worker child survived client disconnect")


def test_accept_loop_closes_connections_above_global_cap(worker, monkeypatch) -> None:
    socket_path, _roots, _lease_ids, server = worker
    monkeypatch.setattr(worker_module, "MAX_ACTIVE_CONNECTIONS", 1)

    first = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    second = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    first.connect(str(socket_path))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with server._threads_lock:
            if len(server._threads) == 1:
                break
        time.sleep(0.01)
    else:
        pytest.fail("first connection was not accepted")

    try:
        second.connect(str(socket_path))
        second.settimeout(0.1)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                if second.recv(1) == b"":
                    break
            except socket.timeout:
                continue
        else:
            pytest.fail("connection above cap was not closed")
    finally:
        first.close()
        second.close()


def test_symlink_cwd_and_peer_uid_spoof_fail_closed(worker, tmp_path: Path) -> None:
    socket_path, roots, lease_ids, server = worker
    server._ensure_lease(lease_ids["lease-alpha"])
    link = roots["lease-alpha"] / "escape"
    link.symlink_to(tmp_path)
    client = _client(socket_path, lease_ids["lease-alpha"])
    try:
        with pytest.raises(
            ProtocolError,
            match="cwd_symlink_or_not_directory|lease_contains_symlink",
        ):
            client.start("true", cwd=Path("/workspace/escape"), timeout_seconds=1)
    finally:
        client.close()

    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    original = worker_module._peer_credentials
    worker_module._peer_credentials = lambda _connection: (os.getuid() + 1, os.getgid())
    try:
        with pytest.raises(ProtocolError, match="peer_uid_not_authorized"):
            server.serve_connection(left)
    finally:
        worker_module._peer_credentials = original
        right.close()


def test_rejected_start_releases_lease_job_reservation(worker, monkeypatch) -> None:
    _socket_path, _roots, lease_ids, server = worker
    lease = server._ensure_lease(lease_ids["lease-alpha"])
    monkeypatch.setattr(
        server,
        "_validate_cwd",
        lambda _lease, _cwd: (_ for _ in ()).throw(ProtocolError("cwd_invalid")),
    )

    with pytest.raises(ProtocolError, match="cwd_invalid"):
        server._start(
            lease,
            {
                "command": "true",
                "cwd": "/workspace",
                "stdin_b64": "",
                "timeout_seconds": 1,
            },
            {},
        )

    assert lease.jobs == 0


def test_workspace_scan_failure_kills_child_instead_of_losing_monitor(
    worker,
    monkeypatch,
) -> None:
    socket_path, _roots, lease_ids, server = worker
    two_processes_started = threading.Event()
    spawn_count = 0
    original_spawn = server._spawn_sandboxed
    original_usage = server._lease_usage

    def mark_started(**kwargs):
        nonlocal spawn_count
        process = original_spawn(**kwargs)
        spawn_count += 1
        if spawn_count == 2:
            two_processes_started.set()
        return process

    def fail_after_start(lease):
        if two_processes_started.is_set():
            raise OSError("workspace changed during scan")
        return original_usage(lease)

    monkeypatch.setattr(server, "_spawn_sandboxed", mark_started)
    monkeypatch.setattr(server, "_lease_usage", fail_after_start)
    monkeypatch.setattr(
        server,
        "_quota_sentinel",
        lambda: (0, 0, 0, 0)
        if two_processes_started.is_set()
        else (1, 1, 1, 1),
    )
    client = _client(socket_path, lease_ids["lease-alpha"])
    try:
        first = client.start(
            "sleep 5",
            cwd=Path("/workspace"),
            timeout_seconds=3,
        )
        second = client.start(
            "sleep 5",
            cwd=Path("/workspace"),
            timeout_seconds=3,
        )
        _stdout, _stderr, first_final = _collect(client, first)
        _stdout, _stderr, second_final = _collect(client, second)
        assert first_final["state"] == "quota_exceeded"
        assert second_final["state"] == "quota_exceeded"
        assert first_final["returncode"] is not None
        assert second_final["returncode"] is not None

        lease = server._leases[lease_ids["lease-alpha"]]
        with lease.usage_lock:
            assert lease.usage_state == worker_module._USAGE_POISONED
            assert lease.quota_monitor_token is None
            assert lease.active_executions == []
        with pytest.raises(ProtocolError, match="lease_usage_poisoned"):
            client.start(
                "true",
                cwd=Path("/workspace"),
                timeout_seconds=1,
            )
    finally:
        client.close()


def test_exact_idle_start_has_zero_prescan_and_each_last_writer_scans_once(
    worker,
    monkeypatch,
) -> None:
    socket_path, _roots, lease_ids, server = worker
    lease = server._ensure_lease(lease_ids["lease-alpha"])
    scans = 0
    original = server._lease_usage

    def counted(scanned_lease):
        nonlocal scans
        assert scanned_lease is lease
        scans += 1
        return original(scanned_lease)

    monkeypatch.setattr(server, "_lease_usage", counted)
    client = _client(socket_path, lease_ids["lease-alpha"])
    try:
        first = client.start(
            "sleep 0.2",
            cwd=Path("/workspace"),
            timeout_seconds=2,
        )
        assert scans == 0
        _stdout, _stderr, first_final = _collect(client, first)
        assert first_final["state"] == "exited"
        assert scans == 1

        second = client.start(
            "sleep 0.2",
            cwd=Path("/workspace"),
            timeout_seconds=2,
        )
        assert scans == 1
        _stdout, _stderr, second_final = _collect(client, second)
        assert second_final["state"] == "exited"
        assert scans == 2
        with lease.usage_lock:
            assert lease.usage_state == worker_module._USAGE_EXACT_IDLE
            assert lease.quota_monitor_token is None
    finally:
        client.close()


def test_quota_monitor_uses_changed_sentinel_then_sparse_fallback_cadence(
    worker,
    monkeypatch,
) -> None:
    _socket_path, _roots, lease_ids, server = worker
    lease = server._ensure_lease(lease_ids["lease-alpha"])
    now = 0.0
    waits: list[float] = []
    scan_starts: list[float] = []

    class AdvancingWake:
        def wait(self, delay):
            nonlocal now
            waits.append(delay)
            now += delay
            if len(waits) == 2:
                lease.active_executions.clear()
            return False

        def clear(self):
            return None

        def set(self):
            return None

    token = worker_module._QuotaMonitorToken()
    token.wake = AdvancingWake()
    monkeypatch.setattr(server, "_quota_clock", lambda: now)

    def sampled(_lease):
        scan_starts.append(now)
        return (0, 0)

    monkeypatch.setattr(server, "_lease_usage", sampled)
    with lease.usage_lock:
        lease.active_executions.append(object())
        lease.usage_state = worker_module._USAGE_DIRTY_ACTIVE
        lease.quota_monitor_token = token
        lease.quota_last_scan_started_monotonic = 0.0
        lease.usage_sample_started_monotonic = 0.0
        lease.quota_sentinel_epoch_seen = 0
        lease.quota_sentinel_dirty = False
        lease.quota_near_limit = False
    with server._leases_lock:
        server._quota_sentinel_epoch = 1
        server._quota_dirty_leases[lease.lease_id] = token

    server._quota_monitor_loop_inner(lease, token)

    assert scan_starts == [worker_module._QUOTA_NORMAL_SCAN_INTERVAL_SECONDS]
    assert waits == [
        worker_module._QUOTA_NORMAL_SCAN_INTERVAL_SECONDS,
        worker_module._QUOTA_SPARSE_FALLBACK_SECONDS,
    ]
    with lease.usage_lock:
        assert lease.quota_monitor_token is None


def test_quota_pressure_enters_on_projection_and_exits_below_hysteresis(
    worker,
) -> None:
    _socket_path, _roots, lease_ids, server = worker
    lease = server._ensure_lease(lease_ids["lease-alpha"])
    with lease.usage_lock:
        lease.usage_sample = (1_000, 0)
        lease.usage_sample_started_monotonic = 0.01
        lease.quota_near_limit = False
        server._update_quota_pressure_locked(
            lease,
            previous_sample=(0, 0),
            previous_started=0.0,
        )
        assert lease.quota_near_limit

        lease.usage_sample = (69_999, 0)
        lease.usage_sample_started_monotonic = 1.01
        server._update_quota_pressure_locked(
            lease,
            previous_sample=(80_000, 0),
            previous_started=0.01,
        )
        assert not lease.quota_near_limit


def test_eight_sibling_writers_share_one_epoch_and_only_last_exit_scans(
    worker,
    monkeypatch,
) -> None:
    socket_path, roots, lease_ids, server = worker
    lease = server._ensure_lease(lease_ids["lease-alpha"])
    scans = 0
    scans_in_flight = 0
    maximum_scans_in_flight = 0
    scan_lock = threading.Lock()
    original = server._lease_usage

    def counted(scanned_lease):
        nonlocal scans, scans_in_flight, maximum_scans_in_flight
        assert scanned_lease is lease
        with scan_lock:
            scans += 1
            scans_in_flight += 1
            maximum_scans_in_flight = max(
                maximum_scans_in_flight,
                scans_in_flight,
            )
        try:
            time.sleep(0.03)
            return original(scanned_lease)
        finally:
            with scan_lock:
                scans_in_flight -= 1

    monkeypatch.setattr(server, "_lease_usage", counted)
    client = _client(socket_path, lease_ids["lease-alpha"])
    sessions: list[str] = []
    tokens: list[object] = []
    release = roots["lease-alpha"] / "release"
    try:
        for _index in range(8):
            sessions.append(
                client.start(
                    "while [ ! -f release ]; do sleep 0.02; done",
                    cwd=Path("/workspace"),
                    timeout_seconds=3,
                )
            )
            with lease.usage_lock:
                assert len(lease.active_executions) == len(sessions)
                tokens.append(lease.quota_monitor_token)
        assert tokens[0] is not None
        assert all(token is tokens[0] for token in tokens)
        assert scans == 0

        release.write_text("go", encoding="utf-8")
        finals = [_collect(client, session)[2] for session in sessions]
        assert all(final["state"] == "exited" for final in finals)
        assert scans == 1
        assert maximum_scans_in_flight == 1
        with lease.usage_lock:
            assert lease.active_executions == []
            assert lease.usage_state == worker_module._USAGE_EXACT_IDLE
            assert lease.quota_monitor_token is None
    finally:
        client.close()


def test_quota_monitor_thread_start_failure_poison_kills_and_rejects_retry(
    worker,
    monkeypatch,
) -> None:
    _socket_path, _roots, lease_ids, server = worker
    lease = server._ensure_lease(lease_ids["lease-alpha"])
    captured_processes = []
    original_spawn = server._spawn_sandboxed
    original_thread_start = threading.Thread.start

    def capture_spawn(**kwargs):
        process = original_spawn(**kwargs)
        captured_processes.append(process)
        return process

    def fail_quota_monitor_start(thread):
        target = getattr(thread, "_target", None)
        if getattr(target, "__name__", "") == "_quota_monitor_loop":
            raise RuntimeError("injected quota monitor start failure")
        return original_thread_start(thread)

    monkeypatch.setattr(server, "_spawn_sandboxed", capture_spawn)
    monkeypatch.setattr(threading.Thread, "start", fail_quota_monitor_start)
    params = {
        "command": "sleep 5",
        "cwd": "/workspace",
        "stdin_b64": "",
        "timeout_seconds": 3,
    }
    with pytest.raises(ProtocolError, match="quota_monitor_start_failed"):
        server._start(lease, params, {})

    assert len(captured_processes) == 1
    assert captured_processes[0].poll() is not None
    with lease.usage_lock:
        assert lease.active_executions == []
        assert lease.usage_state == worker_module._USAGE_POISONED
        assert lease.quota_monitor_token is None
    with pytest.raises(ProtocolError, match="lease_usage_poisoned"):
        server._start(lease, params, {})
    assert len(captured_processes) == 1


def test_execution_thread_start_failure_poison_kills_every_sibling(
    worker,
    monkeypatch,
) -> None:
    _socket_path, _roots, lease_ids, server = worker
    lease = server._ensure_lease(lease_ids["lease-alpha"])
    first_executions = {}
    params = {
        "command": "sleep 5",
        "cwd": "/workspace",
        "stdin_b64": "",
        "timeout_seconds": 3,
    }
    first_result = server._start(lease, params, first_executions)
    first_id = first_result["session_id"]
    first_execution = first_executions[first_id]
    captured = []
    original_spawn = server._spawn_sandboxed
    original_thread_start = threading.Thread.start

    def capture_spawn(**kwargs):
        process = original_spawn(**kwargs)
        captured.append(process)
        return process

    def fail_execution_monitor_start(thread):
        target = getattr(thread, "_target", None)
        if getattr(target, "__name__", "") == "monitor_execution":
            raise RuntimeError("injected execution monitor start failure")
        return original_thread_start(thread)

    monkeypatch.setattr(server, "_spawn_sandboxed", capture_spawn)
    monkeypatch.setattr(
        threading.Thread,
        "start",
        fail_execution_monitor_start,
    )
    with pytest.raises(
        ProtocolError,
        match="execution_monitor_start_failed",
    ):
        server._start(lease, params, {})

    assert len(captured) == 1
    assert captured[0].poll() is not None
    assert first_execution.complete.wait(2)
    assert first_execution.process.poll() is not None
    assert first_execution.state == "quota_exceeded"
    with lease.usage_lock:
        assert lease.active_executions == []
        assert lease.usage_state == worker_module._USAGE_POISONED
        assert lease.quota_monitor_token is None

    polled = server._poll(
        lease.lease_id,
        {
            "session_id": first_id,
            "wait_milliseconds": 1000,
        },
        first_executions,
    )
    assert polled["state"] == "quota_exceeded"
    assert lease.jobs == 0


def test_quota_monitor_scans_only_active_lease_and_short_job_pays_exit_scan(
    worker,
    monkeypatch,
) -> None:
    socket_path, roots, lease_ids, server = worker
    alpha_lease = server._ensure_lease(lease_ids["lease-alpha"])
    bravo_lease = server._ensure_lease(lease_ids["lease-bravo"])
    for index in range(250):
        (roots["lease-alpha"] / f"alpha-{index}.txt").write_text(
            "a",
            encoding="utf-8",
        )
        (roots["lease-bravo"] / f"bravo-{index}.txt").write_text(
            "b",
            encoding="utf-8",
        )
    server._global_usage()

    counts = {alpha_lease.lease_id: 0, bravo_lease.lease_id: 0}
    original = server._lease_usage

    def counted(lease):
        counts[lease.lease_id] += 1
        return original(lease)

    monkeypatch.setattr(server, "_lease_usage", counted)
    monkeypatch.setattr(
        server,
        "_load_existing_leases_locked",
        lambda _now: (_ for _ in ()).throw(
            AssertionError("hot path rediscovered lease roots")
        ),
    )
    monkeypatch.setattr(
        server,
        "_proof_authority_usage",
        lambda: (_ for _ in ()).throw(
            AssertionError("hot path rescanned proof authority")
        ),
    )
    client = _client(socket_path, lease_ids["lease-alpha"])
    started = time.monotonic()
    try:
        session_id = client.start(
            "sleep 1",
            cwd=Path("/workspace"),
            timeout_seconds=2,
        )
        _stdout, _stderr, final = _collect(client, session_id)
    finally:
        client.close()

    assert final["state"] == "exited"
    assert time.monotonic() - started < 2.5
    # Exact-idle admission performs no walk.  With no physical block/inode
    # sentinel change, a sub-2s job pays only the last-writer exit scan.
    assert counts[alpha_lease.lease_id] == 1
    assert counts[bravo_lease.lease_id] == 0


def test_startup_removes_only_exact_owned_proof_temp(tmp_path: Path) -> None:
    lease_base = tmp_path / "leases"
    lease_base.mkdir(mode=0o700)
    os.chown(lease_base, os.getuid(), os.getgid())
    os.chmod(lease_base, 0o700)
    proof_root = lease_base / ".hermes-runtime"
    proof_root.mkdir(mode=0o700)
    os.chown(proof_root, os.getuid(), os.getgid())
    os.chmod(proof_root, 0o700)
    lease_id = canonical_lease_id("orphan-proof-temp")
    orphan = proof_root / f".{lease_id}.{'a' * 32}.tmp"
    orphan.write_bytes(b"partial")
    os.chown(orphan, os.getuid(), os.getgid())
    orphan.chmod(0o600)

    server = IsolatedWorkerServer(_worker_policy(lease_base))
    try:
        assert not orphan.exists()
    finally:
        server.close()


def test_git_snapshot_uses_porcelain_without_full_tree_walk(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess = worker_module.subprocess
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    (repo / ".gitignore").write_text(
        "build/\ndist/\nnode_modules/\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(repo), "add", "tracked.py", ".gitignore"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Proof Test",
            "-c",
            "user.email=proof@example.invalid",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )
    build = repo / "build"
    build.mkdir()
    (build / "source.py").write_text("value = 2\n", encoding="utf-8")
    dist = repo / "dist"
    dist.mkdir()
    (dist / "config.json").write_text('{"value": 3}\n', encoding="utf-8")
    node_modules = repo / "node_modules"
    node_modules.mkdir()
    for index in range(300):
        (node_modules / f"package-{index}.js").write_text(
            "module.exports = 1\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(
        worker_module.os,
        "walk",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("git material snapshot must not walk the full tree")
        ),
    )
    snapshot = worker_module._material_snapshot(repo, repo)
    snapshot_files = dict(snapshot.files)
    assert "build/source.py" in snapshot_files
    assert "dist/config.json" in snapshot_files
    assert not any("node_modules" in path for path in snapshot_files)


def test_non_git_fallback_covers_soft_build_source_without_walking_hard_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "build").mkdir(parents=True)
    (workspace / "build" / "source.py").write_text(
        "value = 1\n",
        encoding="utf-8",
    )
    (workspace / "build" / "artifact.bin").write_bytes(b"ignored")
    (workspace / "node_modules").mkdir()
    for index in range(300):
        (workspace / "node_modules" / f"package-{index}.js").write_text(
            "module.exports = 1\n",
            encoding="utf-8",
        )

    original_scandir = worker_module.os.scandir

    def guarded_scandir(path):
        if "node_modules" in Path(path).parts:
            raise AssertionError("hard cache tree was traversed")
        return original_scandir(path)

    monkeypatch.setattr(worker_module.os, "scandir", guarded_scandir)
    before = worker_module._material_snapshot(workspace, workspace)
    (workspace / "build" / "source.py").write_text(
        "value = 2\n",
        encoding="utf-8",
    )
    (workspace / "build" / "artifact.bin").write_bytes(b"still ignored")
    after = worker_module._material_snapshot(workspace, workspace)

    assert worker_module._material_fingerprint(before) != (
        worker_module._material_fingerprint(after)
    )
    assert worker_module._changed_material_paths(before, after) == [
        "/workspace/build/source.py"
    ]
    assert "build/artifact.bin" not in dict(after.files)
    assert not any("node_modules" in path for path, _digest in after.files)


def test_nested_dirty_repo_content_changes_combined_fingerprint(
    tmp_path: Path,
) -> None:
    outer = tmp_path / "outer"
    inner = outer / "embedded"
    inner.mkdir(parents=True)
    subprocess = worker_module.subprocess
    for repo in (outer, inner):
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (outer / "outer.py").write_text("outer = 1\n", encoding="utf-8")
    (inner / "inner.py").write_text("inner = 1\n", encoding="utf-8")
    for repo, path in ((outer, "outer.py"), (inner, "inner.py")):
        subprocess.run(["git", "-C", str(repo), "add", path], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=Proof Test",
                "-c",
                "user.email=proof@example.invalid",
                "commit",
                "-qm",
                "initial",
            ],
            check=True,
        )

    (inner / "inner.py").write_text("inner = 2\n", encoding="utf-8")
    before = worker_module._material_snapshot(outer, inner)
    (inner / "inner.py").write_text("inner = 3\n", encoding="utf-8")
    after = worker_module._material_snapshot(outer, inner)

    assert worker_module._material_fingerprint(before) != (
        worker_module._material_fingerprint(after)
    )
    assert "/workspace/embedded/inner.py" in (
        worker_module._changed_material_paths(before, after)
    )


def test_global_byte_quota_blocks_aggregate_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_base = tmp_path / "leases"
    lease_base.mkdir(mode=0o700)
    os.chown(lease_base, os.getuid(), os.getgid())
    os.chmod(lease_base, 0o700)
    policy = _worker_policy(
        lease_base,
        lease_quota_bytes=4096,
        lease_quota_entries=4,
        global_quota_bytes=6000,
        global_quota_entries=10,
    )
    server = IsolatedWorkerServer(policy)
    alpha = server._ensure_lease(canonical_lease_id("aggregate-alpha"))
    bravo = server._ensure_lease(canonical_lease_id("aggregate-bravo"))
    try:
        (alpha.root / "payload").write_bytes(b"a" * 3000)
        (bravo.root / "payload").write_bytes(b"b" * 3000)
        assert server._global_usage() == (4, 6000)

        (bravo.root / "overage").write_bytes(b"x")
        assert server._lease_usage(alpha) == (1, 3000)
        assert server._lease_usage(bravo) == (2, 3001)
        with pytest.raises(ProtocolError, match="global_quota_exceeded"):
            server._global_usage()

        monkeypatch.setattr(
            server,
            "_spawn_sandboxed",
            lambda **_kwargs: pytest.fail("aggregate-over-quota job was spawned"),
        )
        with pytest.raises(ProtocolError, match="global_quota_exceeded"):
            server._start(
                alpha,
                {
                    "command": "true",
                    "cwd": "/workspace",
                    "stdin_b64": "",
                    "timeout_seconds": 1,
                },
                {},
            )
    finally:
        server.close()


def test_global_entry_quota_counts_all_lease_roots(tmp_path: Path) -> None:
    lease_base = tmp_path / "leases"
    lease_base.mkdir(mode=0o700)
    os.chown(lease_base, os.getuid(), os.getgid())
    os.chmod(lease_base, 0o700)
    policy = _worker_policy(
        lease_base,
        lease_quota_bytes=4096,
        lease_quota_entries=4,
        global_quota_bytes=8192,
        global_quota_entries=5,
    )
    server = IsolatedWorkerServer(policy)
    alpha = server._ensure_lease(canonical_lease_id("entries-alpha"))
    bravo = server._ensure_lease(canonical_lease_id("entries-bravo"))
    try:
        (alpha.root / "one").mkdir()
        (alpha.root / "two").mkdir()
        (bravo.root / "one").mkdir()
        assert server._global_usage() == (5, 0)

        (bravo.root / "two").mkdir()
        assert server._lease_usage(alpha) == (2, 0)
        assert server._lease_usage(bravo) == (2, 0)
        with pytest.raises(ProtocolError, match="global_quota_exceeded"):
            server._global_usage()
    finally:
        server.close()


def test_failed_new_lease_transaction_removes_root_sidecar_and_cache_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_base = tmp_path / "leases"
    lease_base.mkdir(mode=0o700)
    os.chown(lease_base, os.getuid(), os.getgid())
    os.chmod(lease_base, 0o700)
    server = IsolatedWorkerServer(_worker_policy(lease_base))
    monkeypatch.setattr(server, "_quota_sentinel", lambda: (1, 1, 1, 1))
    lease_id = canonical_lease_id("transactional-create")
    original_touch = server._touch_lease_locked

    def fail_after_sidecar(_lease, _now=None):
        raise ProtocolError("injected_post_mkdir_failure")

    try:
        assert server._global_usage() == (0, 0)
        monkeypatch.setattr(
            server,
            "_touch_lease_locked",
            fail_after_sidecar,
        )
        with pytest.raises(
            ProtocolError,
            match="injected_post_mkdir_failure",
        ):
            server._ensure_lease(lease_id)

        assert not (lease_base / lease_id).exists()
        assert lease_id not in server._leases
        assert server._global_usage_entries == 0
        assert server._global_usage_bytes == 0
        assert not server._accounting_poisoned
        assert not (
            lease_base
            / worker_module._PROOF_PRIVATE_DIR
            / server._proof_state_name(lease_id)
        ).exists()

        monkeypatch.setattr(
            server,
            "_touch_lease_locked",
            original_touch,
        )
        lease = server._ensure_lease(lease_id)
        assert lease.root.is_dir()
        assert server._global_usage_entries == 1
    finally:
        server.close()


def test_dynamic_lease_cap_ttl_quota_and_canonical_ids(tmp_path: Path) -> None:
    lease_base = tmp_path / "leases"
    lease_base.mkdir(mode=0o700)
    os.chown(lease_base, os.getuid(), os.getgid())
    os.chmod(lease_base, 0o700)
    policy = _worker_policy(
        lease_base,
        maximum_active_leases=1,
        lease_ttl_seconds=10,
        lease_quota_bytes=4096,
        lease_quota_entries=4,
    )
    server = IsolatedWorkerServer(policy)
    alpha_id = canonical_lease_id("session-alpha")
    bravo_id = canonical_lease_id("session-bravo")
    try:
        alpha = server._ensure_lease(alpha_id)
        assert alpha.root == lease_base / alpha_id
        assert alpha.root.is_dir()

        with pytest.raises(ProtocolError, match="lease_id_not_canonical"):
            server._ensure_lease("../caller-path")
        with pytest.raises(ProtocolError, match="lease_capacity_exhausted"):
            server._ensure_lease(bravo_id)

        (alpha.root / "oversized").write_bytes(b"x" * 4097)
        with pytest.raises(ProtocolError, match="lease_quota_exceeded"):
            server._lease_usage(alpha)
        (alpha.root / "oversized").unlink()

        removed = server.reap_expired(
            now_monotonic=alpha.last_used_monotonic + policy.lease_ttl_seconds + 1
        )
        assert removed == (alpha_id,)
        assert not alpha.root.exists()
        assert server._ensure_lease(bravo_id).root == lease_base / bravo_id
    finally:
        server.close()


def test_existing_lease_ttl_survives_server_restart(tmp_path: Path) -> None:
    lease_base = tmp_path / "leases"
    lease_base.mkdir(mode=0o700)
    os.chown(lease_base, os.getuid(), os.getgid())
    os.chmod(lease_base, 0o700)
    policy = _worker_policy(lease_base, lease_ttl_seconds=1)
    lease_id = canonical_lease_id("stale-session")
    root = lease_base / lease_id
    root.mkdir(mode=0o700)
    stale = time.time() - 60
    os.utime(root, (stale, stale))

    server = IsolatedWorkerServer(policy)
    try:
        assert server.reap_expired() == (lease_id,)
        assert not root.exists()
    finally:
        server.close()


def test_read_only_bind_is_config_only_and_not_worker_mutable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(worker_module, "HOST_READ_ONLY_ROOT", tmp_path)
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o700)
    os.chown(shared, os.getuid(), os.getgid())
    reference = shared / "reference.txt"
    reference.write_text("sealed", encoding="utf-8")
    os.chown(reference, os.getuid(), os.getgid())
    reference.chmod(0o400)
    shared.chmod(0o500)
    bind = ReadOnlyBind(
        source=shared,
        destination=Path("/opt/hermes-shared/reference"),
        source_uid=os.getuid(),
        source_gid=os.getgid(),
    )
    lease_base = tmp_path / "leases"
    lease_base.mkdir(mode=0o700)
    os.chown(lease_base, os.getuid(), os.getgid())
    os.chmod(lease_base, 0o700)
    with pytest.raises(ValueError, match="read_only_bind_mutable_by_worker"):
        _worker_policy(lease_base, read_only_binds=(bind,))

    nested = tmp_path / "nested" / "reference"
    nested.mkdir(parents=True, mode=0o500)
    with pytest.raises(ValueError, match="read_only_bind_source_namespace_invalid"):
        ReadOnlyBind(
            source=nested,
            destination=Path("/opt/hermes-shared/nested"),
            source_uid=os.getuid(),
            source_gid=os.getgid(),
        )

    forbidden = tmp_path / "skills"
    forbidden.mkdir(mode=0o500)
    with pytest.raises(ValueError, match="read_only_bind_source_forbidden"):
        ReadOnlyBind(
            source=forbidden,
            destination=Path("/opt/hermes-shared/skills"),
            source_uid=os.getuid(),
            source_gid=os.getgid(),
        )
