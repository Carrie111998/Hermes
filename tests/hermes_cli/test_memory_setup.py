from types import SimpleNamespace
from unittest.mock import MagicMock

import hermes_cli.memory_setup as memory_setup
from hermes_cli.memory_setup import _CANCELLED, _curses_select








def test_cmd_setup_generic_choice_cancel_writes_nothing(tmp_path, monkeypatch):
    class ChoiceProvider:
        def __init__(self):
            self.save_config = MagicMock()

        def get_config_schema(self):
            return [{
                "key": "mode",
                "description": "Mode",
                "default": "one",
                "choices": ["one", "two"],
            }]

    provider = ChoiceProvider()
    selections = iter([0, _CANCELLED])
    save_config = MagicMock()
    install_dependencies = MagicMock()

    monkeypatch.setattr(memory_setup, "_get_available_providers", lambda: [("fake", "local", provider)])
    monkeypatch.setattr(memory_setup, "_curses_select", lambda *args, **kwargs: next(selections))
    monkeypatch.setattr(memory_setup, "_install_dependencies", install_dependencies)
    monkeypatch.setattr(memory_setup, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"memory": {}})
    monkeypatch.setattr("hermes_cli.config.save_config", save_config)

    memory_setup.cmd_setup(SimpleNamespace())

    install_dependencies.assert_called_once_with("fake")
    save_config.assert_not_called()
    provider.save_config.assert_not_called()
    assert not (tmp_path / ".env").exists()


def test_write_env_vars_strips_line_separators_and_nul(tmp_path):
    """A pasted secret with embedded CR/LF/NUL must not inject an extra
    KEY=VALUE line into .env (mirrors the openviking plugin's writer)."""
    env_path = tmp_path / ".env"

    memory_setup._write_env_vars(
        env_path,
        {"PROVIDER_API_KEY": "good\nINJECTED_KEY=attacker\r\u2028\x00tail"},
    )

    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert lines == ["PROVIDER_API_KEY=goodINJECTED_KEY=attackertail"]
    parsed = dict(line.split("=", 1) for line in lines if "=" in line)
    assert set(parsed) == {"PROVIDER_API_KEY"}




# ---------------------------------------------------------------------------
# _provider_pip_dependencies — mode-aware dep expansion (#70636)
# ---------------------------------------------------------------------------





def test_install_dependencies_force_reinstalls_versioned_specs(tmp_path, monkeypatch):
    """force=True hands every declared spec (version ranges intact) to pip,
    so a downgraded/stripped bridge package is restored on hermes update."""
    import yaml as _yaml

    plugin_dir = tmp_path / "mem0"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text(
        _yaml.safe_dump({"pip_dependencies": ["mem0ai>=2.0.10,<3"]}), encoding="utf-8"
    )
    monkeypatch.setattr(
        "plugins.memory.find_provider_dir", lambda name: plugin_dir
    )

    installed = []

    def fake_install_specs(specs, timeout=120):
        installed.append(list(specs))
        return SimpleNamespace(ok=True, blocked=False, reason="", stderr="")

    monkeypatch.setattr("tools.lazy_deps.install_specs", fake_install_specs)

    memory_setup._install_dependencies("mem0", force=True)

    assert installed, "force=True must reach the install step"
    assert any("mem0ai>=2.0.10,<3" in specs for specs in installed)


# ---------------------------------------------------------------------------
# _provider_pip_dependencies — platform-aware slim-runtime selection (#81421)
# ---------------------------------------------------------------------------
# Darwin+x86_64 (Intel macOS) cannot satisfy the full `hindsight-all` stack
# cleanly: the meta-package pulls MLX without distinguishing arm64 from x86_64,
# so the resolver backtracks to ancient releases and the slim runtime is
# silently downgraded (#81421). The fix routes Intel macOS to the slim stack
# while keeping every other platform (Apple Silicon, Linux, Windows) on the
# existing full `hindsight-all` install path.
# ---------------------------------------------------------------------------


def _fake_platform(system, machine):
    """Build a `platform`-like namespace exposing .system() / .machine().

    `memory_setup._provider_pip_dependencies` will import `platform` and call
    `.system()` / `.machine()`; we don't want the host's actual values to
    leak into the test, so we monkeypatch the whole `platform` module.
    """
    return SimpleNamespace(system=lambda: system, machine=lambda: machine)


