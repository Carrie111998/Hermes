"""Contract tests for the native-Anthropic interrupt-test gate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import cast

import pytest

from tests.conftest import _looks_like_credential
from tests.run_agent import anthropic_test_gate as gate


def _write_config(home: Path, text: str) -> None:
    (home / "config.yaml").write_text(text, encoding="utf-8")


def _write_mapping(home: Path, config: dict) -> None:
    import yaml

    _write_config(home, yaml.safe_dump(config, sort_keys=False))


def _route_config(source: str, provider: str, provider_blocks: dict) -> dict:
    config = {
        "model": {
            "provider": provider if source == "primary" else "openrouter",
            "default": "claude-sonnet-4.6" if source == "primary" else "openai/gpt-5.6",
        },
        "providers": provider_blocks,
    }
    if source == "fallback":
        config["fallback_providers"] = [
            {"provider": provider, "model": "claude-sonnet-4.6"}
        ]
    return config


def _probe_runtime_route(monkeypatch, config: dict, source: str, raw_provider: str) -> dict:
    """Exercise runtime's raw enabled lookup without resolving credentials."""
    from hermes_cli import config as config_module
    from hermes_cli import runtime_provider

    monkeypatch.setattr(config_module, "load_config", lambda: config)
    monkeypatch.setattr(runtime_provider, "_get_model_config", lambda: config["model"])
    monkeypatch.setattr(
        runtime_provider,
        "_resolve_named_custom_runtime",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        runtime_provider,
        "_resolve_explicit_runtime",
        lambda *, provider, requested_provider, **_kwargs: {
            "provider": provider,
            "requested_provider": requested_provider,
            "source": "test",
        },
    )
    return runtime_provider.resolve_runtime_provider(
        requested=None if source == "primary" else raw_provider,
        explicit_api_key="x",
    )


@pytest.fixture
def hermes_root(tmp_path, monkeypatch) -> Path:
    """Use the real Hermes root/profile path helpers against a temp estate."""
    import hermes_constants

    root = tmp_path / "hermes-root"
    root.mkdir()
    monkeypatch.setattr(
        hermes_constants,
        "_get_platform_default_hermes_home",
        lambda: root,
    )
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)
    return root


def test_legacy_root_native_provider_is_effective_and_fails(hermes_root, monkeypatch):
    """Legacy root keys select native Anthropic after canonical normalization."""
    _write_config(
        hermes_root,
        "provider: anthropic\n"
        "model: claude-sonnet-4.6\n",
    )
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.FAIL


def test_env_expanded_native_primary_is_effective_and_fails(hermes_root, monkeypatch):
    """Provider selection sees config expansion before testing the native route."""
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: ${D2_C03_NATIVE_PRIMARY}\n"
        "  default: claude-sonnet-4.6\n",
    )
    monkeypatch.setenv("D2_C03_NATIVE_PRIMARY", "claude")
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.FAIL


def test_env_expanded_native_fallback_is_effective_and_fails(hermes_root, monkeypatch):
    """Fallback provider entries receive the same config expansion treatment."""
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: openrouter\n"
        "  default: openai/gpt-5.6\n"
        "fallback_providers:\n"
        "  - provider: ${D2_C03_NATIVE_FALLBACK}\n"
        "    model: claude-sonnet-4.6\n",
    )
    monkeypatch.setenv("D2_C03_NATIVE_FALLBACK", "claude-code")
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.FAIL


@pytest.mark.parametrize(
    ("managed_config", "expected_source"),
    [
        (
            "model:\n"
            "  provider: anthropic\n"
            "  default: claude-sonnet-4.6\n",
            "primary",
        ),
        (
            "fallback_providers:\n"
            "  - provider: anthropic\n"
            "    model: claude-sonnet-4.6\n",
            "fallback",
        ),
    ],
)
def test_explicit_managed_scope_native_route_fails(
    hermes_root, monkeypatch, managed_config, expected_source
):
    """An explicit managed overlay is part of every profile's effective route."""
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: openrouter\n"
        "  default: openai/gpt-5.6\n",
    )
    managed_dir = hermes_root.parent / "managed-scope"
    managed_dir.mkdir()
    _write_config(managed_dir, managed_config)
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
    from hermes_cli import managed_scope

    managed_scope.invalidate_managed_cache()
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.FAIL
    assert gate.find_native_anthropic_selections()[0].source == expected_source


@pytest.mark.parametrize("failure", ("malformed", "unreadable", "unstatable"))
def test_explicit_managed_scope_failure_fails_gate_without_secret_diagnostic(
    hermes_root, monkeypatch, failure
):
    """A selected managed scope cannot fail open during the audit."""
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: openrouter\n"
        "  default: openai/gpt-5.6\n",
    )
    managed_dir = hermes_root.parent / "managed-scope"
    managed_dir.mkdir()
    managed_config = managed_dir / "config.yaml"
    secret = f"d2-c03-managed-{failure}-secret"
    if failure == "malformed":
        managed_config.write_text(
            "model: [anthropic\n" f"secret: {secret}\n",
            encoding="utf-8",
        )
    else:
        managed_config.write_text(
            "model:\n"
            "  provider: anthropic\n"
            "  default: claude-sonnet-4.6\n",
            encoding="utf-8",
        )
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
    from hermes_cli import managed_scope

    managed_scope.invalidate_managed_cache()
    if failure == "unreadable":
        import builtins

        real_open = builtins.open

        def deny_managed_config(path, *args, **kwargs):
            if Path(path) == managed_config:
                raise PermissionError(secret)
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", deny_managed_config)
    elif failure == "unstatable":
        real_stat = Path.stat

        def deny_managed_dir(path, *args, **kwargs):
            if path == managed_dir:
                raise PermissionError(secret)
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", deny_managed_dir)
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.FAIL
    assert decision.reason == gate._CONFIG_INSPECTION_FAILURE_REASON
    assert secret not in decision.reason


