from __future__ import annotations

import asyncio
import builtins
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_cli.image_provenance as image_provenance
import hermes_cli.update_receipt as receipts
from hermes_cli.update_contract import IMAGE_MANAGED_UPDATE_REFUSED


@pytest.fixture(autouse=True)
def _reset_image_bootstrap_observations(monkeypatch):
    from hermes_cli import _early_recovery as early_recovery

    monkeypatch.setattr(early_recovery, "_IMAGE_MANAGED_RUNTIME_OBSERVED", False)
    monkeypatch.setattr(
        early_recovery,
        "_IMAGE_MANAGED_UPDATE_BOOTSTRAP_OBSERVED",
        False,
    )


def _write_marker(path, *, malformed: bool = False):
    if malformed:
        path.write_text("{not-json", encoding="utf-8")
    else:
        path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "deployment_kind": "image",
                    "manager": "docker",
                    "image": "nousresearch/hermes-agent",
                    "version": "0.20.5",
                    "revision": "f" * 40,
                }
            ),
            encoding="utf-8",
        )
    return path


def _boom(name):
    def fail(*_args, **_kwargs):
        raise AssertionError(f"{name} must not run before image refusal")

    return fail


def _isolated_subprocess_env(hermes_home: Path) -> dict[str, str]:
    """Run process-level assertions against this checkout, not a stale edit."""

    return {
        **os.environ,
        "HERMES_HOME": str(hermes_home),
        "HERMES_NONINTERACTIVE": "1",
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
    }


@pytest.mark.parametrize("malformed", [False, True])
def test_import_time_recovery_never_mutates_an_image(
    monkeypatch, tmp_path, malformed
):
    from hermes_cli import _early_recovery as early_recovery

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nversion = "1"\n', encoding="utf-8"
    )
    incomplete = checkout / ".update-incomplete"
    incomplete.write_text("pid=0\n", encoding="utf-8")
    marker = _write_marker(tmp_path / "image-provenance.json", malformed=malformed)
    monkeypatch.setattr(image_provenance, "IMAGE_PROVENANCE_PATH", marker)
    monkeypatch.setattr(
        early_recovery,
        "_complete_pending_core_install",
        _boom("early dependency repair"),
    )

    early_recovery.recover_if_needed(
        project_root=checkout,
        argv=["update"],
    )

    assert incomplete.is_file()
    assert not (checkout / ".update-incomplete.lock").exists()


def test_early_image_observation_stays_positive_across_present_absent_race(
    monkeypatch, tmp_path
):
    from hermes_cli import _early_recovery as early_recovery

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nversion = "1"\n', encoding="utf-8"
    )
    incomplete = checkout / ".update-incomplete"
    incomplete.write_text("pid=0\n", encoding="utf-8")
    marker = _write_marker(tmp_path / "image-provenance.json")
    monkeypatch.setattr(image_provenance, "IMAGE_PROVENANCE_PATH", marker)
    calls = []
    monkeypatch.setattr(
        early_recovery,
        "_complete_pending_core_install",
        lambda *args: calls.append(args) or True,
    )

    early_recovery.recover_if_needed(project_root=checkout, argv=["doctor"])
    marker.unlink()
    early_recovery.recover_if_needed(project_root=checkout, argv=["doctor"])

    assert early_recovery._IMAGE_MANAGED_RUNTIME_OBSERVED is True
    assert calls == []
    assert incomplete.is_file()


def test_import_time_recovery_is_unchanged_when_marker_is_absent(
    monkeypatch, tmp_path
):
    from hermes_cli import _early_recovery as early_recovery

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nversion = "1"\n', encoding="utf-8"
    )
    incomplete = checkout / ".update-incomplete"
    incomplete.write_text("pid=0\n", encoding="utf-8")
    monkeypatch.setattr(
        image_provenance,
        "IMAGE_PROVENANCE_PATH",
        tmp_path / "missing-provenance.json",
    )
    calls = []
    monkeypatch.setattr(
        early_recovery,
        "_complete_pending_core_install",
        lambda root, marker: calls.append((root, marker)) or True,
    )

    early_recovery.recover_if_needed(
        project_root=checkout,
        argv=["update"],
    )

    assert calls == [(checkout, incomplete)]


def test_early_recovery_stays_available_when_new_provenance_module_is_missing(
    monkeypatch, tmp_path
):
    from hermes_cli import _early_recovery as early_recovery

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nversion = "1"\n', encoding="utf-8"
    )
    incomplete = checkout / ".update-incomplete"
    incomplete.write_text("pid=0\n", encoding="utf-8")
    monkeypatch.setattr(
        image_provenance,
        "IMAGE_PROVENANCE_PATH",
        tmp_path / "missing-provenance.json",
    )
    calls = []
    monkeypatch.setattr(
        early_recovery,
        "_complete_pending_core_install",
        lambda root, pending: calls.append((root, pending)) or True,
    )
    real_import = builtins.__import__

    def _without_new_helper(name, *args, **kwargs):
        if name == "hermes_cli.image_provenance":
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _without_new_helper)

    early_recovery.recover_if_needed(project_root=checkout, argv=["update"])

    assert calls == [(checkout, incomplete)]


