"""Regression contracts for the #41374 installer dependency floors."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from hermes_cli import _pip_security

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
UPDATE_CMD = REPO_ROOT / "hermes_cli" / "update_cmd.py"
MAIN_CMD = REPO_ROOT / "hermes_cli" / "main.py"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _load_voice_doctor():
    path = REPO_ROOT / "scripts" / "discord-voice-doctor.py"
    spec = importlib.util.spec_from_file_location("discord_voice_doctor", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _function_body(source: str, name: str) -> str:
    start = source.index(f"function {name}")
    brace = source.index("{", start)
    depth = 0
    quote = None
    in_comment = False
    index = brace
    while index < len(source):
        char = source[index]
        if in_comment:
            if char == "\n":
                in_comment = False
            index += 1
            continue
        if quote is not None:
            # PowerShell uses a backtick to escape the next character and
            # doubled single quotes to embed a quote in a literal.
            if char == "`":
                index += 2
                continue
            if char == quote:
                if quote == "'" and index + 1 < len(source) and source[index + 1] == "'":
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char == "#":
            in_comment = True
        elif char in {"'", '"'}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                body = source[brace : index + 1]
                assert body.count("\n") > 1, f"suspicious body slice for {name}"
                return body
        index += 1
    raise AssertionError(f"unterminated function body for {name}")


def test_windows_discord_recovery_carries_direct_security_pins() -> None:
    source = INSTALL_PS1.read_text(encoding="utf-8")
    body = _function_body(source, "Install-PlatformSdks")

    assert 'SecuritySpecs = @("PyNaCl==1.6.2", "aiohttp==3.14.3")' in body
    assert 'Spec = "discord.py==2.7.1"' in body
    assert 'ExtraSpecs = @("davey==0.1.4", "brotlicffi==1.2.0.1", "cffi==2.0.0")' in body
    assert 'foreach ($extraSpec in @($sdk.ExtraSpecs | Where-Object { $_ }))' in body
    assert 'foreach ($securitySpec in @($sdk.SecuritySpecs | Where-Object { $_ }))' in body
    assert "Test-VenvPackageExactVersion" in body
    assert "Ensure-VenvPipFloor" in body
    assert body.index("Ensure-VenvPipFloor") < body.index(
        "foreach ($regularSpec in $missingRegularSpecs)"
    )
    assert "securityInstallFailures" in body
    assert "regularInstallFailures" in body
    assert "remainingRegularSpecs" in body
    assert '"davey==0.1.4"' in body
    assert '"brotlicffi==1.2.0.1"' in body
    assert "allSecuritySpecs" in body
    assert "securitySpecsToRepair" in body
    assert "remainingSecuritySpecs" in body
    assert body.index("securitySpecsToRepair") > body.index(
        "foreach ($regularSpec in $missingRegularSpecs)"
    )
    assert body.count("$allSecuritySpecs | Where-Object") >= 2
    assert '$securityPackageName -ieq "PyNaCl"' in body
    assert '$securityInstallArgs = @("--no-deps")' in body
    assert "pip install @securityInstallArgs $securitySpec" in body
    assert "pip install --no-deps $securitySpec" not in body
    assert "Test-VenvRuntimeImports" in body
    assert "runtimeImportFailures" in body
    assert '"multidict"' in source
    assert '"cffi"' in source
    assert "throw \"Platform-SDK security repair failed" in body


def test_windows_security_spec_syntax_fails_closed() -> None:
    source = INSTALL_PS1.read_text(encoding="utf-8")
    helper = _function_body(source, "Test-VenvPackageExactVersion")
    assert 'throw "Unsupported security package spec syntax: $Spec"' in helper


def test_windows_slack_recovery_carries_aiohttp_security_pin() -> None:
    source = INSTALL_PS1.read_text(encoding="utf-8")
    body = _function_body(source, "Install-PlatformSdks")
    assert 'Import = "slack_sdk"' in body
    assert 'Import = "slack_bolt"' in body
    assert 'Spec = "slack-sdk==3.43.0"' in body
    assert 'Spec = "slack-bolt==1.29.0"' in body
    assert 'SecuritySpecs = @("aiohttp==3.14.3")' in body
    assert "recovery-only direct closure pin" in body


def test_windows_security_repair_checks_runtime_closure_after_metadata() -> None:
    source = INSTALL_PS1.read_text(encoding="utf-8")
    body = _function_body(source, "Install-PlatformSdks")

    runtime_call = "Test-VenvRuntimeImports -PythonExe $pythonExe -Sdks $needed"
    assert body.count(runtime_call) == 2
    assert body.index(runtime_call) < body.index(
        "if ($missingRegularSpecs.Count -eq 0"
    )
    assert body.rindex(runtime_call) > body.index("$remainingSecuritySpecs")
    assert 'throw "Platform-SDK runtime verification failed' in body
    assert '"aiohappyeyeballs"' in source
    assert '"multidict"' in source
    assert '"nacl"' in source
    assert '"cffi"' in source


def test_slack_adapter_defers_optional_aiohttp_import() -> None:
    source = (REPO_ROOT / "plugins" / "platforms" / "slack" / "adapter.py").read_text(
        encoding="utf-8"
    )
    sentinel = "aiohttp: Any = None"
    assert sentinel in source
    assert "import aiohttp as _aiohttp" in source
    assert source.index(sentinel) < source.index("def slack_deps_present")
    assert source.index("import aiohttp as _aiohttp") > source.index("try:")


def test_feishu_lazy_contract_rebinds_websocket_runtime() -> None:
    source = (REPO_ROOT / "plugins/platforms/feishu/adapter.py").read_text(
        encoding="utf-8"
    )
    assert "import websockets" in source
    assert "FEISHU_WEBSOCKET_AVAILABLE" in source
    assert "import websockets as _websockets" in source
    assert "websockets = _websockets" in source


def test_dingtalk_lazy_contract_rebinds_card_sdk_runtime() -> None:
    source = (REPO_ROOT / "plugins/platforms/dingtalk/adapter.py").read_text(
        encoding="utf-8"
    )
    assert "def _load_optional_card_sdk" in source
    assert "_load_optional_card_sdk()" in source
    assert "CARD_SDK_AVAILABLE = True" in source


def test_dingtalk_stream_contract_keeps_card_sdk_optional() -> None:
    """Basic Stream Mode must not install the optional AI-card SDK."""
    from tools.lazy_deps import LAZY_DEPS

    assert "alibabacloud-dingtalk==2.2.42" not in LAZY_DEPS["platform.dingtalk"]
    assert LAZY_DEPS["platform.dingtalk_card"] == (
        "alibabacloud-dingtalk==2.2.42",
    )


def test_windows_ensurepip_recovery_verifies_the_pip_floor() -> None:
    source = INSTALL_PS1.read_text(encoding="utf-8")
    helper = _function_body(source, "Ensure-VenvPipFloor")

    assert '$MinimumPipVersion = "26.1.2"' in source
    assert '$MinimumPipSpec = "pip>=$MinimumPipVersion"' in source
    assert "ensurepip" in _function_body(source, "Install-PlatformSdks")
    assert "pip install --upgrade $MinimumPipSpec" in helper
    assert helper.count("Get-VenvPipVersion") >= 2
    assert "return $false" in helper
    runtime_helper = _function_body(source, "Test-VenvRuntimeImports")
    assert "$securityNames" in runtime_helper
    assert "$extraNames" in runtime_helper
    assert "$securityPackageName" in source


def test_windows_pip_probe_ignores_warning_lines() -> None:
    source = INSTALL_PS1.read_text(encoding="utf-8")
    helper = _function_body(source, "Get-VenvPipVersion")
    assert "foreach ($rawLine in @($output))" in helper
    assert (
        r"from\s+(?:/|[A-Za-z]:[\\/]|\\\\)"
        in helper
    )
    assert "(?:\\s+\\(python\\s+\\d+(?:\\.\\d+){1,3}\\))?\\s*$" in helper
    assert "$canonicalVersions" in helper
    assert "$canonicalVersions.Count -ne 1" in helper
    assert "continue" in helper
    assert "TrimEnd" not in helper
    assert r"\[\]" not in helper
    assert r'[^\r\n"]*' in helper


def test_termux_pip_bootstrap_uses_and_verifies_the_floor() -> None:
    source = INSTALL_SH.read_text(encoding="utf-8")

    assert 'MIN_PIP_VERSION="26.1.2"' in source
    assert 'pip install --upgrade "pip>=${MIN_PIP_VERSION}"' in source
    assert 'pip --version' in source
    assert '"$MIN_PIP_VERSION" "$pip_version_output"' in source
    assert "minimum = tuple(int(part) for part in sys.argv[1].split(\".\"))" in source
    assert "if len(canonical_versions) == 1 and canonical_versions[0] >= minimum:" in source
    assert "release_match = re.fullmatch" in source
    assert r"\.post\d+" in source
    assert "canonical_versions = []" in source
    assert "line_match = re.fullmatch" in source
    assert "len(canonical_versions) == 1" in source
    assert "pip_upgrade_output" in source
    assert "pip floor bootstrap failed" in source


def test_legacy_setup_pip_floor_scans_all_version_candidates() -> None:
    source = (REPO_ROOT / "setup-hermes.sh").read_text(encoding="utf-8")
    assert "canonical_versions = []" in source
    assert "line_match = re.fullmatch" in source
    assert "len(canonical_versions) == 1" in source
    assert "raise SystemExit(1)" in source


@pytest.mark.parametrize(
    ("script_path", "start_marker", "end_marker"),
    [
        (
            INSTALL_SH,
            '"$PIP_PYTHON" -c \'\n',
            '\n\' "$MIN_PIP_VERSION"',
        ),
        (
            REPO_ROOT / "setup-hermes.sh",
            '"$SETUP_PYTHON" - "$MIN_PIP_VERSION" "$pip_version_output" <<\'PY\'\n',
            "\nPY",
        ),
    ],
)
def test_shell_pip_floor_parsers_skip_bad_candidates_and_find_stable_release(
    script_path: Path, start_marker: str, end_marker: str
) -> None:
    source = script_path.read_text(encoding="utf-8")
    start = source.index(start_marker) + len(start_marker)
    end = source.index(end_marker, start)
    parser = source[start:end]
    cases = (
        # A high warning must not override the lower authoritative record.
        (
            "WARNING: pip 99.0 is outdated\n"
            "pip 26.1.1 from /venv/site-packages/pip",
            False,
        ),
        # A noncanonical warning alone is not version evidence.
        ("WARNING: pip 99.0 is available", False),
        ("pip 26.1.2 from    ", False),
        ("pip 26.1.2 from", False),
        ("pip 26.1.2, from /venv/site-packages/pip", False),
        ("pip 26.1.2) from /venv/site-packages/pip", False),
        (
            "pip 26.1.2 from /venv/site-packages/pip [unexpected extra]",
            False,
        ),
        ("pip 26.1.2 from /venv/site-packages/pip trailing", False),
        (
            "pip 26.1.2 from /venv/site-packages/pip (python 3.13)",
            True,
        ),
        (
            "pip 26.1.2 from /srv/Hermes Agent/venv/site-packages/pip "
            "(python 3.13)",
            True,
        ),
        (
            r"pip 26.1.2 from C:\Program Files\Hermes Agent\venv\Lib"
            r"\site-packages\pip (python 3.13)",
            True,
        ),
        (
            "pip 26.1.2 from /home/user/[work]/'venv'/site-packages/pip",
            True,
        ),
        (
            "pip 26.1.2 from '/srv/Hermes Agent/venv/site-packages/pip' "
            "(python 3.13)",
            False,
        ),
        # A warning may accompany one valid canonical record.
        (
            "warning pip 99.0 is outdated\n"
            "pip 26.1.2.post1 from /venv/site-packages/pip",
            True,
        ),
        # Conflicting/duplicate canonical records fail closed.
        (
            "pip 26.1.2 from /venv/site-packages/pip\n"
            "pip 26.1.2 from /other/site-packages/pip",
            False,
        ),
        (
            "noise pip ???\n"
            "pip 26.1.2.post1 from /venv/site-packages/pip",
            True,
        ),
    )
    for output, expected in cases:
        result = subprocess.run(
            [sys.executable, "-", "26.1.2", output],
            input=parser,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        assert (result.returncode == 0) is expected, (output, result.stderr)


def test_termux_shell_floor_probes_fail_closed_on_pip_version_error() -> None:
    install = INSTALL_SH.read_text(encoding="utf-8")
    legacy = (REPO_ROOT / "setup-hermes.sh").read_text(encoding="utf-8")
    assert 'if ! pip_version_output="$("$PIP_PYTHON" -m pip --version 2>&1)"; then' in install
    assert 'if ! pip_version_output="$("$SETUP_PYTHON" -m pip --version 2>&1)"; then' in legacy
    assert '"$MIN_PIP_VERSION" "$pip_version_output"' in legacy


def test_update_no_uv_paths_verify_the_pip_floor() -> None:
    source = UPDATE_CMD.read_text(encoding="utf-8")

    assert "from tools.lazy_deps import _ensure_pip_floor" in source
    assert source.count("_ensure_update_pip_floor(pip_cmd)") >= 2
    assert "_MIN_PIP_SPEC" in source
    start = source.index("def _ensure_update_pip_floor")
    end = source.index("\ndef _refresh_active_lazy_features", start)
    helper = source[start:end]
    assert "timeout=15" in helper
    assert "timeout=120" in helper
    assert "floor helper import failed" in helper
    upgrade_start = source.index("def _upgrade_pip_before_lazy_refresh")
    upgrade_end = source.index("\ndef _ensure_update_pip_floor", upgrade_start)
    upgrade_helper = source[upgrade_start:upgrade_end]
    assert "except (" in upgrade_helper
    assert "ImportError" in upgrade_helper
    assert "OSError" in upgrade_helper
    assert "subprocess.CalledProcessError" in upgrade_helper
    assert "subprocess.TimeoutExpired" in upgrade_helper
    assert "_pip_security.PipFloorError" in upgrade_helper


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("pip 26.1.1 from /venv/site-packages/pip (python 3.13)", False),
        ("pip 26.1.2 from /venv/site-packages/pip (python 3.13)", True),
        ("pip 26.1.2.post1 from /venv/site-packages/pip (python 3.13)", True),
        (
            "WARNING: pip 99.0 is available\npip 26.1.1 from /venv/site-packages/pip",
            False,
        ),
        ("WARNING: pip 99.0 is available", False),
        ("pip 26.1.2 from    ", False),
        ("pip 26.1.2 from", False),
        (
            "WARNING: pip 99.0 is available\npip 26.1.2 from /venv/site-packages/pip",
            True,
        ),
        (
            "pip 26.1.2 from /venv/site-packages/pip\n"
            "pip 26.1.2 from /other/site-packages/pip",
            False,
        ),
        ("pip 26.1.2.dev0 from /venv/site-packages/pip (python 3.13)", False),
        ("pip 26.1.2.rc1 from /venv/site-packages/pip (python 3.13)", False),
        ("pip 26.1.2rc1 from /venv/site-packages/pip (python 3.13)", False),
        ("pip 26.1.2+local from /venv/site-packages/pip (python 3.13)", False),
        ("pip 26.1.2, from /venv/site-packages/pip (python 3.13)", False),
        ("pip 26.1.2) from /venv/site-packages/pip (python 3.13)", False),
        (
            "pip 26.1.2 from /home/user/[work]/venv/site-packages/pip",
            True,
        ),
        (
            "pip 26.1.2 from /home/user/'work'/venv/site-packages/pip",
            True,
        ),
        (
            "pip 26.1.2 from /srv/Hermes Agent/venv/site-packages/pip "
            "(python 3.13)",
            True,
        ),
        (
            r"pip 26.1.2 from C:\Program Files\Hermes Agent\venv\Lib"
            r"\site-packages\pip (python 3.13)",
            True,
        ),
        (
            "pip 26.1.2 from '/srv/Hermes Agent/venv/site-packages/pip' "
            "(python 3.13)",
            False,
        ),
        (
            "pip 26.1.2 from /venv/site-packages/pip [unexpected extra]",
            False,
        ),
        ("pip 26.1.2 from /venv/site-packages/pip trailing", False),
        (
            "pip 26.1.2 from /venv/site-packages/pip (python 3.13)",
            True,
        ),
    ],
)
def test_pip_floor_accepts_only_stable_versions(output: str, expected: bool) -> None:
    assert _pip_security.pip_version_meets_floor(output) is expected


def test_pip_floor_does_not_normalize_punctuated_version_tokens() -> None:
    assert not _pip_security.pip_version_meets_floor(
        "pip 26.1.2, from /venv/site-packages/pip"
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1.6.2", (1, 6, 2)),
        ("1.6.2.post1", (1, 6, 2)),
        ("1.6.2.dev0", None),
        ("1.6.2rc1", None),
        ("1.6.2+local", None),
        ("1.6.2.vendor", None),
    ],
)
def test_shared_stable_version_evidence_rejects_nonstable_values(
    value: str, expected: tuple[int, ...] | None
) -> None:
    assert _pip_security.stable_version_tuple(value) == expected


def test_messaging_extra_uses_explicit_discord_voice_contract() -> None:
    """Published pip metadata must keep voice support at the fixed floor."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    messaging = data["project"]["optional-dependencies"]["messaging"]
    assert "discord.py==2.7.1" in messaging
    assert "davey==0.1.4" in messaging
    assert "brotlicffi==1.2.0.1" in messaging
    assert "PyNaCl==1.6.2" in messaging

    # Avoid discord.py's stale [voice] resolver metadata while retaining its
    # voice runtime dependencies explicitly at known-compatible versions.
    assert not any(spec.startswith("discord.py[voice]") for spec in messaging)
    assert re.search(r"davey==0\.1\.4", " ".join(messaging))
    assert re.search(r"PyNaCl==1\.6\.2", " ".join(messaging))
    assert re.search(r"discord\.py==2\.7\.1", " ".join(messaging))