def test_inference_provider_env_fallback_is_effective_and_fails(hermes_root, monkeypatch):
    """The runtime provider override applies only after no config provider exists."""
    _write_config(
        hermes_root,
        "model:\n"
        "  default: claude-sonnet-4.6\n",
    )
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "claude-code")
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.FAIL


def test_sdk_absent_without_native_configuration_skips(hermes_root, monkeypatch):
    """A non-native Claude model route does not require the native SDK."""
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: openrouter\n"
        "  default: anthropic/claude-sonnet-4.6\n"
        "fallback_providers:\n"
        "  - provider: openrouter\n"
        "    model: anthropic/claude-opus-4.6\n",
    )
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.SKIP
    assert "Anthropic SDK is not installed" in decision.reason
    with pytest.raises(pytest.skip.Exception, match="no active Hermes configuration"):
        gate.enforce_native_anthropic_test_gate()


def test_sdk_absent_with_native_primary_configuration_fails(hermes_root, monkeypatch):
    """An explicit native primary must not be hidden behind a conditional skip."""
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: anthropic\n"
        "  default: claude-sonnet-4.6\n",
    )
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.FAIL
    assert "restore the supported anthropic sdk" in decision.reason.casefold()
    with pytest.raises(pytest.fail.Exception, match="native Anthropic provider"):
        gate.enforce_native_anthropic_test_gate()


def test_sdk_absent_with_native_fallback_configuration_fails(hermes_root, monkeypatch):
    """An explicit native fallback also requires the optional SDK."""
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: openrouter\n"
        "  default: gpt-5.6-terra\n"
        "fallback_providers:\n"
        "  - provider: anthropic\n"
        "    model: claude-sonnet-4.6\n",
    )
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.FAIL
    assert "fallback" in decision.reason
    with pytest.raises(
        pytest.fail.Exception,
        match="(?i)restore the supported anthropic sdk",
    ):
        gate.enforce_native_anthropic_test_gate()


def test_detects_native_anthropic_in_ordered_fallback_entries(hermes_root):
    """Fallback selection follows the shared chain's ordered provider entries."""
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: openai-codex\n"
        "  default: gpt-5.6-terra\n"
        "fallback_providers:\n"
        "  - provider: openrouter\n"
        "    model: anthropic/claude-sonnet-4.6\n"
        "  - provider: anthropic\n"
        "    model: claude-sonnet-4.6\n"
        "  - provider: claude-code\n"
        "    model: claude-opus-4.6\n"
        "fallback_model:\n"
        "  provider: anthropic\n"
        "  model: claude-haiku-4.5\n",
    )

    selections = gate.find_native_anthropic_selections()

    assert [
        (selection.scope, selection.source, selection.fallback_index, selection.provider)
        for selection in selections
    ] == [
        ("default", "fallback", 1, "anthropic"),
        ("default", "fallback", 2, "claude-code"),
        ("default", "fallback", 3, "anthropic"),
    ]


def test_profile_enumeration_excludes_default_hidden_and_invalid_directories(hermes_root, monkeypatch):
    """The gate uses the same active-profile taxonomy as the gateway."""
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: openrouter\n"
        "  default: anthropic/claude-sonnet-4.6\n",
    )
    profiles_root = hermes_root / "profiles"
    for name in ("default", ".quarantine", "not a valid profile"):
        profile = profiles_root / name
        profile.mkdir(parents=True)
        _write_config(
            profile,
            "model:\n"
            "  provider: anthropic\n"
            "  default: claude-sonnet-4.6\n",
        )
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.SKIP


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_profile_enumeration_follows_a_valid_named_symlink(hermes_root):
    """A valid named profile symlink is intentionally followed, like production."""
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: openrouter\n"
        "  default: anthropic/claude-sonnet-4.6\n",
    )
    target = hermes_root.parent / "linked-profile-target"
    target.mkdir()
    _write_config(
        target,
        "model:\n"
        "  provider: anthropic\n"
        "  default: claude-sonnet-4.6\n",
    )
    link = hermes_root / "profiles" / "operator"
    link.parent.mkdir()
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    selections = gate.find_native_anthropic_selections()

    assert [(selection.scope, selection.source) for selection in selections] == [
        ("profile:operator", "primary"),
    ]


def test_scans_active_profile_directories_but_ignores_retired_ones(hermes_root):
    """Only the canonical profiles root contributes to the active taxonomy."""
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: openrouter\n"
        "  default: anthropic/claude-sonnet-4.6\n",
    )
    active_profile = hermes_root / "profiles" / "operator"
    active_profile.mkdir(parents=True)
    _write_config(
        active_profile,
        "model:\n"
        "  provider: anthropic\n"
        "  default: claude-sonnet-4.6\n",
    )
    retired_profile = hermes_root / "quarantine" / "operator"
    retired_profile.mkdir(parents=True)
    _write_config(
        retired_profile,
        "model:\n"
        "  provider: anthropic\n"
        "  default: claude-sonnet-4.6\n",
    )

    selections = gate.find_native_anthropic_selections()

    assert [(selection.scope, selection.source) for selection in selections] == [
        ("profile:operator", "primary"),
    ]


def test_sdk_present_runs_without_reading_configuration(monkeypatch):
    """The installed SDK is decisive and leaves configuration untouched."""
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: True)
    monkeypatch.setattr(
        gate,
        "find_native_anthropic_selections",
        lambda: pytest.fail("SDK-present gate must not scan configuration"),
    )

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.RUN


