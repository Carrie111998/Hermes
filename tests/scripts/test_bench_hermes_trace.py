import importlib.util
import os
import sys
from pathlib import Path

import pytest


def load_bench_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "bench_hermes_trace.py"
    spec = importlib.util.spec_from_file_location("bench_hermes_trace", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_write_trace_produces_deterministic_summary(tmp_path: Path):
    bench = load_bench_module()
    trace_path = tmp_path / "trace.jsonl"

    bench.write_trace(trace_path, events=10)
    summary = bench.scan_trace_python(trace_path)

    assert summary == {
        "turns": 1,
        "steps": 6,
        "tool_calls": 2,
        "tool_errors": 1,
        "input_tokens": 45,
        "output_tokens": 20,
    }


def test_time_call_runs_warmups_before_measured_runs():
    bench = load_bench_module()
    calls = []

    def action():
        calls.append(len(calls))

    timings = bench.time_call(action, runs=3, warmups=2)

    assert len(timings) == 3
    assert calls == [0, 1, 2, 3, 4]


def test_add_rust_command_timings_records_each_command(tmp_path: Path):
    bench = load_bench_module()
    result = {}
    calls = []

    def runner(binary: Path, command: str, trace_path: Path):
        assert binary == tmp_path / "hermes-trace"
        assert trace_path == tmp_path / "trace.jsonl"
        calls.append(command)

    bench.add_rust_command_timings(
        result,
        tmp_path / "hermes-trace",
        tmp_path / "trace.jsonl",
        runs=2,
        warmups=1,
        runner=runner,
    )

    assert calls == [
        "verify",
        "verify",
        "verify",
        "summary",
        "summary",
        "summary",
        "digest",
        "digest",
        "digest",
    ]
    assert result["rust_verify_ms_median"] >= 0
    assert result["rust_summary_ms_median"] >= 0
    assert result["rust_digest_ms_median"] >= 0
    assert len(result["rust_verify_ms_runs"]) == 2
    assert len(result["rust_summary_ms_runs"]) == 2
    assert len(result["rust_digest_ms_runs"]) == 2


def test_snapshot_digest_python_matches_rust_digest_semantics_for_json_edge_cases(
    tmp_path: Path,
):
    bench = load_bench_module()
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        "\n".join(
            [
                '{"schema_version":1,"seq":1,"time":1.0,"session_id":"s",'
                '"type":"turn/start","turn":null,"step":null,'
                '"data":{"z":"é","a":null,"nested":{"b":2,"a":1}}}',
                '{"schema_version":1,"seq":2,"time":2.0,"session_id":"s",'
                '"type":"step/start","data":null}',
                '{"schema_version":1,"seq":3,"time":3.0,"session_id":"s",'
                '"type":"assistant/message"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert (
        bench.snapshot_digest_python(trace)
        == "606da8b6c413f0563e01ee3202c93f5cae1f8ffbcc270da7e54e5dd0662261a3"
    )


def test_snapshot_digest_python_rejects_float_data_instead_of_hashing_wrong_bytes(
    tmp_path: Path,
):
    bench = load_bench_module()
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        '{"schema_version":1,"seq":1,"time":1.0,"session_id":"s",'
        '"type":"turn/start","data":{"v":1e-06}}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="float JSON values"):
        bench.snapshot_digest_python(trace)


def test_main_fails_when_required_binary_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    bench = load_bench_module()
    missing = tmp_path / "missing-hermes-trace"
    trace = tmp_path / "trace.jsonl"
    trace.write_text("keep me\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "bench_hermes_trace.py",
            "--events",
            "1",
            "--runs",
            "1",
            "--warmups",
            "0",
            "--trace",
            str(trace),
            "--binary",
            str(missing),
            "--require-binary",
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        bench.main()

    assert excinfo.value.code == 2
    assert "required binary not found" in capsys.readouterr().err
    assert trace.read_text(encoding="utf-8") == "keep me\n"


def test_main_fails_when_required_binary_is_not_a_file_before_trace_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    bench = load_bench_module()
    binary_dir = tmp_path / "hermes-trace-dir"
    binary_dir.mkdir()
    trace = tmp_path / "trace.jsonl"
    trace.write_text("keep me\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "bench_hermes_trace.py",
            "--events",
            "1",
            "--runs",
            "1",
            "--warmups",
            "0",
            "--trace",
            str(trace),
            "--binary",
            str(binary_dir),
            "--require-binary",
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        bench.main()

    assert excinfo.value.code == 2
    assert "required binary is not a file" in capsys.readouterr().err
    assert trace.read_text(encoding="utf-8") == "keep me\n"


def test_main_fails_when_required_binary_is_not_runnable_before_trace_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    bench = load_bench_module()
    suffix = ".exe" if os.name == "nt" else ""
    invalid_binary = tmp_path / f"not-hermes-trace{suffix}"
    invalid_binary.write_text("not an executable\n", encoding="utf-8")
    invalid_binary.chmod(0o755)
    trace = tmp_path / "trace.jsonl"
    trace.write_text("keep me\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "bench_hermes_trace.py",
            "--events",
            "1",
            "--runs",
            "1",
            "--warmups",
            "0",
            "--trace",
            str(trace),
            "--binary",
            str(invalid_binary),
            "--require-binary",
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        bench.main()

    assert excinfo.value.code == 2
    assert "required binary is not runnable" in capsys.readouterr().err
    assert trace.read_text(encoding="utf-8") == "keep me\n"


def test_main_fails_when_required_binary_is_not_hermes_trace_before_trace_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    bench = load_bench_module()
    trace = tmp_path / "trace.jsonl"
    trace.write_text("keep me\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "bench_hermes_trace.py",
            "--events",
            "1",
            "--runs",
            "1",
            "--warmups",
            "0",
            "--trace",
            str(trace),
            "--binary",
            sys.executable,
            "--require-binary",
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        bench.main()

    assert excinfo.value.code == 2
    assert "required binary failed hermes-trace preflight" in capsys.readouterr().err
    assert trace.read_text(encoding="utf-8") == "keep me\n"


def test_main_fails_when_required_binary_returns_zero_with_wrong_output_before_trace_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    bench = load_bench_module()
    trace = tmp_path / "trace.jsonl"
    trace.write_text("keep me\n", encoding="utf-8")
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return bench.subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(bench.subprocess, "run", fake_run)
    monkeypatch.setattr(
        "sys.argv",
        [
            "bench_hermes_trace.py",
            "--events",
            "1",
            "--runs",
            "1",
            "--warmups",
            "0",
            "--trace",
            str(trace),
            "--binary",
            sys.executable,
            "--require-binary",
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        bench.main()

    assert excinfo.value.code == 2
    assert "required binary failed hermes-trace preflight (verify)" in capsys.readouterr().err
    assert calls == [[sys.executable, "verify", calls[0][2]]]
    assert trace.read_text(encoding="utf-8") == "keep me\n"