def test_lazy_aiohttp_security_roots_are_explicitly_pinned() -> None:
    """Every supported lazy SDK root carries the patched aiohttp contract."""
    from tools.lazy_deps import LAZY_DEPS

    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    expected = {
        "platform.dingtalk": "dingtalk",
        "platform.feishu": "feishu",
        "search.firecrawl": "firecrawl",
        "tts.edge": "edge-tts",
        "memory.hindsight": "hindsight",
        "terminal.modal": "modal",
        "terminal.daytona": "daytona",
        # Callback mode has its own lazy installer and can be enabled without
        # the broader messaging extra, so it must carry the closure too.
        "platform.wecom_callback": "wecom",
    }
    for feature, extra in expected.items():
        assert "aiohttp==3.14.3" in LAZY_DEPS[feature], feature
        assert "aiohttp==3.14.3" in data["project"]["optional-dependencies"][extra]


def test_messaging_security_pin_is_in_the_committed_lock() -> None:
    """Keep the fail-closed published extra and uv metadata in sync."""
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    project = next(
        package
        for package in lock["package"]
        if package.get("name") == "hermes-agent"
        and package.get("source") == {"editable": "."}
    )
    messaging = project["optional-dependencies"]["messaging"]
    assert {item["name"] for item in messaging} >= {"davey", "discord-py", "pynacl"}
    requires_dist = project["metadata"]["requires-dist"]
    assert {
        item["specifier"]
        for item in requires_dist
        if item["name"] == "pynacl" and item.get("marker") == "extra == 'messaging'"
    } == {"==1.6.2"}
    assert {
        item["specifier"]
        for item in requires_dist
        if item["name"] == "davey" and item.get("marker") == "extra == 'messaging'"
    } == {"==0.1.4"}
    assert {
        item.get("extras")
        for item in requires_dist
        if item["name"] == "discord-py" and item.get("marker") == "extra == 'messaging'"
    } == {None}


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("1.5.0", False),
        ("1.6.2.dev0", False),
        ("1.6.2rc1", False),
        ("1.6.2+local", False),
        ("1.6.2", True),
        ("1.6.2.post1", True),
    ],
)
def test_discord_voice_doctor_rejects_nonfixed_pynacl(version: str, expected: bool) -> None:
    doctor = _load_voice_doctor()
    assert doctor._pynacl_meets_security_floor(version) is expected