def _run_secret_prompt_import_state_regressions(tmp_path: Path) -> None:
    """Exercise the real target import lock and every cleanup exit in a child."""
    project_root = Path(__file__).resolve().parents[2]
    child_root = tmp_path / "secret-prompt-import-state-child"
    child_home = child_root / "home"
    child_hermes_home = child_root / "hermes-home"
    child_tmpdir = child_root / "tmp"
    for directory in (child_root, child_home, child_hermes_home, child_tmpdir):
        directory.mkdir()
    script_path = child_root / "secret_prompt_import_state_regression.py"
    script_path.write_text(
        r'''from __future__ import annotations

import asyncio
import importlib
import importlib.abc
import importlib.machinery
import json
import sys
import threading
import time
from importlib import _bootstrap

import hermes_cli
from tests.run_agent import anthropic_test_gate as gate


TARGET = "hermes_cli.secret_prompt"
CONFIG = "hermes_cli.config"
MISSING = object()


def module_state(name):
    return (name in sys.modules, sys.modules.get(name))


def parent_state(name):
    namespace = vars(hermes_cli)
    return (name in namespace, namespace.get(name))


def restore_module(name, state):
    present, value = state
    if present:
        sys.modules[name] = value
    else:
        sys.modules.pop(name, None)


def restore_parent(name, state):
    present, value = state
    if present:
        setattr(hermes_cli, name, value)
    else:
        vars(hermes_cli).pop(name, None)


def clear_target_and_config():
    sys.modules.pop(TARGET, None)
    sys.modules.pop(CONFIG, None)
    vars(hermes_cli).pop("secret_prompt", None)
    vars(hermes_cli).pop("config", None)


def wait_for(predicate, label):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.001)
    raise AssertionError("timed out waiting for " + label)


saved_target = module_state(TARGET)
saved_config = module_state(CONFIG)
saved_target_parent = parent_state("secret_prompt")
saved_config_parent = parent_state("config")
saved_import_module = importlib.import_module
saved_module_type = gate.ModuleType
saved_meta_path = list(sys.meta_path)
release_factory = threading.Event()
threads = []
results = {}

try:
    # Forced interleaving: pause helper stub construction after its absence
    # decision. A correct helper already owns the actual target-module import
    # lock here, so a simultaneous real import must wait rather than becoming
    # detached from the package parent or observing the temporary stub.
    clear_target_and_config()
    target_lock = _bootstrap._get_module_lock(TARGET)
    factory_entered = threading.Event()
    real_module_type = gate.ModuleType

    def blocking_module_type(name):
        factory_entered.set()
        if not release_factory.wait(5):
            raise AssertionError("stub factory was not released")
        return real_module_type(name)

    gate.ModuleType = blocking_module_type
    prompt_calls = []

    class ControlledPromptLoader(importlib.abc.Loader):
        def exec_module(self, module):
            module.import_kind = "real-import"

            def masked_secret_prompt(prompt, *, mask="*"):
                prompt_calls.append((prompt, mask))
                return "real-prompt-result"

            module.masked_secret_prompt = masked_secret_prompt

    class ControlledPromptFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if fullname == TARGET:
                return importlib.machinery.ModuleSpec(
                    fullname, ControlledPromptLoader()
                )
            return None

    finder = ControlledPromptFinder()
    sys.meta_path.insert(0, finder)
    helper_result = {}

    def call_helper():
        try:
            helper_result["helpers"] = gate._load_config_audit_helpers()
        except BaseException as exc:
            helper_result["error"] = exc

    helper_thread = threading.Thread(target=call_helper, daemon=True)
    threads.append(helper_thread)
    helper_thread.start()
    assert factory_entered.wait(5), "helper never reached stub construction"
    assert target_lock.owner == helper_thread.ident

    importer_result = {}

    def import_real_target():
        try:
            importer_result["module"] = saved_import_module(TARGET)
        except BaseException as exc:
            importer_result["error"] = exc

    importer_thread = threading.Thread(target=import_real_target, daemon=True)
    threads.append(importer_thread)
    importer_thread.start()
    wait_for(lambda: target_lock.waiters >= 1, "real importer to wait on target lock")
    assert importer_thread.is_alive()
    assert "module" not in importer_result

    release_factory.set()
    helper_thread.join(5)
    importer_thread.join(5)
    assert not helper_thread.is_alive()
    assert not importer_thread.is_alive()
    assert "error" not in helper_result, repr(helper_result.get("error"))
    assert "error" not in importer_result, repr(importer_result.get("error"))
    imported = importer_result["module"]
    assert getattr(imported, "import_kind", None) == "real-import"
    assert sys.modules.get(TARGET) is imported
    assert getattr(hermes_cli, "secret_prompt", MISSING) is imported
    config_module = sys.modules[CONFIG]
    assert config_module.masked_secret_prompt("forced", mask="#") == "real-prompt-result"
    assert prompt_calls == [("forced", "#")]
    results["forced_interleaving"] = True
    results["actual_target_lock"] = True
    results["concurrent_real_import"] = True
    results["deferred_prompt_after_interleaving"] = True

    # A coherent preloaded real target is never replaced, and config receives
    # the original prompt callable rather than the deferred audit shim.
    sys.modules.pop(CONFIG, None)
    vars(hermes_cli).pop("config", None)

    def forbid_stub(_name):
        raise AssertionError("preloaded real module must not be stubbed")

    gate.ModuleType = forbid_stub
    gate._load_config_audit_helpers()
    preloaded_config = sys.modules[CONFIG]
    assert sys.modules.get(TARGET) is imported
    assert getattr(hermes_cli, "secret_prompt", MISSING) is imported
    assert preloaded_config.masked_secret_prompt is imported.masked_secret_prompt
    assert preloaded_config.masked_secret_prompt("preloaded", mask="+") == "real-prompt-result"
    assert prompt_calls[-1] == ("preloaded", "+")
    results["preloaded_real_module"] = True
    results["preloaded_prompt_semantics"] = True

    # With the target genuinely absent, success removes the temporary stub and
    # parent attribute. The bound config callable defers to the real prompt at
    # call time and forwards arguments unchanged.
    gate.ModuleType = saved_module_type
    sys.meta_path.remove(finder)
    clear_target_and_config()
    gate._load_config_audit_helpers()
    absent_config = sys.modules[CONFIG]
    assert TARGET not in sys.modules
    assert "secret_prompt" not in vars(hermes_cli)
    real_prompt_module = saved_import_module(TARGET)
    original_prompt = real_prompt_module.masked_secret_prompt
    deferred_calls = []

    def fake_real_prompt(prompt, *, mask="*"):
        deferred_calls.append((prompt, mask))
        return "deferred-real-result"

    real_prompt_module.masked_secret_prompt = fake_real_prompt
    try:
        assert absent_config.masked_secret_prompt("deferred", mask="@") == (
            "deferred-real-result"
        )
    finally:
        real_prompt_module.masked_secret_prompt = original_prompt
    assert deferred_calls == [("deferred", "@")]
    results["absent_module_cleanup"] = True
    results["deferred_real_prompt_semantics"] = True

    # Success preserves an orphan parent attribute exactly rather than leaking
    # the temporary module or silently deleting unrelated prior state.
    clear_target_and_config()
    parent_sentinel = object()
    setattr(hermes_cli, "secret_prompt", parent_sentinel)
    gate._load_config_audit_helpers()
    assert TARGET not in sys.modules
    assert getattr(hermes_cli, "secret_prompt", MISSING) is parent_sentinel
    results["orphan_parent_success_cleanup"] = True

    # Every BaseException class required by the review restores exact absent
    # module + pre-existing parent state while the target lock is still held.
    failure_types = (RuntimeError, KeyboardInterrupt, asyncio.CancelledError)
    for failure_type in failure_types:
        clear_target_and_config()
        parent_sentinel = object()
        setattr(hermes_cli, "secret_prompt", parent_sentinel)
        failure = failure_type("controlled config import failure")

        def failing_import(name, package=None, *, _failure=failure):
            if name == CONFIG:
                raise _failure
            return saved_import_module(name, package)

        importlib.import_module = failing_import
        try:
            try:
                gate._load_config_audit_helpers()
            except BaseException as caught:
                assert caught is failure
            else:
                raise AssertionError("controlled import failure did not propagate")
        finally:
            importlib.import_module = saved_import_module
        assert TARGET not in sys.modules
        assert getattr(hermes_cli, "secret_prompt", MISSING) is parent_sentinel
        assert CONFIG not in sys.modules
        results[failure_type.__name__ + "_cleanup"] = True

    # A preloaded target plus a missing parent attribute is also restored
    # exactly when config import fails.
    clear_target_and_config()
    preloaded = saved_import_module(TARGET)
    vars(hermes_cli).pop("secret_prompt", None)
    preloaded_failure = RuntimeError("controlled preloaded import failure")

    def fail_preloaded_config(name, package=None):
        if name == CONFIG:
            raise preloaded_failure
        return saved_import_module(name, package)

    importlib.import_module = fail_preloaded_config
    try:
        try:
            gate._load_config_audit_helpers()
        except RuntimeError as caught:
            assert caught is preloaded_failure
        else:
            raise AssertionError("preloaded import failure did not propagate")
    finally:
        importlib.import_module = saved_import_module
    assert sys.modules.get(TARGET) is preloaded
    assert "secret_prompt" not in vars(hermes_cli)
    results["preloaded_failure_cleanup"] = True
finally:
    release_factory.set()
    importlib.import_module = saved_import_module
    gate.ModuleType = saved_module_type
    sys.meta_path[:] = saved_meta_path
    for thread in threads:
        thread.join(5)
    restore_module(TARGET, saved_target)
    restore_module(CONFIG, saved_config)
    restore_parent("secret_prompt", saved_target_parent)
    restore_parent("config", saved_config_parent)

required = {
    "forced_interleaving",
    "actual_target_lock",
    "concurrent_real_import",
    "deferred_prompt_after_interleaving",
    "preloaded_real_module",
    "preloaded_prompt_semantics",
    "absent_module_cleanup",
    "deferred_real_prompt_semantics",
    "orphan_parent_success_cleanup",
    "RuntimeError_cleanup",
    "KeyboardInterrupt_cleanup",
    "CancelledError_cleanup",
    "preloaded_failure_cleanup",
}
assert set(results) == required
assert all(results.values())
print(json.dumps(results, sort_keys=True))
''',
        encoding="utf-8",
    )
    child_env = {
        "HERMES_HOME": str(child_hermes_home),
        "HOME": str(child_home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.environ["PATH"],
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONPATH": str(project_root),
        "TMPDIR": str(child_tmpdir),
        "TZ": "UTC",
    }
    assert not any(_looks_like_credential(name) for name in child_env)
    result = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=project_root,
        env=child_env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    child_output = result.stdout + result.stderr
    assert result.returncode == 0, child_output
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert len(payload) == 13
    assert all(payload.values())


def test_probe_is_read_only_and_never_loads_a_credential_pool(hermes_root, monkeypatch):
    """The config diagnostic neither seeds files nor resolves provider auth."""
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: anthropic\n"
        "  default: claude-sonnet-4.6\n",
    )
    import agent.credential_pool as credential_pool

    monkeypatch.setattr(
        credential_pool,
        "load_pool",
        lambda *_args, **_kwargs: pytest.fail("gate must not load auth pools"),
    )
    before = {
        path.relative_to(hermes_root): path.read_bytes()
        for path in hermes_root.rglob("*")
        if path.is_file()
    }

    selections = gate.find_native_anthropic_selections()

    after = {
        path.relative_to(hermes_root): path.read_bytes()
        for path in hermes_root.rglob("*")
        if path.is_file()
    }
    assert selections[0].source == "primary"
    assert after == before
    assert not (hermes_root / "auth.json").exists()
    assert not (hermes_root / "SOUL.md").exists()
    _run_secret_prompt_import_state_regressions(hermes_root.parent)