def test_cmd_update_preserves_legacy_path_when_provenance_helper_is_missing(
    monkeypatch, tmp_path
):
    import hermes_cli.config as config
    import hermes_cli.main as main

    calls = []
    monkeypatch.setattr(
        main._early_recovery_mod,
        "_image_provenance_marker_path",
        lambda: tmp_path / "missing-provenance.json",
    )
    monkeypatch.setattr(config, "is_managed", lambda: False)
    monkeypatch.setattr(config, "detect_install_method", lambda _root: "git")
    monkeypatch.setattr(
        main,
        "_self",
        lambda: SimpleNamespace(
            _cmd_update_check=lambda **kwargs: calls.append(kwargs)
        ),
    )
    real_import = builtins.__import__

    def _without_new_helper(name, *args, **kwargs):
        if name == "hermes_cli.image_provenance":
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _without_new_helper)

    main.cmd_update(
        SimpleNamespace(plan=False, check=True, branch="release", gateway=False)
    )

    assert calls == [{"branch": "release", "branch_explicit": True}]


def test_earliest_positive_image_observation_is_sticky_if_marker_disappears(
    monkeypatch
):
    from hermes_cli import _early_recovery as early_recovery
    import hermes_cli.main as main

    monkeypatch.setattr(early_recovery, "_IMAGE_MANAGED_RUNTIME_OBSERVED", True)
    monkeypatch.setattr(image_provenance, "read_image_provenance", lambda: None)

    provenance = main._read_bootstrap_image_provenance()

    assert provenance is not None
    assert provenance.valid is False
    assert provenance.deployment_kind == "image"
    assert provenance.error == "marker_disappeared_after_early_startup_probe"


def test_early_cli_recognizer_refuses_and_persists_before_dispatch(
    monkeypatch, tmp_path, capsys
):
    import hermes_cli.main as main

    marker = _write_marker(tmp_path / "image-provenance.json")
    monkeypatch.setattr(image_provenance, "IMAGE_PROVENANCE_PATH", marker)
    monkeypatch.setattr(receipts, "_receipt_dir", lambda: tmp_path / "receipts")
    receipts._current = None

    with pytest.raises(SystemExit) as exc:
        main._early_image_managed_update_gate(["update"])

    assert exc.value.code == 2
    assert "image-managed" in capsys.readouterr().out
    payload = json.loads(
        (tmp_path / "receipts" / "latest.json").read_text(encoding="utf-8")
    )
    assert payload["outcome"] == "refused"
    assert payload["refusal"]["code"] == IMAGE_MANAGED_UPDATE_REFUSED


def test_cmd_update_marker_outranks_managed_gate_io_lock_and_subprocess(
    monkeypatch, tmp_path, capsys
):
    import hermes_cli.config as config
    import hermes_cli.main as main
    from hermes_cli.update_lock import UpdateLock

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    marker = _write_marker(tmp_path / "image-provenance.json")
    monkeypatch.setattr(image_provenance, "IMAGE_PROVENANCE_PATH", marker)
    monkeypatch.setattr(main, "PROJECT_ROOT", checkout)
    monkeypatch.setattr(config, "is_managed", _boom("managed-runtime gate"))
    monkeypatch.setattr(main, "_install_hangup_protection", _boom("I/O setup"))
    monkeypatch.setattr(UpdateLock, "acquire", _boom("update lock"))
    monkeypatch.setattr(main, "_run_pre_update_backup", _boom("backup"))
    monkeypatch.setattr("subprocess.run", _boom("subprocess"))
    monkeypatch.setattr(receipts, "_receipt_dir", lambda: tmp_path / "receipts")
    receipts._current = None

    with pytest.raises(SystemExit) as exc:
        main.cmd_update(
            SimpleNamespace(
                plan=False,
                check=False,
                branch=None,
                gateway=False,
            )
        )

    assert exc.value.code == 2
    assert "image-managed" in capsys.readouterr().out


@pytest.mark.parametrize("boundary", ["bootstrap", "command"])
def test_positive_marker_observation_cannot_disappear_on_second_read(
    monkeypatch, tmp_path, boundary
):
    import hermes_cli.main as main

    marker = _write_marker(tmp_path / "image-provenance.json")
    observed = image_provenance.read_image_provenance(marker)
    assert observed is not None
    reads = []

    def _read_once(*_args, **_kwargs):
        reads.append(True)
        if len(reads) > 1:
            raise AssertionError("image admission re-read the marker")
        return observed

    monkeypatch.setattr(image_provenance, "read_image_provenance", _read_once)
    monkeypatch.setattr(receipts, "_receipt_dir", lambda: tmp_path / "receipts")
    receipts._current = None

    with pytest.raises(SystemExit) as exc:
        if boundary == "bootstrap":
            main._early_image_managed_update_gate(["update"])
        else:
            main.cmd_update(
                SimpleNamespace(
                    plan=False,
                    check=False,
                    branch=None,
                    gateway=False,
                )
            )

    assert exc.value.code == 2
    assert len(reads) == 1