@pytest.mark.parametrize("version", ["1.6.2.1", "2.0", "2.0.0.post1"])
def test_discord_voice_doctor_accepts_future_stable_pynacl_releases(version: str) -> None:
    doctor = _load_voice_doctor()
    assert doctor._pynacl_meets_security_floor(version) is True


def test_discord_voice_doctor_guidance_uses_fixed_voice_contract() -> None:
    source = (REPO_ROOT / "scripts" / "discord-voice-doctor.py").read_text(
        encoding="utf-8"
    )
    assert "discord.py==2.7.1 davey==0.1.4 PyNaCl==1.6.2" in source
    assert "need >=1.6.2" in source
    assert "pip install PyNaCl==1.6.2" in source


def test_update_termux_uv_bootstrap_is_after_floor_gate() -> None:
    source = UPDATE_CMD.read_text(encoding="utf-8")
    first_bootstrap = source.index("_ensure_uv_for_termux(pip_cmd)")
    preceding = source[:first_bootstrap]
    assert preceding.rfind("_ensure_update_pip_floor(pip_cmd)") > preceding.rfind(
        "if not uv_bin:"
    )
    assert source.count("except _pip_security.PipFloorError") >= 2


def test_direct_pip_recovery_paths_reuse_the_shared_floor_guard() -> None:
    main = (REPO_ROOT / "hermes_cli/main.py").read_text(encoding="utf-8")
    early = (REPO_ROOT / "hermes_cli/_early_recovery.py").read_text(encoding="utf-8")
    tools = (REPO_ROOT / "hermes_cli/tools_config.py").read_text(encoding="utf-8")
    setup = (REPO_ROOT / "hermes_cli/setup.py").read_text(encoding="utf-8")
    google = (
        REPO_ROOT / "skills/productivity/google-workspace/scripts/setup.py"
    ).read_text(encoding="utf-8")
    legacy = (REPO_ROOT / "setup-hermes.sh").read_text(encoding="utf-8")

    assert "_ensure_direct_pip_floor(install_cmd_prefix" in main
    assert "_pip_security.ensure_pip_floor" in main
    assert "_pip_security.ensure_pip_floor" in early
    assert "_pip_security.ensure_pip_floor" in tools
    assert 'ensure("terminal.vercel", prompt=False)' in setup
    assert 'result = _pip_install(["vercel"]' not in setup
    assert "_ensure_pip_floor" in google
    assert 'pip>=${MIN_PIP_VERSION}' in legacy
    assert "verify_pip_floor" in legacy