@pytest.mark.parametrize("source", ("primary", "fallback"))
@pytest.mark.parametrize(
    ("case", "raw_provider", "provider_blocks", "runtime_allows"),
    [
        pytest.param(
            "raw canonical disabled",
            "anthropic",
            {"anthropic": {"enabled": False}},
            False,
            id="raw-canonical-disabled",
        ),
        pytest.param(
            "raw canonical with alias-only disabled block",
            "anthropic",
            {"claude": {"enabled": False}},
            True,
            id="raw-canonical-alias-only",
        ),
        pytest.param(
            "raw claude alias disabled",
            "claude",
            {"claude": {"enabled": "false"}},
            False,
            id="raw-claude-disabled",
        ),
        pytest.param(
            "raw claude-code alias disabled",
            "claude-code",
            {"claude-code": {"enabled": "off"}},
            False,
            id="raw-claude-code-disabled",
        ),
        pytest.param(
            "raw claude with canonical-only disabled block",
            "claude",
            {"anthropic": {"enabled": False}},
            True,
            id="raw-claude-canonical-only",
        ),
        pytest.param(
            "raw claude-code with canonical-only disabled block",
            "claude-code",
            {"anthropic": {"enabled": False}},
            True,
            id="raw-claude-code-canonical-only",
        ),
        pytest.param(
            "raw claude with other-alias-only disabled block",
            "claude",
            {"claude-code": {"enabled": False}},
            True,
            id="raw-claude-other-alias-only",
        ),
        pytest.param(
            "raw claude-code with other-alias-only disabled block",
            "claude-code",
            {"claude": {"enabled": False}},
            True,
            id="raw-claude-code-other-alias-only",
        ),
        pytest.param(
            "raw disabled wins over canonical enabled",
            "claude",
            {"claude": {"enabled": False}, "anthropic": {"enabled": True}},
            False,
            id="raw-disabled-canonical-enabled",
        ),
        pytest.param(
            "raw enabled wins over canonical disabled",
            "claude",
            {"claude": {"enabled": True}, "anthropic": {"enabled": False}},
            True,
            id="raw-enabled-canonical-disabled",
        ),
        pytest.param(
            "raw claude-code disabled wins over canonical enabled",
            "claude-code",
            {"claude-code": {"enabled": False}, "anthropic": {"enabled": True}},
            False,
            id="raw-claude-code-disabled-canonical-enabled",
        ),
        pytest.param(
            "raw claude-code enabled wins over canonical disabled",
            "claude-code",
            {"claude-code": {"enabled": True}, "anthropic": {"enabled": False}},
            True,
            id="raw-claude-code-enabled-canonical-disabled",
        ),
    ],
)
def test_native_gate_matches_runtime_raw_requested_provider_enabled_lookup(
    hermes_root,
    monkeypatch,
    source,
    case,
    raw_provider,
    provider_blocks,
    runtime_allows,
):
    """The gate checks the same raw provider block as production runtime."""
    config = _route_config(source, raw_provider, provider_blocks)
    _write_mapping(hermes_root, config)
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    if runtime_allows:
        runtime = _probe_runtime_route(monkeypatch, config, source, raw_provider)
        assert runtime["requested_provider"] == raw_provider, case
        assert runtime["provider"] == "anthropic", case
    else:
        with pytest.raises(ValueError, match=raw_provider):
            _probe_runtime_route(monkeypatch, config, source, raw_provider)

    decision = gate.decide_native_anthropic_test_gate()

    assert (decision.action is gate.GateAction.FAIL) is runtime_allows, case


