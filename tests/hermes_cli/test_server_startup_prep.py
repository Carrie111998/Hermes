from __future__ import annotations

import builtins
import subprocess

import hermes_cli.config as config_mod
import hermes_cli.main as main_mod
import hermes_cli.server_startup as startup_mod
import tools.skills_sync as skills_sync_mod


def _configure_fingerprint(monkeypatch, tmp_path, status: bytes = b""):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    monkeypatch.setattr(main_mod, "PROJECT_ROOT", repo)
    monkeypatch.setattr(main_mod, "get_hermes_home", lambda: home)
    monkeypatch.setattr(main_mod, "_read_git_revision_fingerprint", lambda _root: "revision")
    monkeypatch.setattr(config_mod, "read_raw_config", lambda: {})
    monkeypatch.setattr(
        main_mod.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout=status),
    )
    return repo, home


def test_startup_skills_fingerprint_is_stable_for_unchanged_inputs(monkeypatch, tmp_path) -> None:
    _configure_fingerprint(monkeypatch, tmp_path)

    first = main_mod._startup_skills_fingerprint()
    second = main_mod._startup_skills_fingerprint()

    assert first
    assert first == second


def test_startup_skills_fingerprint_tracks_dirty_skill_content(monkeypatch, tmp_path) -> None:
    repo, _home = _configure_fingerprint(
        monkeypatch,
        tmp_path,
        status=b"?? skills/example/SKILL.md\0",
    )
    skill = repo / "skills" / "example" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("first", encoding="utf-8")
    first = main_mod._startup_skills_fingerprint()

    skill.write_text("second", encoding="utf-8")
    second = main_mod._startup_skills_fingerprint()

    assert first
    assert second
    assert first != second


def test_external_skill_dirs_disable_the_shortcut(monkeypatch, tmp_path) -> None:
    _configure_fingerprint(monkeypatch, tmp_path)
    monkeypatch.setattr(
        config_mod,
        "read_raw_config",
        lambda: {"skills": {"external_dirs": ["external"]}},
    )

    assert main_mod._startup_skills_fingerprint() is None


def test_successful_sync_writes_stamp_and_cache_hit_skips_work(monkeypatch, tmp_path) -> None:
    _repo, home = _configure_fingerprint(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(main_mod, "_startup_skills_fingerprint", lambda: "fingerprint")
    monkeypatch.setattr(skills_sync_mod, "sync_skills", lambda **kwargs: calls.append(kwargs))

    main_mod._sync_bundled_skills_quietly()
    main_mod._sync_bundled_skills_quietly()

    assert calls == [{"quiet": True}]
    assert (home / "skills" / ".startup_sync_stamp").read_text(encoding="utf-8") == "fingerprint\n"


def test_failed_sync_does_not_stamp_success(monkeypatch, tmp_path) -> None:
    _repo, home = _configure_fingerprint(monkeypatch, tmp_path)
    monkeypatch.setattr(main_mod, "_startup_skills_fingerprint", lambda: "fingerprint")

    def fail(**_kwargs) -> None:
        raise RuntimeError("sync failed")

    monkeypatch.setattr(skills_sync_mod, "sync_skills", fail)

    main_mod._sync_bundled_skills_quietly()

    assert not (home / "skills" / ".startup_sync_stamp").exists()


def test_gateway_module_warmup_is_process_idempotent(monkeypatch) -> None:
    imported = []
    original_import = builtins.__import__

    def tracking_import(name, *args, **kwargs):
        if name in startup_mod._WARM_MODULES:
            imported.append(name)
            return object()
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(startup_mod, "_warm_complete", False)
    monkeypatch.setattr(builtins, "__import__", tracking_import)

    startup_mod.warm_gateway_modules()
    startup_mod.warm_gateway_modules()

    assert imported == list(startup_mod._WARM_MODULES)