@pytest.mark.parametrize(("mode", "expected_exit"), [("--plan", 0), ("--check", 2)])
def test_bootstrap_read_modes_reuse_positive_observation_if_marker_disappears(
    monkeypatch, tmp_path, mode, expected_exit
):
    import hermes_cli.main as main
    import hermes_cli.update_inventory as inventory

    marker = _write_marker(tmp_path / "image-provenance.json")
    observed = image_provenance.read_image_provenance(marker)
    assert observed is not None
    reads = []

    def _disappeared(*_args, **_kwargs):
        reads.append(True)
        return None

    real_collect = inventory.collect_runtime_inventory
    collected = []

    def _collect(*args, **kwargs):
        collected.append(kwargs.get("_known_image_provenance"))
        assert kwargs.get("_known_image_provenance") is observed
        return real_collect(*args, **kwargs)

    monkeypatch.setattr(image_provenance, "read_image_provenance", _disappeared)
    monkeypatch.setattr(inventory, "collect_runtime_inventory", _collect)
    monkeypatch.setattr(receipts, "_receipt_dir", lambda: tmp_path / "receipts")
    receipts._current = None

    with pytest.raises(SystemExit) as exc:
        main._early_image_managed_update_gate(
            ["update", mode],
            _known_image_provenance=observed,
        )

    assert exc.value.code == expected_exit
    assert collected == [observed]
    assert reads == []
    assert not (tmp_path / "receipts").exists()


def test_image_cli_check_matches_read_only_api_and_creates_no_receipt(
    monkeypatch, tmp_path, capsys
):
    pytest.importorskip("fastapi")
    import hermes_cli.main as main
    import hermes_cli.web_server as web_server

    marker = _write_marker(tmp_path / "image-provenance.json")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(image_provenance, "IMAGE_PROVENANCE_PATH", marker)
    monkeypatch.setattr(main, "PROJECT_ROOT", checkout)
    monkeypatch.setattr(web_server, "PROJECT_ROOT", checkout)
    monkeypatch.setattr(receipts, "_receipt_dir", lambda: tmp_path / "receipts")
    receipts._current = None

    with pytest.raises(SystemExit) as exc:
        main.cmd_update(
            SimpleNamespace(
                plan=False,
                check=True,
                branch="release",
                gateway=False,
            )
        )
    cli_message = capsys.readouterr().out.rstrip()
    api_result = asyncio.run(web_server.check_hermes_update(force=True))

    assert exc.value.code == 2
    assert api_result["error"] == IMAGE_MANAGED_UPDATE_REFUSED
    assert api_result["message"] == cli_message
    assert api_result["update_command"] in cli_message
    assert not (tmp_path / "receipts").exists()


def test_image_update_plan_is_read_only_and_creates_no_refusal_receipt(
    monkeypatch, tmp_path
):
    import hermes_cli.config as config
    import hermes_cli.main as main
    import hermes_cli.update_contract as contract
    import hermes_cli.update_inventory as inventory

    marker = _write_marker(tmp_path / "image-provenance.json")
    monkeypatch.setattr(image_provenance, "IMAGE_PROVENANCE_PATH", marker)
    monkeypatch.setattr(receipts, "_receipt_dir", lambda: tmp_path / "receipts")
    receipts._current = None
    sentinel = object()
    printed = []
    monkeypatch.setattr(
        inventory,
        "collect_runtime_inventory",
        lambda *args, **kwargs: sentinel,
    )
    monkeypatch.setattr(inventory, "print_update_plan", printed.append)
    monkeypatch.setattr(contract, "perform_update", _boom("refusal persistence"))
    monkeypatch.setattr(config, "is_managed", _boom("managed-runtime gate"))

    main.cmd_update(SimpleNamespace(plan=True))

    assert printed == [sentinel]
    assert not (tmp_path / "receipts").exists()


@pytest.mark.parametrize("reasoning_flag", ["--reasoning", "--reas"])
def test_stdlib_bootstrap_detector_finds_update_after_global_value_flags(
    reasoning_flag,
):
    from hermes_cli import _early_recovery as early_recovery
    import hermes_cli.main as main

    assert early_recovery._BOOTSTRAP_VALUE_FLAGS == (
        main._TOP_LEVEL_VALUE_FLAGS | {"-p", "--profile"}
    )
    assert early_recovery._image_update_invocation(
        [reasoning_flag, "high", "update", "--check"]
    ) is True


def test_stdlib_bootstrap_long_options_stay_in_sync_with_argparse():
    from hermes_cli import _early_recovery as early_recovery
    from hermes_cli._parser import build_top_level_parser

    parser, _subparsers, _chat_parser = build_top_level_parser()
    parser_long_options = {
        option
        for action in parser._actions
        for option in action.option_strings
        if option.startswith("--")
    }

    assert early_recovery._BOOTSTRAP_ARGPARSE_LONG_FLAGS == parser_long_options


@pytest.mark.parametrize(
    "argv",
    [
        ["--mod", "Hermes-4", "update"],
        ["--prov", "nous", "update"],
        ["--reas", "high", "update"],
        ["--tool", "terminal", "update"],
        ["--res", "latest", "update"],
        ["--ski", "github", "update"],
        ["--usage", "usage.json", "update"],
    ],
)
def test_stdlib_bootstrap_detector_matches_argparse_long_abbreviations(argv):
    from hermes_cli import _early_recovery as early_recovery
    from hermes_cli.subcommands.update import parse_update_bootstrap_command_or_exit

    parsed = parse_update_bootstrap_command_or_exit(argv)

    assert parsed.command == "update"
    assert early_recovery._image_update_invocation(argv) is True