def test_malformed_config_fails_closed_without_secret_diagnostic(hermes_root, monkeypatch):
    """A parse failure cannot turn an unknown native route into a skip."""
    secret = "d2-c03-malformed-secret"
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: [anthropic\n"
        f"  api_key: {secret}\n",
    )
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.FAIL
    assert "could not safely inspect active Hermes configuration" in decision.reason
    assert secret not in decision.reason
    with pytest.raises(gate.NativeAnthropicGateInspectionError) as inspection:
        gate.find_native_anthropic_selections()
    assert inspection.value.__cause__ is None
    assert secret not in str(inspection.value)

    with pytest.raises(pytest.fail.Exception) as failure:
        gate.enforce_native_anthropic_test_gate()
    assert secret not in str(failure.value)


def test_unreadable_config_fails_closed_without_secret_diagnostic(hermes_root, monkeypatch):
    """An I/O error uses the same non-secret, fail-closed gate decision."""
    config_path = hermes_root / "config.yaml"
    secret = "d2-c03-unreadable-secret"
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: openrouter\n"
        f"  api_key: {secret}\n",
    )
    import builtins

    real_open = builtins.open

    def deny_gate_config(path, *args, **kwargs):
        if Path(path) == config_path:
            raise PermissionError(secret)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", deny_gate_config)
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.FAIL
    assert "could not safely inspect active Hermes configuration" in decision.reason
    assert secret not in decision.reason


def test_unreadable_profiles_root_fails_closed_without_secret_diagnostic(hermes_root, monkeypatch):
    """Canonical profile enumeration errors must not become a false skip."""
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: openrouter\n"
        "  default: openai/gpt-5.6\n",
    )
    profiles_root = hermes_root / "profiles"
    profiles_root.mkdir()
    secret = "d2-c03-profiles-secret"
    real_iterdir = Path.iterdir

    def deny_profiles_root(path):
        if path == profiles_root:
            raise PermissionError(secret)
        return real_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", deny_profiles_root)
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.FAIL
    assert "could not safely inspect active Hermes configuration" in decision.reason
    assert secret not in decision.reason


