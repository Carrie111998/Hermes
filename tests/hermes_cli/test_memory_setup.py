from pathlib import Path
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
    # Substring checks would silently accept the full bundle too — assert
    # the exact slim spec so a future `hindsight-all-slim` rename cannot
    # quietly lose the `[local-onnx]` extra that powers the configured
    # ONNX embeddings provider.
    assert "hindsight-all-slim" in deps, (
        "Intel macOS must request `hindsight-all-slim` as part of the slim stack."
    )
    assert "hindsight-api-slim[local-onnx]" in deps, (
        "Intel macOS must request `hindsight-api-slim[local-onnx]` (with the "
        "local-onnx extra) so the daemon can import the configured ONNX "
        "embeddings provider — dropping the extra silently breaks the daemon "
        "even with the slim stack installed (#81421)."
    )
    assert "hindsight-embed" in deps, (
        "Intel macOS must request `hindsight-embed` so the embed manager "
        "that drives the ONNX embeddings provider can be imported."
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
    assert "hindsight-all-slim" in deps
    assert "hindsight-api-slim[local-onnx]" in deps
    assert "hindsight-embed" in deps


# ---------------------------------------------------------------------------
# _install_dependencies — post-install smoke check on Intel macOS (#81421)
# ---------------------------------------------------------------------------
# The slim stack install reports success if pip returns ok, but the real
# failure mode is the resolver backtracking to an ancient API release that
# shadows the slim runtime — pip says ok, the daemon then crashes with
# `Unknown embeddings provider: onnx`. After install we smoke-check that
# the configured local runtime actually imports, and surface an actionable
# warning if it doesn't. We only smoke-check on the affected code path
# (Intel macOS + local_embedded) so non-Intel installs and cloud modes
# don't pay a new import cost on every refresh.
# ---------------------------------------------------------------------------


def test_hindsight_intel_macos_install_smoke_import_failure_raises(
    tmp_path, monkeypatch, capsys
):
    """When install_specs reports success on Intel macOS local_embedded but
    the slim runtime smoke-validation fails, ``_install_dependencies`` must
    raise RuntimeError so callers (and ``hermes update``) treat the heal
    as failed — not silently return as if the heal succeeded (#81421)."""
    import pytest
    import yaml as _yaml

    plugin_dir = tmp_path / "hindsight"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text(
        _yaml.safe_dump({"pip_dependencies": ["hindsight-client>=0.6.1"]}),
        encoding="utf-8",
    )

    config_path = tmp_path / "hindsight" / "config.json"
    config_path.write_text('{"mode": "local_embedded"}', encoding="utf-8")

    monkeypatch.setattr(
        "plugins.memory.find_provider_dir", lambda name: plugin_dir
    )
    monkeypatch.setattr(memory_setup, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(memory_setup, "platform", _fake_platform("Darwin", "x86_64"))

    def fake_install_specs(specs, timeout=120):
        return SimpleNamespace(ok=True, blocked=False, reason="", stderr="")

    monkeypatch.setattr("tools.lazy_deps.install_specs", fake_install_specs)

    def fake_smoke_import():
        return [
            "hindsight_api.LocalSTEmbeddings: AttributeError: the slim runtime "
            "shipped an API release that does not expose the configured ONNX "
            "embeddings provider — this is the #81421 backtrack failure mode"
        ]

    # Patch the dedicated helper rather than builtins.__import__ so
    # pytest's own fixture lookup isn't disturbed.
    monkeypatch.setattr(
        memory_setup, "_smoke_import_hindsight_local", fake_smoke_import
    )

    with pytest.raises(RuntimeError) as excinfo:
        memory_setup._install_dependencies("hindsight", force=True)

    assert "Hindsight slim runtime smoke validation failed" in str(excinfo.value), (
        "The raised error must clearly identify the #81421 smoke-validation "
        "failure so upstream callers can surface it in their own failure UI."
    )

    captured = capsys.readouterr().out
    assert "smoke validation failed" in captured, (
        "The smoke-failure warning must still be printed so the user can see "
        "which module caused the failure before the exception is raised."
    )
    assert "LocalSTEmbeddings" in captured, (
        "The smoke-failure warning must name the failed symbol so the user "
        "knows which class to chase down."
    )


def test_hindsight_intel_macos_install_smoke_success_is_silent(
    tmp_path, monkeypatch, capsys
):
    """When the smoke check returns no errors on Intel macOS local_embedded,
    ``_install_dependencies`` must not raise and must print neither the
    failure banner nor a success line — the existing `✓ Installed ...`
    line is enough and the smoke check should be invisible on healthy
    setups."""
    import yaml as _yaml

    plugin_dir = tmp_path / "hindsight"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text(
        _yaml.safe_dump({"pip_dependencies": ["hindsight-client>=0.6.1"]}),
        encoding="utf-8",
    )

    config_path = tmp_path / "hindsight" / "config.json"
    config_path.write_text('{"mode": "local_embedded"}', encoding="utf-8")

    monkeypatch.setattr(
        "plugins.memory.find_provider_dir", lambda name: plugin_dir
    )
    monkeypatch.setattr(memory_setup, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(memory_setup, "platform", _fake_platform("Darwin", "x86_64"))

    def fake_install_specs(specs, timeout=120):
        return SimpleNamespace(ok=True, blocked=False, reason="", stderr="")

    monkeypatch.setattr("tools.lazy_deps.install_specs", fake_install_specs)
    monkeypatch.setattr(memory_setup, "_smoke_import_hindsight_local", lambda: [])

    # Must not raise — the heal is genuinely successful.
    memory_setup._install_dependencies("hindsight", force=True)

    captured = capsys.readouterr().out
    assert "smoke validation failed" not in captured, (
        "Healthy Intel macOS installs must not print a smoke-failure banner."
    )


def test_smoke_import_hindsight_local_reports_missing_class(tmp_path):
    """The helper that backs the post-install smoke check must surface the
    #81421 backtrack signature: ``hindsight_api`` importing but
    ``LocalSTEmbeddings`` absent. The helper runs in a clean subprocess
    (#81421 round-4) so we drive it via PYTHONPATH stub modules."""
    import os
    import subprocess
    import sys

    stubs_dir = tmp_path / "stubs"
    stubs_dir.mkdir()
    (stubs_dir / "hindsight_api.py").write_text(
        "# Deliberately omit LocalSTEmbeddings to simulate the #81421 backtrack.\n"
    )
    (stubs_dir / "hindsight_embed.py").write_text("# stub\n")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(stubs_dir) + os.pathsep + env.get("PYTHONPATH", "")

    script = (
        "import importlib.util\n"
        "spec = importlib.util.spec_from_file_location('memory_setup_under_test', "
        f"{repr(str(Path(memory_setup.__file__).resolve()))})\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "errors = mod._smoke_import_hindsight_local()\n"
        "print(repr(errors))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, (
        f"Smoke subprocess exited {completed.returncode}: {completed.stderr}"
    )
    errors = eval(completed.stdout.strip())  # noqa: S307 — test fixture
    assert any(
        "LocalSTEmbeddings" in e and "ONNX embeddings provider" in e
        for e in errors
    ), (
        "The smoke check must report the missing LocalSTEmbeddings symbol "
        "when hindsight_api is present but the configured provider class is "
        "not — that's the exact signature of the #81421 backtrack failure."
    )


def test_install_dependencies_missing_dep_probe_resolves_slim_packages(
    tmp_path, monkeypatch
):
    """The slim-stack pip packages have a non-trivial pip-name → import-name
    mapping (``hindsight-all-slim`` → ``hindsight_api``, ``hindsight-api-slim``
    → ``hindsight_api``, ``hindsight-embed`` → ``hindsight_embed``). Without
    these entries the missing-dep probe at the top of
    ``_install_dependencies`` would mark the slim packages as still-missing
    on every refresh (since ``import hindsight_all_slim`` always fails), and
    reinstall them indefinitely — defeating the point of the slim-stack
    switch.

    This is a real-behavior test (no ``inspect.getsource``). It injects
    fake ``hindsight_api`` / ``hindsight_embed`` modules into
    ``sys.modules`` so the missing-dep probe sees the slim packages as
    importable, then drives ``_install_dependencies`` with ``force=False``
    and asserts that no install call is made."""
    import sys
    import types

    import yaml as _yaml

    plugin_dir = tmp_path / "hindsight"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text(
        _yaml.safe_dump({"pip_dependencies": ["hindsight-client>=0.6.1"]}),
        encoding="utf-8",
    )

    config_path = tmp_path / "hindsight" / "config.json"
    config_path.write_text('{"mode": "local_embedded"}', encoding="utf-8")

    monkeypatch.setattr(
        "plugins.memory.find_provider_dir", lambda name: plugin_dir
    )
    monkeypatch.setattr(memory_setup, "get_hermes_home", lambda: tmp_path)
    # Linux + x86_64 keeps the missing-dep probe path non-Apple-Silicon,
    # so the slim packages on the Intel-macOS branch are produced but
    # the smoke check is gated off — we want to test the probe, not
    # the smoke path.
    monkeypatch.setattr(
        memory_setup, "platform", _fake_platform("Linux", "x86_64")
    )

    # Fake modules that the slim pip names map to.
    api_module = types.ModuleType("hindsight_api")
    embed_module = types.ModuleType("hindsight_embed")
    client_module = types.ModuleType("hindsight_client")
    # Pop any pre-existing real versions so the fake ones win for the
    # duration of this test.
    for name in ("hindsight_api", "hindsight_embed", "hindsight_client"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    sys.modules["hindsight_api"] = api_module
    sys.modules["hindsight_embed"] = embed_module
    sys.modules["hindsight_client"] = client_module

    install_calls = []

    def fake_install_specs(specs, timeout=120):
        install_calls.append(list(specs))
        return SimpleNamespace(ok=True, blocked=False, reason="", stderr="")

    monkeypatch.setattr("tools.lazy_deps.install_specs", fake_install_specs)

    try:
        # The full slim stack on Intel macOS — but we're on Linux so
        # we get hindsight-all (full bundle) instead. The point is
        # the missing-dep probe at the top must resolve every dep
        # through _IMPORT_NAMES, regardless of which branch chose them.
        # We patch platform to Darwin+x86_64 to get the slim branch,
        # then rely on the fake modules for resolution.
        monkeypatch.setattr(
            memory_setup,
            "platform",
            _fake_platform("Darwin", "x86_64"),
        )
        # Smoke must succeed on a healthy fake — fake modules don't
        # ship LocalSTEmbeddings, so we'd raise. Patch smoke to skip.
        monkeypatch.setattr(
            memory_setup, "_smoke_import_hindsight_local", lambda: []
        )

        memory_setup._install_dependencies("hindsight", force=False)
    finally:
        for name in ("hindsight_api", "hindsight_embed", "hindsight_client"):
            sys.modules.pop(name, None)

    assert not install_calls, (
        "force=False on Intel macOS with all slim packages importable "
        "in the host venv must NOT trigger pip — every slim package "
        "must resolve through _IMPORT_NAMES, otherwise `hermes update` "
        "would reinstall the slim stack on every refresh and defeat "
        "the #81421 fix."
    )


def test_smoke_import_hindsight_local_healthy_path_uses_api_module(
    tmp_path, monkeypatch
):
    """Regression guard for the round-3 #81421 review: the helper that
    probes for the configured embeddings class must read it from
    ``hindsight_api`` specifically, not from the final value of a
    shared loop variable (which was ``hindsight_embed`` after the last
    iteration). A healthy slim install where ``hindsight_api`` exposes
    ``LocalSTEmbeddings`` but ``hindsight_embed`` does not must report
    *no* errors — the helper used to wrongly reject that case.

    The helper now runs the probe in a clean subprocess (#81421 round-4),
    so we drive it through ``sys.modules`` injection on a PYTHONPATH-bound
    directory that contains stub packages."""
    import os
    import subprocess
    import sys

    stubs_dir = tmp_path / "stubs"
    stubs_dir.mkdir()
    # hindsight_api with LocalSTEmbeddings, hindsight_embed without.
    (stubs_dir / "hindsight_api.py").write_text(
        "class LocalSTEmbeddings:\n    pass\n"
    )
    (stubs_dir / "hindsight_embed.py").write_text("# intentionally no LocalSTEmbeddings\n")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(stubs_dir) + os.pathsep + env.get("PYTHONPATH", "")
    # Import the module that contains the helper and invoke it from
    # Python so subprocess.run resolves to the same Python interpreter.
    script = (
        "import importlib.util\n"
        "spec = importlib.util.spec_from_file_location('memory_setup_under_test', "
        f"{repr(str(Path(memory_setup.__file__).resolve()))})\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "errors = mod._smoke_import_hindsight_local()\n"
        "print(repr(errors))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, (
        f"Smoke subprocess exited {completed.returncode}: {completed.stderr}"
    )
    errors = eval(completed.stdout.strip())  # noqa: S307 — test fixture
    assert errors == [], (
        "Healthy slim install where hindsight_api exposes "
        "LocalSTEmbeddings must report no errors. The smoke helper "
        "must probe the API module specifically, not the embed manager "
        "(#81421 round-3 review). Got: " + repr(errors)
    )


def test_smoke_import_hindsight_local_ancient_api_only_imports_but_no_class(
    tmp_path,
):
    """Inverse of the healthy-path test: ancient hindsight_api that
    imports but no longer exposes LocalSTEmbeddings (the exact #81421
    backtrack signature) must be reported as a failure on the
    ``hindsight_api.LocalSTEmbeddings`` symbol.

    The helper now runs the probe in a clean subprocess (#81421 round-4),
    so we drive it through stub modules on a controlled PYTHONPATH."""
    import os
    import subprocess
    import sys

    stubs_dir = tmp_path / "stubs"
    stubs_dir.mkdir()
    (stubs_dir / "hindsight_api.py").write_text(
        "# Deliberately omit LocalSTEmbeddings to simulate the #81421 backtrack.\n"
    )
    (stubs_dir / "hindsight_embed.py").write_text("# stub\n")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(stubs_dir) + os.pathsep + env.get("PYTHONPATH", "")

    script = (
        "import importlib.util\n"
        "spec = importlib.util.spec_from_file_location('memory_setup_under_test', "
        f"{repr(str(Path(memory_setup.__file__).resolve()))})\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "errors = mod._smoke_import_hindsight_local()\n"
        "print(repr(errors))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, (
        f"Smoke subprocess exited {completed.returncode}: {completed.stderr}"
    )
    errors = eval(completed.stdout.strip())  # noqa: S307 — test fixture
    assert any(
        "LocalSTEmbeddings" in e and "ONNX embeddings provider" in e
        for e in errors
    ), (
        "The smoke check must report the missing LocalSTEmbeddings symbol "
        "on the hindsight_api module specifically when that class is "
        "absent — that's the exact #81421 backtrack failure signature."
    )
    assert not any("hindsight_embed.LocalSTEmbeddings" in e for e in errors), (
        "The smoke check must attribute the missing-provider failure to "
        "hindsight_api, not hindsight_embed — the latter doesn't ship "
        "provider classes."
    )


def test_intel_macos_non_force_stale_runtime_still_validated(
    tmp_path, monkeypatch, capsys
):
    """On Intel macOS local_embedded, a non-force refresh where every slim
    dep already imports must still run the smoke check. Without this, an
    ancient shadowing release of ``hindsight_api`` that still imports but
    no longer exposes ``LocalSTEmbeddings`` would be classified as
    "everything present, nothing to do" by the missing-dep probe, the
    reinstall would be skipped, and the broken state would silently
    persist (the exact failure mode #81421 was filed against)."""
    import pytest
    import yaml as _yaml

    plugin_dir = tmp_path / "hindsight"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text(
        _yaml.safe_dump({"pip_dependencies": ["hindsight-client>=0.6.1"]}),
        encoding="utf-8",
    )

    config_path = tmp_path / "hindsight" / "config.json"
    config_path.write_text('{"mode": "local_embedded"}', encoding="utf-8")

    monkeypatch.setattr(
        "plugins.memory.find_provider_dir", lambda name: plugin_dir
    )
    monkeypatch.setattr(memory_setup, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(memory_setup, "platform", _fake_platform("Darwin", "x86_64"))

    # Simulate the pre-existing broken environment: an ancient
    # hindsight_api / hindsight_embed are importable in the host venv
    # (so the missing-dep probe at the top of _install_dependencies
    # would skip reinstall), but the configured embeddings class is
    # missing (the #81421 backtrack signature).
    def fake_smoke_import():
        return [
            "hindsight_api.LocalSTEmbeddings: AttributeError: the slim runtime "
            "shipped an API release that does not expose the configured ONNX "
            "embeddings provider — this is the #81421 backtrack failure mode"
        ]

    monkeypatch.setattr(memory_setup, "_smoke_import_hindsight_local", fake_smoke_import)

    # Even with force=False (no pip call at all), the smoke check must
    # still detect the stale runtime and raise. If the implementation
    # only ran the smoke check after `outcome.ok`, this test would
    # silently pass — and the broken environment would be preserved.
    with pytest.raises(RuntimeError) as excinfo:
        memory_setup._install_dependencies("hindsight", force=False)

    assert "Hindsight slim runtime smoke validation failed" in str(excinfo.value)


def test_intel_macos_non_force_healthy_runtime_is_silent(
    tmp_path, monkeypatch, capsys
):
    """The inverse of the stale-runtime test: on Intel macOS local_embedded
    with force=False, a healthy runtime must NOT raise and must not print
    any smoke-validation output. The smoke check is reachable but silent
    when everything is in order."""
    import yaml as _yaml

    plugin_dir = tmp_path / "hindsight"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text(
        _yaml.safe_dump({"pip_dependencies": ["hindsight-client>=0.6.1"]}),
        encoding="utf-8",
    )

    config_path = tmp_path / "hindsight" / "config.json"
    config_path.write_text('{"mode": "local_embedded"}', encoding="utf-8")

    monkeypatch.setattr(
        "plugins.memory.find_provider_dir", lambda name: plugin_dir
    )
    monkeypatch.setattr(memory_setup, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(memory_setup, "platform", _fake_platform("Darwin", "x86_64"))

    monkeypatch.setattr(memory_setup, "_smoke_import_hindsight_local", lambda: [])

    install_calls = []

    def fake_install_specs(specs, timeout=120):
        install_calls.append(list(specs))
        return SimpleNamespace(ok=True, blocked=False, reason="", stderr="")

    monkeypatch.setattr("tools.lazy_deps.install_specs", fake_install_specs)

    # Must not raise.
    memory_setup._install_dependencies("hindsight", force=False)

    assert not install_calls, (
        "force=False on a healthy Intel macOS setup must not trigger pip — "
        "all slim packages must resolve through _IMPORT_NAMES."
    )

    captured = capsys.readouterr().out
    assert "smoke validation failed" not in captured


def test_hindsight_apple_silicon_install_does_not_smoke_check(tmp_path, monkeypatch):
    """On Apple Silicon the install path uses the full runtime, not the
    slim stack — the smoke check is Intel-macOS-specific and must not
    trigger on other platforms (it would impose an import cost on every
    refresh of healthy setups)."""
    import yaml as _yaml

    plugin_dir = tmp_path / "hindsight"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text(
        _yaml.safe_dump({"pip_dependencies": ["hindsight-client>=0.6.1"]}),
        encoding="utf-8",
    )

    config_path = tmp_path / "hindsight" / "config.json"
    config_path.write_text('{"mode": "local_embedded"}', encoding="utf-8")

    monkeypatch.setattr(
        "plugins.memory.find_provider_dir", lambda name: plugin_dir
    )
    monkeypatch.setattr(memory_setup, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(memory_setup, "platform", _fake_platform("Darwin", "arm64"))

    install_calls = []

    def fake_install_specs(specs, timeout=120):
        install_calls.append(list(specs))
        return SimpleNamespace(ok=True, blocked=False, reason="", stderr="")

    monkeypatch.setattr("tools.lazy_deps.install_specs", fake_install_specs)

    # If the smoke check fired on Apple Silicon, _smoke_import_hindsight_local
    # would be called. We patch it to blow up so the test fails loudly if the
    # smoke check ever runs on a non-Intel-macOS code path.
    monkeypatch.setattr(
        memory_setup,
        "_smoke_import_hindsight_local",
        lambda: (_ for _ in ()).throw(
            AssertionError(
                "smoke check must not run on Apple Silicon — it's Intel-macOS-only"
            )
        ),
    )

    memory_setup._install_dependencies("hindsight", force=True)

    assert install_calls, "force=True must still reach the install step on Apple Silicon"
    assert "hindsight-all" in install_calls[0], (
        "Apple Silicon must keep requesting the full `hindsight-all` runtime."
    )