def test_tools_config_direct_pip_recovery_has_one_timeout_budget() -> None:
    source = (REPO_ROOT / "hermes_cli/tools_config.py").read_text(encoding="utf-8")
    helper = source[source.index("def _pip_install("):source.index("\n\n\n# The asset-probe", source.index("def _pip_install("))]
    assert "pip_deadline = time.monotonic()" in helper
    assert "_remaining_timeout" in helper
    assert "floor_timeout" in helper
    assert "install_timeout" in helper


def test_google_workspace_standalone_copy_keeps_a_local_floor_guard() -> None:
    source = (
        REPO_ROOT / "skills/productivity/google-workspace/scripts/setup.py"
    ).read_text(encoding="utf-8")
    assert '_MIN_PIP_SPEC = "pip>=26.1.2"' in source
    assert "def _bundled_pip_version_meets_floor" in source
    assert "def _bundled_ensure_pip_floor" in source
    assert "ensure_pip_floor = _ensure_pip_floor or _bundled_ensure_pip_floor" in source


def test_direct_pip_recovery_callers_hide_windows_console_and_close_stdin() -> None:
    paths = (
        REPO_ROOT / "hermes_cli/_early_recovery.py",
        REPO_ROOT / "hermes_cli/doctor.py",
        REPO_ROOT / "skills/productivity/google-workspace/scripts/setup.py",
        REPO_ROOT / "scripts/install_psutil_android.py",
    )
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "windows_hide_flags" in source, path
        assert "subprocess.DEVNULL" in source, path