def test_unstatable_profiles_root_fails_closed_without_secret_diagnostic(
    hermes_root, monkeypatch
):
    """A permission error hidden by Path.is_dir() still must not become a skip."""
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: openrouter\n"
        "  default: openai/gpt-5.6\n",
    )
    profiles_root = hermes_root / "profiles"
    profiles_root.mkdir()
    secret = "d2-c03-unstatable-profiles-secret"
    real_stat = Path.stat

    def deny_profiles_stat(path, *args, **kwargs):
        if path == profiles_root:
            raise PermissionError(secret)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", deny_profiles_stat)
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.FAIL
    assert "could not safely inspect active Hermes configuration" in decision.reason
    assert secret not in decision.reason


def test_original_interrupt_test_honors_controlled_gate_states(monkeypatch):
    """The unchanged target obeys run, skip, and fail decisions before its body."""
    from unittest.mock import MagicMock

    from agent import anthropic_adapter
    from tests.run_agent.test_run_agent import TestAnthropicInterruptHandler

    # The controlled RUN state must execute the original behavioral assertions
    # without installing the optional SDK; its request client is mocked below.
    monkeypatch.setattr(anthropic_adapter, "build_anthropic_client", MagicMock())
    target = TestAnthropicInterruptHandler().test_interruptible_anthropic_interrupt_never_closes_shared_client

    monkeypatch.setattr(gate, "enforce_native_anthropic_test_gate", lambda *_args, **_kwargs: None)
    target()

    monkeypatch.setattr(
        gate,
        "enforce_native_anthropic_test_gate",
        lambda *_args, **_kwargs: pytest.skip("controlled native gate skip"),
    )
    with pytest.raises(pytest.skip.Exception, match="controlled native gate skip"):
        target()

    monkeypatch.setattr(
        gate,
        "enforce_native_anthropic_test_gate",
        lambda *_args, **_kwargs: pytest.fail("controlled native gate fail"),
    )
    with pytest.raises(pytest.fail.Exception, match="controlled native gate fail"):
        target()


def test_interrupt_target_exposes_immutable_collection_gate_snapshot():
    """The real interrupt target receives a decision captured at import time."""
    from tests.run_agent import test_run_agent as interrupt_tests

    snapshot = interrupt_tests._NATIVE_ANTHROPIC_INTERRUPT_GATE_DECISION

    assert isinstance(snapshot, gate.GateDecision)
    assert snapshot == gate.GateDecision(snapshot.action, snapshot.reason)
    with pytest.raises(FrozenInstanceError):
        setattr(snapshot, "reason", "must remain immutable")


def test_original_collection_environment_capture_is_exact_immutable_and_redacted():
    """Frozen path inputs never surface through repr, validation, or assertions."""
    from tests.collection_environment import OriginalCollectionEnvironment

    sentinel = f"/runtime-random-home-{uuid.uuid4().hex}"
    unset = OriginalCollectionEnvironment.capture({"HOME": sentinel})
    explicitly_empty = OriginalCollectionEnvironment.capture(
        {"HOME": sentinel, "HERMES_HOME": ""}
    )
    explicit = OriginalCollectionEnvironment.capture(
        {"HOME": f"{sentinel}/home", "HERMES_HOME": f"{sentinel}/hermes"}
    )

    assert unset.hermes_home_was_set is False
    assert unset.hermes_home is None
    assert unset.home == sentinel
    assert explicitly_empty.hermes_home_was_set is True
    assert explicitly_empty.hermes_home == ""
    with pytest.raises(FrozenInstanceError):
        setattr(unset, "home", "/rewritten")

    display_surfaces = [repr(unset), str(unset), repr(explicit), str(explicit)]
    different = OriginalCollectionEnvironment(
        hermes_home_was_set=True,
        hermes_home=f"{sentinel}/different-hermes",
        home=f"{sentinel}/different-home",
    )
    with pytest.raises(AssertionError) as assertion:
        assert explicit == different
    display_surfaces.extend((str(assertion.value), repr(assertion.value)))

    invalid_constructions = (
        {
            "hermes_home_was_set": False,
            "hermes_home": f"{sentinel}/unexpected",
            "home": f"{sentinel}/home",
        },
        {
            "hermes_home_was_set": cast(bool, sentinel),
            "hermes_home": f"{sentinel}/hermes",
            "home": f"{sentinel}/home",
        },
        {
            "hermes_home_was_set": True,
            "hermes_home": cast(str, object()),
            "home": f"{sentinel}/home",
        },
        {
            "hermes_home_was_set": True,
            "hermes_home": f"{sentinel}/hermes",
            "home": cast(str, object()),
        },
    )
    for kwargs in invalid_constructions:
        with pytest.raises((TypeError, ValueError)) as validation:
            OriginalCollectionEnvironment(**kwargs)
        display_surfaces.extend((str(validation.value), repr(validation.value)))

    with pytest.raises(TypeError) as dataclass_error:
        OriginalCollectionEnvironment(  # type: ignore[call-arg]
            True,
            f"{sentinel}/hermes",
            f"{sentinel}/home",
            f"{sentinel}/extra",
        )
    display_surfaces.extend((str(dataclass_error.value), repr(dataclass_error.value)))

    leaking_surface_indexes = [
        index for index, surface in enumerate(display_surfaces) if sentinel in surface
    ]
    assert leaking_surface_indexes == []


def test_gate_rejects_an_invalid_audit_snapshot_without_diagnostic_details():
    """Only a frozen GateDecision may bypass a recomputed audit."""
    with pytest.raises(pytest.fail.Exception, match="could not safely inspect"):
        gate.enforce_native_anthropic_test_gate(cast(gate.GateDecision, object()))