@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize("reasoning_flag", ["--reasoning", "--reas"])
@pytest.mark.parametrize(("mode", "expected_exit"), [("--plan", 0), ("--check", 2)])
def test_image_read_only_update_modes_skip_import_time_mutation(
    tmp_path, mode, expected_exit, reasoning_flag
):
    marker = _write_marker(tmp_path / "image-provenance.json")
    hermes_home = tmp_path / "home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text("{malformed", encoding="utf-8")
    script = """
import pathlib
import sys

import hermes_cli.image_provenance as image_provenance

image_provenance.IMAGE_PROVENANCE_PATH = pathlib.Path(sys.argv[1])
mode = sys.argv[2]
expected_exit = int(sys.argv[3])
sys.argv = ["hermes", sys.argv[4], "high", "update", mode]
try:
    import hermes_cli.main
except SystemExit as exc:
    if exc.code != expected_exit:
        raise
else:
    raise AssertionError("image update did not terminate at the import boundary")
print("READ_ONLY_OK")
"""
    env = _isolated_subprocess_env(hermes_home)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(marker),
            mode,
            str(expected_exit),
            reasoning_flag,
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "READ_ONLY_OK" in result.stdout
    assert not (hermes_home / "logs" / "update_receipts").exists()
    assert not list(hermes_home.glob("config.yaml.corrupt.*.bak"))
    assert not list(hermes_home.glob("*.corrupt.*.bak"))


def test_marker_absence_preserves_managed_plan_refusal(monkeypatch, tmp_path):
    import hermes_cli.config as config
    import hermes_cli.main as main
    import hermes_cli.update_inventory as inventory

    monkeypatch.setattr(
        image_provenance,
        "IMAGE_PROVENANCE_PATH",
        tmp_path / "missing-provenance.json",
    )
    monkeypatch.setattr(config, "is_managed", lambda: True)
    managed_errors = []
    monkeypatch.setattr(config, "managed_error", managed_errors.append)
    monkeypatch.setattr(
        inventory,
        "collect_runtime_inventory",
        _boom("managed update plan"),
    )

    main.cmd_update(SimpleNamespace(plan=True))

    assert managed_errors == ["update Hermes Agent"]


def test_dashboard_apply_check_and_durable_status_share_typed_refusal(
    monkeypatch, tmp_path
):
    pytest.importorskip("fastapi")
    import hermes_cli.banner as banner
    import hermes_cli.web_server as web_server

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    marker = _write_marker(tmp_path / "image-provenance.json")
    action_log_dir = tmp_path / "action-logs"
    action_log_dir.mkdir()
    old_action_id = "a" * 32
    durable_log = action_log_dir / "update.log"
    durable_log.write_text(
        "=== hermes update started 2024-01-01T00:00:00 ===\n"
        "old update complete\n"
        f"=== hermes-update completed {old_action_id} ===\n",
        encoding="utf-8",
    )
    os.utime(durable_log, (1_704_067_200, 1_704_067_200))
    monkeypatch.setattr(image_provenance, "IMAGE_PROVENANCE_PATH", marker)
    monkeypatch.setattr(web_server, "PROJECT_ROOT", checkout)
    monkeypatch.setattr(web_server, "_ACTION_LOG_DIR", action_log_dir)
    monkeypatch.setattr(receipts, "_receipt_dir", lambda: tmp_path / "receipts")
    monkeypatch.setattr(
        web_server,
        "_dashboard_local_update_managed_externally",
        _boom("generic dashboard gate"),
    )
    monkeypatch.setattr(web_server, "_spawn_hermes_action", _boom("update spawn"))
    monkeypatch.setattr(banner, "check_for_updates", _boom("network update check"))
    web_server._ACTION_PROCS.pop("hermes-update", None)
    web_server._ACTION_RESULTS.pop("hermes-update", None)
    web_server._ACTION_IDS.pop("hermes-update", None)
    receipts._current = None

    applied = asyncio.run(web_server.update_hermes())
    checked = asyncio.run(web_server.check_hermes_update(force=True))

    assert applied["ok"] is False
    assert applied["error"] == IMAGE_MANAGED_UPDATE_REFUSED
    assert applied["reason"] == IMAGE_MANAGED_UPDATE_REFUSED
    assert applied["deployment_kind"] == "image"
    assert applied["action_id"] == applied["correlation_id"]
    assert applied["receipt_path"]
    assert checked["error"] == IMAGE_MANAGED_UPDATE_REFUSED
    assert checked["reason"] == IMAGE_MANAGED_UPDATE_REFUSED
    assert checked["message"] == applied["message"]
    assert checked["update_command"] == applied["update_command"]

    # Simulate a dashboard restart: in-memory registries disappear, while the
    # exact action-log generation marker written with the typed refusal stays
    # durable.  It deterministically supersedes the older update.log success;
    # no timestamp/mtime arbitration is involved.
    web_server._ACTION_PROCS.clear()
    web_server._ACTION_RESULTS.clear()
    web_server._ACTION_IDS.clear()
    status = asyncio.run(web_server.get_action_status("hermes-update"))

    assert status["running"] is False
    assert status["exit_code"] == 2
    assert status["action_id"] == applied["correlation_id"]
    assert status["action_id"] != old_action_id
    assert status["receipt"]["outcome"] == "refused"
    assert status["receipt"]["correlation_id"] == applied["correlation_id"]
    assert status["receipt"]["refusal"]["code"] == IMAGE_MANAGED_UPDATE_REFUSED
    assert status["receipt"]["refusal"]["message"] == applied["message"]


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["chat"],
        ["gateway", "run"],
        ["dashboard"],
        ["doctor"],
        ["skills", "install", "update"],
        ["update"],
        ["update", "--plan"],
    ],
)
def test_image_marker_suppresses_recovery_mutation_for_every_command(
    monkeypatch, tmp_path, argv
):
    from hermes_cli import _early_recovery as early_recovery

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nversion = "1"\n', encoding="utf-8"
    )
    incomplete = checkout / ".update-incomplete"
    incomplete.write_text("pid=0\n", encoding="utf-8")
    marker = _write_marker(tmp_path / "image-provenance.json")
    monkeypatch.setattr(image_provenance, "IMAGE_PROVENANCE_PATH", marker)
    monkeypatch.setattr(
        early_recovery,
        "_complete_pending_core_install",
        _boom("immutable-image recovery"),
    )

    early_recovery.recover_if_needed(project_root=checkout, argv=argv)

    assert incomplete.is_file()
    assert not (checkout / ".update-incomplete.lock").exists()