def test_hindsight_intel_macos_uses_slim_runtime(tmp_path, monkeypatch):
    """Darwin+x86_64 local_embedded must request the slim stack — never the
    bare full bundle, which triggers the resolver backtrack that drops the
    slim runtime on top of the working one (#81421)."""
    config_path = tmp_path / "hindsight" / "config.json"
    config_path.parent.mkdir()
    config_path.write_text('{"mode": "local_embedded"}', encoding="utf-8")
    monkeypatch.setattr(memory_setup, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(memory_setup, "platform", _fake_platform("Darwin", "x86_64"))

    deps = memory_setup._provider_pip_dependencies(
        "hindsight", ["hindsight-client>=0.6.1"]
    )

    assert "hindsight-all" not in deps, (
        "Intel macOS must NOT request the bare `hindsight-all` meta-package: "
        "it pulls MLX with no x86_64 wheel and the resolver backtracks to "
        "ancient API releases that shadow the working slim runtime (#81421)."
    )
    assert any("hindsight-all-slim" in d for d in deps), (
        "Intel macOS must request `hindsight-all-slim` as part of the slim stack."
    )
    assert any("hindsight-api-slim" in d for d in deps), (
        "Intel macOS must request `hindsight-api-slim` (with local-onnx extra) "
        "so the daemon can import the configured ONNX embeddings provider."
    )


def test_hindsight_apple_silicon_keeps_full_runtime(tmp_path, monkeypatch):
    """Darwin+arm64 has no known MLX/x86_64-wheel conflict and must keep the
    full `hindsight-all` runtime — the fix is Intel-macOS-specific."""
    config_path = tmp_path / "hindsight" / "config.json"
    config_path.parent.mkdir()
    config_path.write_text('{"mode": "local_embedded"}', encoding="utf-8")
    monkeypatch.setattr(memory_setup, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(memory_setup, "platform", _fake_platform("Darwin", "arm64"))

    deps = memory_setup._provider_pip_dependencies(
        "hindsight", ["hindsight-client>=0.6.1"]
    )

    assert "hindsight-all" in deps, (
        "Apple Silicon has no MLX/x86_64 backtrack issue — keep the existing "
        "full `hindsight-all` install path (#81421)."
    )
    assert not any("hindsight-all-slim" in d for d in deps), (
        "Apple Silicon must not be routed to the slim stack — the slim stack "
        "is the Intel-macOS workaround, not a generic slim-first policy."
    )


def test_hindsight_linux_keeps_full_runtime(tmp_path, monkeypatch):
    """Linux has no Darwin-specific MLX conflict and must keep the existing
    full `hindsight-all` runtime untouched."""
    config_path = tmp_path / "hindsight" / "config.json"
    config_path.parent.mkdir()
    config_path.write_text('{"mode": "local_embedded"}', encoding="utf-8")
    monkeypatch.setattr(memory_setup, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(memory_setup, "platform", _fake_platform("Linux", "x86_64"))

    deps = memory_setup._provider_pip_dependencies(
        "hindsight", ["hindsight-client>=0.6.1"]
    )

    assert "hindsight-all" in deps, (
        "Linux must keep the existing full `hindsight-all` runtime — "
        "the #81421 fix is scoped to Intel macOS only."
    )
    assert not any("hindsight-all-slim" in d for d in deps)


def test_hindsight_intel_macos_non_local_embedded_keeps_full_runtime(tmp_path, monkeypatch):
    """The slim-routing only applies to ``local_embedded`` (and its legacy
    alias ``local``). Other modes that legitimately want the full bundle
    — and any mode the codebase doesn't yet expand — must stay unchanged:
    we don't widen the slim workaround to platforms/modes that don't have
    the MLX/x86_64 backtrack issue, and we don't pre-empt other PRs that
    own their respective mode expansions (#81316 for ``local_external``)."""
    config_path = tmp_path / "hindsight" / "config.json"
    config_path.parent.mkdir()
    config_path.write_text('{"mode": "cloud"}', encoding="utf-8")
    monkeypatch.setattr(memory_setup, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(memory_setup, "platform", _fake_platform("Darwin", "x86_64"))

    deps = memory_setup._provider_pip_dependencies(
        "hindsight", ["hindsight-client>=0.6.1"]
    )

    # A mode the function does NOT expand at all (e.g. cloud-only) must
    # pass through untouched on Intel macOS — no slim stack, no full
    # bundle, just the declared bridge packages.
    assert deps == ["hindsight-client>=0.6.1"], (
        "Modes outside {local, local_embedded} must not be expanded on any "
        "platform; the #81421 fix must be scoped narrowly to local_embedded."
    )


def test_hindsight_intel_macos_local_alias_uses_slim_runtime(tmp_path, monkeypatch):
    """``local`` is a legacy alias for ``local_embedded`` and must get the
    same Intel-macOS slim treatment, mirroring the existing alias branch
    in the non-Intel code path."""
    config_path = tmp_path / "hindsight" / "config.json"
    config_path.parent.mkdir()
    config_path.write_text('{"mode": "local"}', encoding="utf-8")
    monkeypatch.setattr(memory_setup, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(memory_setup, "platform", _fake_platform("Darwin", "x86_64"))

    deps = memory_setup._provider_pip_dependencies(
        "hindsight", ["hindsight-client>=0.6.1"]
    )

    assert "hindsight-all" not in deps
    assert any("hindsight-all-slim" in d for d in deps)
    assert any("hindsight-api-slim" in d for d in deps)
