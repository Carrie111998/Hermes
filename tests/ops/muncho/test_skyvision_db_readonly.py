from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from gateway.operational_edge_catalog import (
    build_operation_argv,
    operation_catalog,
)
from gateway.operational_edge_protocol import OperationalAccess
from ops.muncho.runtime import skyvision_db_readonly as db


def test_model_selects_exact_normal_or_sensitive_operation() -> None:
    catalog = operation_catalog()
    normal = catalog["skyvision.db.query"]
    sensitive = catalog["skyvision.db.query_sensitive"]

    assert normal.access is OperationalAccess.READ
    assert normal.argv_prefix == ("--sensitivity", "normal")
    assert sensitive.access is OperationalAccess.MUTATION
    assert sensitive.argv_prefix == ("--sensitivity", "sensitive")
    assert sensitive.minimum_operator_tier == "standard"
    assert {item.name for item in normal.arguments}.isdisjoint({
        "sensitivity",
        "step_up_scope",
        "step_up_request_id",
    })


def test_argument_values_never_change_the_selected_sensitivity() -> None:
    normal = operation_catalog()["skyvision.db.query"]
    arguments = {
        "db": "skyvisio_fp",
        "query": "SELECT email, phone FROM orders_new LIMIT 1",
        "case_id": "case:operator-report",
        "requester": "Ivs",
        "requester_id": "1391703330711142472",
        "purpose": "Client revenue and payment report",
    }

    argv = build_operation_argv(normal, arguments)

    assert argv[:2] == ("--sensitivity", "normal")
    assert "sensitive" not in argv


def test_redaction_applies_only_to_exact_model_authored_fields() -> None:
    stdout = "email\tphone\tnote\nuser@example.com\t+3591\tvisible\n"

    headers, rows, redactions, truncated = db.parse_tsv(
        stdout,
        10,
        ("phone",),
    )

    assert headers == ["email", "phone", "note"]
    assert rows == [
        {
            "email": "user@example.com",
            "phone": "[REDACTED]",
            "note": "visible",
        }
    ]
    assert redactions == 1
    assert truncated is False


def test_column_names_do_not_trigger_implicit_redaction() -> None:
    stdout = "password\ttoken\temail\nvalue-a\tvalue-b\tvalue-c\n"

    _, rows, redactions, _ = db.parse_tsv(stdout, 10, ())

    assert rows == [{"password": "value-a", "token": "value-b", "email": "value-c"}]
    assert redactions == 0


def test_sql_boundary_is_read_only_and_bounded() -> None:
    assert (
        db.validate_query(
            "SELECT id FROM orders_new LIMIT 2",
            "bounded_rows",
            2,
        )
        == "SELECT id FROM orders_new LIMIT 2"
    )

    with pytest.raises(SystemExit):
        db.validate_query("UPDATE orders_new SET id=1", "bounded_rows", 2)
    with pytest.raises(SystemExit):
        db.validate_query("SELECT id FROM orders_new", "bounded_rows", 2)


def test_readiness_probe_is_valid_without_a_row_limit() -> None:
    probe = operation_catalog()["skyvision.db.probe"]
    assert probe.argv_prefix[-2:] == ("--expected-result-shape", "scalar")
    assert (
        db.validate_query(
            "SELECT 1 AS operational_edge_ready",
            "scalar",
            1,
        )
        == "SELECT 1 AS operational_edge_ready"
    )


def test_unknown_model_redaction_field_fails_closed() -> None:
    with pytest.raises(SystemExit):
        db.parse_tsv("id\n1\n", 10, ("not_present",))


def test_database_password_never_appears_in_ssh_process_argv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    helper = tmp_path / "ssh-helper"
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    secret = tmp_path / "db.env"
    secret.write_text(
        "SKYVISION_DB_READONLY_FP_PASSWORD=secret-value\n"
        "SKYVISION_DB_READONLY_FP_USER=reader\n"
        "SKYVISION_DB_READONLY_FP_DB=skyvisio_fp\n"
        f"SKYVISION_DB_READONLY_FP_SSH_HELPER={helper}\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(db.DB_CONFIGS["skyvisio_fp"], "secret_file", secret)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "value\n1\n", "")

    monkeypatch.setattr(db, "_run_bounded_process", run)
    db.run_mysql_over_ssh(
        "SELECT 1 AS value",
        SimpleNamespace(db="skyvisio_fp", timeout_seconds=10),
    )

    assert calls[0][0] == [str(helper), "sh -s"]
    assert "secret-value" not in " ".join(calls[0][0])
    assert "secret-value" in calls[0][1]["input_text"]


def test_subprocess_output_is_stopped_at_the_exact_byte_bound() -> None:
    with pytest.raises(db.ProcessOutputBoundExceeded) as captured:
        db._run_bounded_process(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'x' * 2048)",
            ],
            input_text="",
            timeout_seconds=5,
            stdout_limit=1024,
        )

    assert captured.value.stream == "stdout"
    assert captured.value.maximum == 1024


def test_mysql_failure_exposes_only_bounded_stderr_metadata(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = SimpleNamespace(
        db="skyvisio_fp",
        query="SELECT 1 AS value",
        case_id="case:failure",
        requester="Nassi",
        requester_id="trusted-user",
        purpose="Read one value",
        expected_result_shape="scalar",
        max_rows=1,
        timeout_seconds=10,
        sensitivity="normal",
        redact_field=[],
    )
    secret_stderr = "mysql failed near customer-secret@example.com"
    monkeypatch.setattr(db, "parser", lambda: SimpleNamespace(parse_args=lambda: args))
    monkeypatch.setattr(
        db,
        "run_mysql_over_ssh",
        lambda _query, _args: (
            subprocess.CompletedProcess([], 1, "", secret_stderr),
            1,
        ),
    )

    with pytest.raises(SystemExit):
        db.main()

    emitted = capsys.readouterr().out
    assert secret_stderr not in emitted
    payload = db.json.loads(emitted)
    assert payload["reason"] == "mysql_query_failed"
    assert payload["stderr_bytes"] == len(secret_stderr.encode("utf-8"))
    assert len(payload["stderr_sha256"]) == 64