@pytest.mark.parametrize(
    "argv",
    [[], ["chat"], ["gateway", "run"], ["doctor"], ["update"]],
)
def test_marker_absence_preserves_recovery_for_every_existing_command_shape(
    monkeypatch, tmp_path, argv
):
    from hermes_cli import _early_recovery as early_recovery

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nversion = "1"\n', encoding="utf-8"
    )
    incomplete = checkout / ".update-incomplete"
    incomplete.write_text("pid=0\n", encoding="utf-8")
    monkeypatch.setattr(
        image_provenance,
        "IMAGE_PROVENANCE_PATH",
        tmp_path / "missing-provenance.json",
    )
    calls = []
    monkeypatch.setattr(
        early_recovery,
        "_complete_pending_core_install",
        lambda root, pending: calls.append((root, pending)) or True,
    )

    early_recovery.recover_if_needed(project_root=checkout, argv=argv)

    assert calls == [(checkout, incomplete)]


@pytest.mark.parametrize(
    ("image_managed", "expected_calls"),
    [
        (True, []),
        (False, ["launcher-cleanup", "bytecode-sweep", "install-recovery"]),
    ],
)
def test_main_startup_maintenance_obeys_global_image_fact_and_absence_parity(
    monkeypatch, image_managed, expected_calls
):
    import hermes_cli.main as main

    calls = []
    monkeypatch.setattr(main, "_IMAGE_MANAGED_RUNTIME_BOOTSTRAP", image_managed)
    monkeypatch.setattr(
        main,
        "_cleanup_quarantined_exes",
        lambda: calls.append("launcher-cleanup"),
    )
    monkeypatch.setattr(
        main,
        "_sweep_stale_bytecode_if_checkout_changed",
        lambda: calls.append("bytecode-sweep"),
    )
    monkeypatch.setattr(
        main,
        "_recover_from_interrupted_install",
        lambda: calls.append("install-recovery"),
    )
    monkeypatch.setattr(main, "_try_termux_fast_tui_launch", lambda: True)
    monkeypatch.setattr(sys, "argv", ["hermes", "doctor"])

    main.main()

    assert calls == expected_calls


@pytest.mark.parametrize(
    ("argv", "is_update"),
    [
        (["update"], True),
        (["update", "--check"], True),
        (["--reasoning", "high", "update"], True),
        (["--reasoning=high", "update"], True),
        (["-m", "Hermes-4", "--provider", "nous", "update"], True),
        (["--model=Hermes-4", "update"], True),
        (["--usage-file", "usage.json", "update"], True),
        (["-p", "work", "update"], True),
        (["--profile", "work", "update"], True),
        (["--profile=work", "update"], True),
        (["--unknown-flag", "update"], True),
        (["--unknown-flag", "value", "update"], True),
        (["--i", "value", "update"], True),
        (["--", "update"], True),
        (["--continue", "session", "update"], True),
        (["--continue=session", "update"], True),
        (["skills", "install", "update"], False),
        (["--model", "update", "chat"], False),
        (["--mod", "update"], False),
        (["--provider", "update", "doctor"], False),
        (["--reas", "update"], False),
        (["--cont", "update"], False),
        (["--version", "update"], False),
        (["-V", "update"], False),
        (["--help", "update"], False),
        (["-h", "update"], False),
        (["--oneshot", "describe safety", "update"], False),
        (["--oneshot=describe safety", "update"], False),
        (["--one", "update"], False),
        (["-zdescribe-safety", "update"], False),
        ([], False),
        (["--reasoning", "high"], False),
        (["--"], False),
    ],
)
def test_one_bootstrap_detector_covers_cli_argv_without_false_updates(
    argv, is_update
):
    from hermes_cli import _early_recovery as early_recovery

    assert early_recovery._image_update_invocation(argv) is is_update