@pytest.mark.parametrize("estate", ("home-config", "provider-override"))
def test_pre_fixture_estate_snapshot_survives_real_hermetic_fixture(tmp_path, estate):
    """Collection captures native routing before the real autouse fixture mutates it."""
    if gate.native_anthropic_sdk_available():
        pytest.skip("pre-fixture regression requires the SDK to remain absent")

    project_root = Path(__file__).resolve().parents[2]
    pre_fixture_home = tmp_path / "pre-fixture-home"
    pre_fixture_home.mkdir()
    if estate == "home-config":
        _write_config(
            pre_fixture_home,
            "model:\n"
            "  provider: anthropic\n"
            "  default: claude-sonnet-4.6\n",
        )
    else:
        _write_config(
            pre_fixture_home,
            "model:\n"
            "  default: claude-sonnet-4.6\n",
        )
    managed_dir = tmp_path / "managed"
    managed_dir.mkdir()
    (managed_dir / "config.yaml").write_text("{}\n", encoding="utf-8")
    child_home = tmp_path / "child-home"
    child_home.mkdir()
    child_tmpdir = tmp_path / "prefixture-child-tmp"
    child_tmpdir.mkdir()
    child_test = tmp_path / "test_prefixture_gate.py"
    child_test.write_text(
        "from tests.run_agent.test_run_agent import TestAnthropicInterruptHandler\n"
        "\n"
        "def test_collection_snapshot_is_enforced_after_fixture_mutation():\n"
        "    TestAnthropicInterruptHandler().test_interruptible_anthropic_interrupt_never_closes_shared_client()\n",
        encoding="utf-8",
    )
    child_env = {
        "PATH": os.environ["PATH"],
        "HOME": str(child_home),
        "HERMES_HOME": str(pre_fixture_home),
        "HERMES_MANAGED_DIR": str(managed_dir),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONPATH": str(project_root),
        "TMPDIR": str(child_tmpdir),
        "TZ": "UTC",
    }
    if estate == "provider-override":
        child_env["HERMES_INFERENCE_PROVIDER"] = "claude"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.conftest",
            "-p",
            "no:cacheprovider",
            str(child_test),
            "-q",
            "-rs",
        ],
        cwd=project_root,
        env=child_env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    child_output = result.stdout + result.stderr

    assert result.returncode == 1, child_output
    assert "native Anthropic provider is selected" in child_output
    assert "skipped" not in child_output.lower()


_HISTORICAL_INTERRUPT_NODE = (
    "tests/run_agent/test_run_agent.py::TestAnthropicInterruptHandler::"
    "test_interruptible_anthropic_interrupt_never_closes_shared_client"
)
_CREDENTIAL_VALUE_TRAP = "d2-c03-credential-value-must-not-appear"