def test_windows_platform_sdk_stage_precedes_bootstrap_marker() -> None:
    source = INSTALL_PS1.read_text(encoding="utf-8")
    stages = source[source.index('$InstallStages += @(') :]
    assert stages.index('Name = "platform-sdks"') < stages.index(
        'Name = "bootstrap-marker"'
    )
    stage = _function_body(source, "Stage-PlatformSdks")
    assert "Remove-BootstrapMarker" in stage
    assert "try" in stage and "catch" in stage


def test_windows_platform_sdk_failure_invalidates_bootstrap_marker() -> None:
    source = INSTALL_PS1.read_text(encoding="utf-8")
    helper = _function_body(source, "Remove-BootstrapMarker")
    assert 'Join-Path $InstallDir ".hermes-bootstrap-complete"' in helper
    assert "Remove-Item -LiteralPath $markerPath -Force" in helper
    assert "Bootstrap marker still exists" in helper


def test_windows_platform_sdk_preserves_original_and_marker_errors() -> None:
    source = INSTALL_PS1.read_text(encoding="utf-8")
    stage = _function_body(source, "Stage-PlatformSdks")
    assert "$failure = $_" in stage
    assert "$markerFailure = $_" in stage
    assert "Platform SDK stage failed:" in stage
    assert "bootstrap marker invalidation also failed:" in stage


def test_windows_sdk_spec_probe_fails_closed_when_packaging_is_unavailable() -> None:
    source = INSTALL_PS1.read_text(encoding="utf-8")
    helper = _function_body(source, "Test-VenvPackageSpecSatisfied")
    assert "raise SystemExit(3)" in helper
    assert "$probeExit = $LASTEXITCODE" in helper
    assert "no usable 'packaging' module" in helper


def test_dashboard_startup_rechecks_lazy_security_floor_before_imports() -> None:
    """An importable FastAPI must not bypass a stale Starlette repair."""
    source = MAIN_CMD.read_text(encoding="utf-8")
    ensure_call = '_ensure_dashboard_deps("tool.dashboard", prompt=False)'
    assert ensure_call in source
    assert source.index(ensure_call) < source.index("import fastapi", source.index(ensure_call))