@pytest.mark.parametrize(
    ("argv", "expected_surface", "expected_target"),
    [
        (["update"], "cli", None),
        (["--reasoning", "high", "update", "--branch", "release"], "cli", "release"),
        (["update", "--branch=dev", "--gateway"], "gateway", "dev"),
    ],
)
def test_shared_early_recognizer_preserves_surface_and_target(
    monkeypatch, tmp_path, argv, expected_surface, expected_target
):
    import hermes_cli.main as main
    import hermes_cli.update_contract as contract

    marker = _write_marker(tmp_path / "image-provenance.json")
    monkeypatch.setattr(image_provenance, "IMAGE_PROVENANCE_PATH", marker)
    calls = []

    def _refuse(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(message="image-managed")

    monkeypatch.setattr(contract, "perform_update", _refuse)

    with pytest.raises(SystemExit) as exc:
        main._early_image_managed_update_gate(argv)

    assert exc.value.code == 2
    assert len(calls) == 1
    assert calls[0]["surface"] == expected_surface
    assert calls[0]["requested_target"] == expected_target


@pytest.mark.parametrize(("mode", "expected_exit"), [("--plan", 0), ("--check", 2)])
def test_shared_early_recognizer_keeps_image_read_modes_read_only(
    monkeypatch, tmp_path, mode, expected_exit
):
    import hermes_cli.main as main
    import hermes_cli.update_contract as contract

    marker = _write_marker(tmp_path / "image-provenance.json")
    monkeypatch.setattr(image_provenance, "IMAGE_PROVENANCE_PATH", marker)
    monkeypatch.setattr(contract, "perform_update", _boom("receipt persistence"))

    with pytest.raises(SystemExit) as exc:
        main._early_image_managed_update_gate(["update", mode])

    assert exc.value.code == expected_exit
    assert not (tmp_path / "receipts").exists()


@pytest.mark.parametrize(
    "update_argv",
    [
        ["--help"],
        ["-h"],
        ["--unknown-option"],
        ["--branch"],
        ["unexpected-positional"],
    ],
)
def test_bootstrap_canonically_renders_help_and_invalid_syntax_without_refusal(
    monkeypatch, tmp_path, update_argv, capsys
):
    import hermes_cli.main as main
    import hermes_cli.update_contract as contract

    marker = _write_marker(tmp_path / "image-provenance.json")
    monkeypatch.setattr(image_provenance, "IMAGE_PROVENANCE_PATH", marker)
    monkeypatch.setattr(contract, "perform_update", _boom("refusal persistence"))

    with pytest.raises(SystemExit) as exc:
        main._early_image_managed_update_gate(["update", *update_argv])

    expected_exit = 0 if update_argv[0] in {"--help", "-h"} else 2
    assert exc.value.code == expected_exit
    captured = capsys.readouterr()
    rendered = captured.out if expected_exit == 0 else captured.err
    assert "usage:" in rendered.lower()
    assert not (tmp_path / "receipts").exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["--unknown-global", "update"],
        ["--model", "gpt", "--unknown-global", "update"],
    ],
)
def test_bootstrap_canonically_rejects_invalid_global_prefix_without_refusal(
    monkeypatch, tmp_path, argv, capsys
):
    import hermes_cli.main as main
    import hermes_cli.update_contract as contract

    marker = _write_marker(tmp_path / "image-provenance.json")
    monkeypatch.setattr(image_provenance, "IMAGE_PROVENANCE_PATH", marker)
    monkeypatch.setattr(contract, "perform_update", _boom("refusal persistence"))

    with pytest.raises(SystemExit) as exc:
        main._early_image_managed_update_gate(argv)

    assert exc.value.code == 2
    assert "usage:" in capsys.readouterr().err.lower()
    assert not (tmp_path / "receipts").exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["--version", "update"],
        ["--oneshot", "describe update safety", "update"],
        ["--help", "update"],
        ["-h", "update"],
    ],
)
def test_top_level_dispatch_precedence_does_not_forge_update_attempt(
    monkeypatch, tmp_path, argv
):
    import hermes_cli.main as main
    import hermes_cli.update_contract as contract

    marker = _write_marker(tmp_path / "image-provenance.json")
    monkeypatch.setattr(image_provenance, "IMAGE_PROVENANCE_PATH", marker)
    monkeypatch.setattr(contract, "perform_update", _boom("refusal persistence"))

    assert main._early_image_managed_update_gate(argv) is False


