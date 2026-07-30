"""Startup behavior at the managed read-only skills boundary."""

from pathlib import Path
from types import SimpleNamespace
import sys

import pytest


def _write_managed_skill(home: Path) -> Path:
    skill = home / "skills" / "planning" / "ticket-decomposition"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        "name: ticket-decomposition\n"
        "description: Split work into durable tickets.\n"
        "---\n\n"
        "# Ticket decomposition\n",
        encoding="utf-8",
    )
    (home / "config.yaml").write_text(
        "skills:\n  content_mode: read_only\n",
        encoding="utf-8",
    )
    return home / "skills"


def _census(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_cli_startup_skips_sync_and_reaches_nested_discovery(
    tmp_path,
    monkeypatch,
):
    from hermes_cli import config
    from hermes_cli import main as main_mod
    from tools import skills_tool

    home = tmp_path / ".hermes"
    skills = _write_managed_skill(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    config._LOAD_CONFIG_CACHE.clear()
    config._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    before = _census(skills)

    main_mod._sync_bundled_skills_quietly()
    discovered = skills_tool._find_all_skills(skip_disabled=True)

    assert any(
        skill["name"] == "ticket-decomposition"
        and skill["category"] == "planning"
        for skill in discovered
    )
    assert _census(skills) == before
    assert not (skills / ".bundled_manifest").exists()
    assert not (home / "state").exists()


def test_invalid_config_still_reaches_read_only_discovery(
    tmp_path,
    monkeypatch,
):
    from hermes_cli import config
    from hermes_cli import main as main_mod
    from tools import skills_tool

    home = tmp_path / ".hermes"
    skills = _write_managed_skill(home)
    (home / "config.yaml").write_text(
        "skills: [unterminated\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    config._LOAD_CONFIG_CACHE.clear()
    config._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    before = _census(skills)

    main_mod._sync_bundled_skills_quietly()
    discovered = skills_tool._find_all_skills(skip_disabled=True)

    assert any(
        skill["name"] == "ticket-decomposition"
        for skill in discovered
    )
    assert _census(skills) == before
    assert not (home / "state").exists()


def test_main_oneshot_routes_preloaded_nested_skill(monkeypatch):
    from hermes_cli import main as main_mod

    class RoutedOneshot(Exception):
        pass

    captured = {}

    def capture_oneshot(prompt, **kwargs):
        captured["prompt"] = prompt
        captured.update(kwargs)
        raise RoutedOneshot

    monkeypatch.setattr(main_mod, "_set_process_title", lambda: None)
    monkeypatch.setattr(
        main_mod,
        "_sweep_stale_bytecode_if_checkout_changed",
        lambda: None,
    )
    monkeypatch.setattr(
        main_mod,
        "_recover_from_interrupted_install",
        lambda: None,
    )
    monkeypatch.setattr(main_mod, "_try_termux_fast_tui_launch", lambda: False)
    monkeypatch.setattr(main_mod, "_try_termux_fast_cli_launch", lambda: False)
    monkeypatch.setattr(main_mod, "_prepare_agent_startup", lambda _args: None)
    monkeypatch.setattr(main_mod, "_run_and_exit_oneshot", capture_oneshot)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hermes",
            "--skills",
            "planning/ticket-decomposition",
            "--oneshot",
            "Return the loaded skill name only.",
        ],
    )

    with pytest.raises(RoutedOneshot):
        main_mod.main()

    assert captured["prompt"] == "Return the loaded skill name only."
    assert captured["skills"] == ["planning/ticket-decomposition"]


def test_cmd_skills_propagates_policy_refusal_status(tmp_path, monkeypatch):
    from hermes_cli import main as main_mod

    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "skills:\n  content_mode: read_only\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    assert (
        main_mod.cmd_skills(
            SimpleNamespace(
                skills_action="update",
                name="planning/ticket-decomposition",
            )
        )
        == 2
    )


@pytest.mark.asyncio
async def test_gateway_startup_skips_sync_and_reaches_nested_discovery(
    tmp_path,
    monkeypatch,
):
    from gateway.config import GatewayConfig
    from gateway.run import start_gateway
    from hermes_cli import config
    from tools import skills_tool

    home = tmp_path / ".hermes"
    skills = _write_managed_skill(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    config._LOAD_CONFIG_CACHE.clear()
    config._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    before = _census(skills)
    reached = []

    class _CleanExitRunner:
        def __init__(self, gateway_config):
            self.config = gateway_config
            self.should_exit_cleanly = True
            self.exit_reason = None
            self.exit_code = None
            self.adapters = {}

        async def start(self):
            assert self._platform_lock_takeover_on_start is False
            reached.extend(
                skill["name"]
                for skill in skills_tool._find_all_skills(skip_disabled=True)
            )
            return True

        async def stop(self):
            return None

    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr(
        "hermes_logging.setup_logging",
        lambda hermes_home, mode: tmp_path,
    )
    monkeypatch.setattr(
        "hermes_logging._add_rotating_handler",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr("gateway.run.GatewayRunner", _CleanExitRunner)

    assert (
        await start_gateway(
            config=GatewayConfig(),
            replace=False,
            verbosity=1,
        )
        is True
    )
    assert "ticket-decomposition" in reached
    assert _census(skills) == before
    assert not (skills / ".bundled_manifest").exists()