def _estate_snapshot(root: Path) -> dict[str, tuple[str, int, bytes | str | None]]:
    """Capture directory entries, modes, bytes, and symlink targets without writes."""
    snapshot: dict[str, tuple[str, int, bytes | str | None]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode & 0o777
        if path.is_symlink():
            snapshot[relative] = ("symlink", mode, os.readlink(path))
        elif path.is_dir():
            snapshot[relative] = ("directory", mode, None)
        else:
            snapshot[relative] = ("file", mode, path.read_bytes())
    return snapshot


def _write_child_gate_audit_plugin(plugin_dir: Path) -> None:
    """Instrument only the gate import/decision boundary in the child process."""
    (plugin_dir / "d2_c03_child_gate_audit.py").write_text(
        '''from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


_AUDIT_PATH = Path(os.environ["D2_C03_AUDIT_PATH"])
_CREDENTIAL_VALUE_TRAP = "d2-c03-credential-value-must-not-appear"


def _is_forbidden_gate_module(name: str) -> bool:
    if name == "anthropic" or name.startswith("anthropic."):
        return True
    if name == "tools.lazy_deps" or name.startswith("tools.lazy_deps."):
        return True
    if name == "agent.credential_pool" or name.startswith("agent.credential_pool."):
        return True
    if name == "hermes_cli.auth" or name.startswith("hermes_cli.auth."):
        return True
    if name.startswith(("agent.", "hermes_cli.", "tools.")):
        return any("secret" in part for part in name.split("."))
    return False


@contextmanager
def _deny_credential_environment_reads(conftest, reads: list[str]):
    env_type = type(os.environ)
    real_getenv = os.getenv
    real_get = env_type.get
    real_getitem = env_type.__getitem__

    def deny(name: object) -> None:
        key = str(name)
        if conftest._looks_like_credential(key):
            reads.append(key)
            raise AssertionError(
                "gate attempted to read a credential value: " + _CREDENTIAL_VALUE_TRAP
            )

    def guarded_getenv(name, default=None):
        deny(name)
        return real_getenv(name, default)

    def guarded_get(environ, name, default=None):
        deny(name)
        return real_get(environ, name, default)

    def guarded_getitem(environ, name):
        deny(name)
        return real_getitem(environ, name)

    with (
        patch.object(os, "getenv", guarded_getenv),
        patch.object(env_type, "get", guarded_get),
        patch.object(env_type, "__getitem__", guarded_getitem),
    ):
        yield


def pytest_configure(config):
    conftest = sys.modules.get("tests.conftest")
    if conftest is None:
        raise RuntimeError("repository tests/conftest.py was not loaded by pytest collection")

    credential_reads: list[str] = []
    modules_before_import = set(sys.modules)
    with _deny_credential_environment_reads(conftest, credential_reads):
        gate = importlib.import_module("tests.run_agent.anthropic_test_gate")
    gate_imports = set(sys.modules) - modules_before_import
    real_decide = gate.decide_native_anthropic_test_gate
    sandbox_home = os.environ.get("HERMES_HOME")

    def audited_decide(*args, **kwargs):
        modules_before_decision = set(sys.modules)
        with _deny_credential_environment_reads(conftest, credential_reads):
            decision = real_decide(*args, **kwargs)
        gate_imports.update(set(sys.modules) - modules_before_decision)

        snapshot = getattr(conftest, "ORIGINAL_COLLECTION_ENVIRONMENT", None)
        snapshot_payload = None
        if snapshot is not None:
            snapshot_payload = {
                "hermes_home_was_set": snapshot.hermes_home_was_set,
                "hermes_home_present": snapshot.hermes_home is not None,
                "home_present": snapshot.home is not None,
                "home_matches_process_home": snapshot.home == os.environ.get("HOME"),
            }
        payload = {
            "conftest_path": str(Path(conftest.__file__).resolve()),
            "credential_reads": sorted(set(credential_reads)),
            "decision": decision.action.value,
            "forbidden_gate_modules": sorted(
                name for name in gate_imports if _is_forbidden_gate_module(name)
            ),
            "original_collection_environment": snapshot_payload,
            "sandbox_home_present": sandbox_home is not None,
            "sandbox_home_restored_after_gate": (
                os.environ.get("HERMES_HOME") == sandbox_home
            ),
            "sandbox_home_is_not_original_default_home": bool(
                snapshot is not None
                and snapshot.home is not None
                and sandbox_home is not None
                and Path(sandbox_home).resolve()
                != (Path(snapshot.home) / ".hermes").resolve()
            ),
            "sdk_available": importlib.util.find_spec("anthropic") is not None,
        }
        _AUDIT_PATH.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return decision

    gate.decide_native_anthropic_test_gate = audited_decide
''',
        encoding="utf-8",
    )


def _run_unset_hermes_home_child(
    tmp_path: Path,
    *,
    config_text: str,
) -> tuple[subprocess.CompletedProcess[str], dict, dict, dict]:
    project_root = Path(__file__).resolve().parents[2]
    child_home = tmp_path / "unset-hermes-home-child"
    native_estate = child_home / ".hermes"
    native_estate.mkdir(parents=True)
    _write_config(native_estate, config_text)
    managed_dir = tmp_path / "managed"
    managed_dir.mkdir()
    _write_config(managed_dir, "{}\n")
    plugin_dir = tmp_path / "audit-plugin"
    plugin_dir.mkdir()
    _write_child_gate_audit_plugin(plugin_dir)
    audit_path = tmp_path / "gate-audit.json"
    child_tmpdir = tmp_path / "unset-home-child-tmp"
    child_tmpdir.mkdir()

    estate_before = _estate_snapshot(native_estate)
    managed_before = _estate_snapshot(managed_dir)
    child_env = {
        "D2_C03_AUDIT_PATH": str(audit_path),
        "HERMES_MANAGED_DIR": str(managed_dir),
        "HOME": str(child_home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.environ["PATH"],
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONPATH": os.pathsep.join((str(plugin_dir), str(project_root))),
        "TMPDIR": str(child_tmpdir),
        "TZ": "UTC",
    }
    assert "HERMES_HOME" not in child_env
    assert not any(_looks_like_credential(name) for name in child_env)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "d2_c03_child_gate_audit",
            "-p",
            "no:cacheprovider",
            _HISTORICAL_INTERRUPT_NODE,
            "-q",
            "-rs",
        ],
        cwd=project_root,
        env=child_env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    audit_text = audit_path.read_text(encoding="utf-8") if audit_path.exists() else ""
    assert str(child_home) not in audit_text
    audit = json.loads(audit_text) if audit_text else {}
    estate_after = _estate_snapshot(native_estate)
    managed_after = _estate_snapshot(managed_dir)
    assert estate_after == estate_before
    assert managed_after == managed_before
    return result, audit, estate_before, estate_after


def _assert_clean_child_gate_audit(
    audit: dict,
    *,
    child_home: Path,
    expected_decision: str,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    assert audit["conftest_path"] == str((project_root / "tests" / "conftest.py").resolve())
    assert audit["credential_reads"] == []
    assert audit["decision"] == expected_decision
    assert audit["forbidden_gate_modules"] == []
    assert audit["original_collection_environment"] == {
        "hermes_home_was_set": False,
        "hermes_home_present": False,
        "home_present": True,
        "home_matches_process_home": True,
    }
    assert audit["sandbox_home_present"] is True
    assert audit["sandbox_home_restored_after_gate"] is True
    assert audit["sandbox_home_is_not_original_default_home"] is True
    assert audit["sdk_available"] is False


def test_unset_hermes_home_native_default_home_fails_in_real_child_pytest(tmp_path):
    """Real conftest collection cannot hide a native HOME/.hermes route."""
    child_home = tmp_path / "unset-hermes-home-child"
    result, audit, _before, _after = _run_unset_hermes_home_child(
        tmp_path,
        config_text=(
            "model:\n"
            "  provider: anthropic\n"
            "  default: claude-sonnet-4.6\n"
        ),
    )
    child_output = result.stdout + result.stderr

    assert result.returncode == 1, child_output
    assert "native Anthropic provider is selected" in child_output
    assert "skipped" not in child_output.lower()
    assert _CREDENTIAL_VALUE_TRAP not in child_output
    _assert_clean_child_gate_audit(audit, child_home=child_home, expected_decision="fail")


def test_unset_hermes_home_non_native_default_home_skips_in_real_child_pytest(tmp_path):
    """SDK absence remains an explicit SKIP when HOME/.hermes has no native route."""
    child_home = tmp_path / "unset-hermes-home-child"
    result, audit, _before, _after = _run_unset_hermes_home_child(
        tmp_path,
        config_text=(
            "model:\n"
            "  provider: openrouter\n"
            "  default: anthropic/claude-sonnet-4.6\n"
        ),
    )
    child_output = result.stdout + result.stderr

    assert result.returncode == 0, child_output
    assert "1 skipped" in child_output
    assert "no active Hermes configuration selects the native Anthropic provider" in child_output
    assert _CREDENTIAL_VALUE_TRAP not in child_output
    _assert_clean_child_gate_audit(audit, child_home=child_home, expected_decision="skip")