@pytest.mark.live_system_guard_bypass
def test_image_update_help_is_exit_zero_read_only_and_receipt_free(tmp_path):
    marker = _write_marker(tmp_path / "image-provenance.json")
    hermes_home = tmp_path / "home"
    script = """
import pathlib
import sys

import hermes_cli.image_provenance as image_provenance

image_provenance.IMAGE_PROVENANCE_PATH = pathlib.Path(sys.argv[1])
sys.argv = ["hermes", "update", "--help"]
try:
    import hermes_cli.main
except SystemExit as exc:
    if exc.code != 0:
        raise
else:
    raise AssertionError("update --help did not exit through argparse")
"""
    env = _isolated_subprocess_env(hermes_home)

    result = subprocess.run(
        [sys.executable, "-c", script, str(marker)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()
    assert "--plan" in result.stdout
    assert "image_managed_update_refused" not in result.stdout
    assert not (hermes_home / "logs" / "update_receipts").exists()


@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize(
    "argv",
    [
        ["update", "--not-a-real-update-option"],
        ["--unknown-global", "value", "update"],
        ["--i", "value", "update"],
    ],
)
def test_image_update_invalid_syntax_uses_canonical_parser_without_receipt(
    tmp_path, argv
):
    marker = _write_marker(tmp_path / "image-provenance.json")
    hermes_home = tmp_path / "home"
    script = """
import pathlib
import json
import sys

import hermes_cli.image_provenance as image_provenance

image_provenance.IMAGE_PROVENANCE_PATH = pathlib.Path(sys.argv[1])
sys.argv = ["hermes", *json.loads(sys.argv[2])]
try:
    import hermes_cli.main
except SystemExit as exc:
    if exc.code != 2:
        raise
    print("CANONICAL_SYNTAX_OK")
else:
    raise AssertionError("invalid update syntax did not exit through argparse")
"""
    env = _isolated_subprocess_env(hermes_home)

    result = subprocess.run(
        [sys.executable, "-c", script, str(marker), json.dumps(argv)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "CANONICAL_SYNTAX_OK" in result.stdout
    assert "usage:" in result.stderr.lower()
    assert "image_managed_update_refused" not in result.stdout
    assert not (hermes_home / "logs" / "update_receipts").exists()


@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize(
    ("argv", "expected_exit", "receipt_expected"),
    [
        (["update"], 2, True),
        (["update", "--plan"], 0, False),
        (["update", "--check"], 2, False),
        (["update", "--help"], 0, False),
        (["update", "--not-a-real-update-option"], 2, False),
    ],
)
def test_image_update_import_boundary_has_exact_fresh_home_tree(
    tmp_path, argv, expected_exit, receipt_expected
):
    marker = _write_marker(tmp_path / "image-provenance.json")
    hermes_home = tmp_path / "fresh-home"
    script = """
import json
import pathlib
import sys

import hermes_cli.image_provenance as image_provenance

image_provenance.IMAGE_PROVENANCE_PATH = pathlib.Path(sys.argv[1])
expected_exit = int(sys.argv[2])
sys.argv = ["hermes", *json.loads(sys.argv[3])]
try:
    import hermes_cli.main
except SystemExit as exc:
    if exc.code != expected_exit:
        raise
else:
    raise AssertionError("image update crossed the terminal import boundary")
"""
    env = _isolated_subprocess_env(hermes_home)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(marker),
            str(expected_exit),
            json.dumps(argv),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    tree = (
        sorted(str(path.relative_to(hermes_home)) for path in hermes_home.rglob("*"))
        if hermes_home.exists()
        else []
    )
    if receipt_expected:
        assert tree[0:2] == ["logs", "logs/update_receipts"]
        assert "logs/update_receipts/latest.json" in tree
        receipt_files = [
            path
            for path in tree
            if path.startswith("logs/update_receipts/update_")
        ]
        assert len(receipt_files) == 1
        assert set(tree) == {
            "logs",
            "logs/update_receipts",
            "logs/update_receipts/latest.json",
            receipt_files[0],
        }
    else:
        assert tree == []


@pytest.mark.parametrize(
    "argv",
    [
        ["chat", "please update this file"],
        ["skills", "install", "update"],
        ["--model", "update", "chat"],
        ["doctor"],
    ],
)
def test_shared_early_recognizer_never_refuses_non_update_commands(
    monkeypatch, tmp_path, argv
):
    import hermes_cli.main as main

    marker = _write_marker(tmp_path / "image-provenance.json")
    monkeypatch.setattr(image_provenance, "IMAGE_PROVENANCE_PATH", marker)

    assert main._early_image_managed_update_gate(argv) is False


def test_marker_absence_preserves_git_check_path(monkeypatch, tmp_path):
    import hermes_cli.config as config
    import hermes_cli.main as main

    monkeypatch.setattr(
        image_provenance,
        "IMAGE_PROVENANCE_PATH",
        tmp_path / "missing-provenance.json",
    )
    monkeypatch.setattr(config, "is_managed", lambda: False)
    monkeypatch.setattr(config, "detect_install_method", lambda root: "git")
    calls = []
    monkeypatch.setattr(
        main,
        "_self",
        lambda: SimpleNamespace(
            _cmd_update_check=lambda **kwargs: calls.append(kwargs)
        ),
    )

    main.cmd_update(
        SimpleNamespace(plan=False, check=True, branch="release", gateway=False)
    )

    assert calls == [{"branch": "release", "branch_explicit": True}]


@pytest.mark.parametrize(
    ("method", "expected_fragment"),
    [
        ("docker", "legacy docker guidance"),
        ("nix", "update-via-nix"),
        ("nixos", "update-via-nixos"),
        ("home-manager", "update-via-home-manager"),
        ("apt", "update-via-apt"),
    ],
)
def test_marker_absence_preserves_every_legacy_package_refusal(
    monkeypatch, tmp_path, capsys, method, expected_fragment
):
    import hermes_cli.config as config
    import hermes_cli.main as main

    monkeypatch.setattr(
        image_provenance,
        "IMAGE_PROVENANCE_PATH",
        tmp_path / "missing-provenance.json",
    )
    monkeypatch.setattr(config, "is_managed", lambda: False)
    monkeypatch.setattr(config, "detect_install_method", lambda root: method)
    monkeypatch.setattr(
        config,
        "format_docker_update_message",
        lambda: "legacy docker guidance",
    )
    monkeypatch.setattr(
        config,
        "recommended_update_command_for_method",
        lambda detected: f"update-via-{detected}",
    )

    with pytest.raises(SystemExit) as exc:
        main.cmd_update(
            SimpleNamespace(plan=False, check=False, branch=None, gateway=False)
        )

    assert exc.value.code == 1
    assert expected_fragment in capsys.readouterr().out


def test_dashboard_marker_absence_preserves_git_action_spawn(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    import hermes_cli.web_server as web_server

    monkeypatch.setattr(
        image_provenance,
        "IMAGE_PROVENANCE_PATH",
        tmp_path / "missing-provenance.json",
    )
    monkeypatch.setattr(web_server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        web_server, "_dashboard_local_update_managed_externally", lambda: False
    )
    monkeypatch.setattr(web_server, "detect_install_method", lambda root: "git")
    calls = []

    def _spawn(subcommand, name, *, env_overrides=None):
        calls.append((subcommand, name, env_overrides))
        return SimpleNamespace(pid=321)

    monkeypatch.setattr(web_server, "_spawn_hermes_action", _spawn)
    web_server._ACTION_PROCS.clear()
    web_server._ACTION_RESULTS.clear()
    web_server._ACTION_IDS.clear()

    result = asyncio.run(web_server.update_hermes())

    assert result["ok"] is True
    assert result["pid"] == 321
    assert result["name"] == "hermes-update"
    assert len(result["action_id"]) == 32
    assert calls == [
        (
            ["update"],
            "hermes-update",
            {"HERMES_ACTION_ID": result["action_id"]},
        )
    ]


@pytest.mark.parametrize(
    ("method", "expected_error"),
    [
        ("docker", "docker_update_unsupported"),
        ("nix", "nix_update_unsupported"),
        ("nixos", "nix_update_unsupported"),
        ("home-manager", "nix_update_unsupported"),
        ("apt", "apt_update_required"),
    ],
)
def test_dashboard_marker_absence_preserves_legacy_package_gate(
    monkeypatch, tmp_path, method, expected_error
):
    pytest.importorskip("fastapi")
    import hermes_cli.web_server as web_server

    monkeypatch.setattr(
        image_provenance,
        "IMAGE_PROVENANCE_PATH",
        tmp_path / "missing-provenance.json",
    )
    monkeypatch.setattr(web_server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        web_server, "_dashboard_local_update_managed_externally", lambda: False
    )
    monkeypatch.setattr(web_server, "detect_install_method", lambda root: method)
    monkeypatch.setattr(
        web_server,
        "is_nix_install_method",
        lambda detected: detected in {"nix", "nixos", "home-manager"},
    )
    monkeypatch.setattr(
        web_server,
        "format_docker_update_message",
        lambda: "legacy docker guidance",
    )
    monkeypatch.setattr(
        web_server,
        "recommended_update_command_for_method",
        lambda detected: f"update-via-{detected}",
    )
    completed = []
    monkeypatch.setattr(
        web_server,
        "_record_completed_action",
        lambda name, message, exit_code=1: completed.append(
            (name, message, exit_code)
        ),
    )
    monkeypatch.setattr(web_server, "_spawn_hermes_action", _boom("action spawn"))

    result = asyncio.run(web_server.update_hermes())

    assert result["ok"] is False
    assert result["error"] == expected_error
    assert completed == [("hermes-update", result["message"], 1)]


def test_dashboard_marker_absence_preserves_git_update_check(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    import hermes_cli.banner as banner
    import hermes_cli.web_server as web_server

    monkeypatch.setattr(
        image_provenance,
        "IMAGE_PROVENANCE_PATH",
        tmp_path / "missing-provenance.json",
    )
    monkeypatch.setattr(web_server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        web_server, "_dashboard_local_update_managed_externally", lambda: False
    )
    monkeypatch.setattr(web_server, "detect_install_method", lambda root: "git")
    monkeypatch.setattr(
        web_server,
        "recommended_update_command_for_method",
        lambda method: "hermes update",
    )
    monkeypatch.setattr(banner, "check_for_updates", lambda: 3)
    commits = [
        {"sha": "abc1234", "summary": "fix: one", "author": "Nous", "at": 1}
    ]
    monkeypatch.setattr(web_server, "_recent_upstream_commits", lambda: commits)

    result = asyncio.run(web_server.check_hermes_update(force=False))

    assert result == {
        "install_method": "git",
        "current_version": web_server.__version__,
        "behind": 3,
        "update_available": True,
        "can_apply": True,
        "update_command": "hermes update",
        "message": None,
        "commits": commits,
    }
