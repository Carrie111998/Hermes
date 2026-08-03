import io
import json
import os
import stat
import sys

import pytest

from tui_gateway import server


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX owner/mode and symlink hardening contract",
)


def _all_log_payloads(path):
    return "".join(
        candidate.read_text(encoding="utf-8")
        for candidate in sorted(path.parent.glob(path.name + "*"))
        if candidate.is_file()
    )


def test_private_crash_log_rejects_symlink_and_repairs_modes(tmp_path):
    from tui_gateway.crash_log import append_crash_record

    log_dir = tmp_path / "logs"
    log_dir.mkdir(mode=0o755)
    target = tmp_path / "external.txt"
    target.write_text("sentinel", encoding="utf-8")
    crash_path = log_dir / "tui_gateway_crash.log"
    crash_path.symlink_to(target)

    assert not append_crash_record(crash_path, "panic", "private detail")
    assert target.read_text(encoding="utf-8") == "sentinel"
    assert crash_path.is_symlink()

    crash_path.unlink()
    assert append_crash_record(crash_path, "panic", "safe detail")
    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(crash_path.stat().st_mode) == 0o600
    assert crash_path.stat().st_uid == os.geteuid()
    assert stat.S_ISREG(crash_path.stat().st_mode)
    assert crash_path.stat().st_nlink == 1


def test_crash_log_redacts_rotates_caps_and_privatises_backups(tmp_path):
    from tui_gateway.crash_log import append_crash_record

    crash_path = tmp_path / "logs" / "tui_gateway_crash.log"
    secret = "sk-" + "A" * 48

    for index in range(8):
        assert append_crash_record(
            crash_path,
            "thread exception",
            f"record={index} {secret}",
            max_bytes=320,
            backup_count=2,
        )

    files = sorted(crash_path.parent.glob(crash_path.name + "*"))
    assert 1 <= len(files) <= 3
    assert secret not in _all_log_payloads(crash_path)
    assert all(path.stat().st_size <= 320 for path in files)
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files)


def test_crash_log_redaction_failure_omits_sensitive_detail(monkeypatch, tmp_path):
    from tui_gateway import crash_log

    crash_path = tmp_path / "logs" / "tui_gateway_crash.log"
    secret = "token=" + "sensitive-runtime-value"
    monkeypatch.setattr(
        crash_log,
        "redact_sensitive_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("redactor down")),
    )

    assert crash_log.append_crash_record(crash_path, "panic", secret)
    payload = crash_path.read_text(encoding="utf-8")
    assert secret not in payload
    assert "detail omitted" in payload


def test_panic_hooks_do_not_echo_exception_values_or_thread_names(
    monkeypatch,
    capsys,
):
    records = []
    monkeypatch.setattr(
        server,
        "_append_crash_record",
        lambda path, kind, detail="", **kwargs: records.append((path, kind, detail)) or True,
    )
    # The default hook prints the exception value; calling it would defeat the
    # safe writer even if the file itself were redacted.
    monkeypatch.setattr(
        sys,
        "__excepthook__",
        lambda *_args: pytest.fail("unsafe default excepthook was chained"),
    )
    secret = "password=" + "runtime-only-value"
    exc = RuntimeError(secret)

    server._panic_hook(RuntimeError, exc, None)
    thread_args = type(
        "ThreadArgs",
        (),
        {
            "exc_type": RuntimeError,
            "exc_value": exc,
            "exc_traceback": None,
            "thread": type("Thread", (), {"name": "customer-session-name"})(),
        },
    )()
    server._thread_panic_hook(thread_args)

    stderr = capsys.readouterr().err
    assert secret not in stderr
    assert "customer-session-name" not in stderr
    assert stderr.count("RuntimeError") == 2
    assert len(records) == 2
    assert secret not in records[0][2]


def test_exit_log_never_persists_raw_reason_or_unlisted_rpc_method(
    monkeypatch,
    tmp_path,
    capsys,
):
    from tui_gateway import entry

    crash_path = tmp_path / "logs" / "tui_gateway_crash.log"
    raw_reason = "private RPC prose 8f14c6"
    raw_method = "private.rpc.method.8f14c6"
    monkeypatch.setattr(entry, "_CRASH_LOG", str(crash_path))

    entry._log_exit(raw_reason)

    write_results = iter((True, False))
    monkeypatch.setattr(entry, "_install_sidecar_publisher", lambda: None)
    monkeypatch.setattr(entry, "resolve_skin", lambda: "default")
    monkeypatch.setattr(entry, "write_json", lambda _payload: next(write_results))
    monkeypatch.setattr(
        entry,
        "dispatch",
        lambda _request: {"jsonrpc": "2.0", "id": 1, "result": {}},
    )
    monkeypatch.setattr(
        "hermes_cli.config.read_raw_config",
        lambda: {},
    )
    monkeypatch.setattr(
        entry.sys,
        "stdin",
        io.StringIO(json.dumps({"id": 1, "method": raw_method}) + "\n"),
    )

    with pytest.raises(SystemExit) as exc_info:
        entry.main()

    assert exc_info.value.code == 0
    payload = crash_path.read_text(encoding="utf-8")
    stderr = capsys.readouterr().err
    for raw_value in (raw_reason, raw_method):
        assert raw_value not in payload
        assert raw_value not in stderr
    assert "code=unclassified" in payload
    assert "code=response_write_failed" in payload
    assert "code=unclassified" in stderr
    assert "code=response_write_failed" in stderr
    assert "method=" not in payload
    assert "method=" not in stderr


def test_exit_log_keeps_canonical_method_from_strict_allowlist(
    monkeypatch,
    tmp_path,
    capsys,
):
    from tui_gateway import entry

    crash_path = tmp_path / "logs" / "tui_gateway_crash.log"
    monkeypatch.setattr(entry, "_CRASH_LOG", str(crash_path))

    entry._log_exit("response_write_failed", method="session.create")

    payload = crash_path.read_text(encoding="utf-8")
    stderr = capsys.readouterr().err
    assert "code=response_write_failed method=session.create" in payload
    assert "code=response_write_failed method=session.create" in stderr
