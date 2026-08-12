"""Behavior contract for the reversible default-profile safety transition.

All state is synthetic and isolated under a temporary ``HERMES_HOME``.  The
fixture intentionally exercises the same config, verification, and memory
control seams that a later live cutover will use without touching live state.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

import utils
from hermes_cli.config import set_config_value


_INITIAL_CONFIG = (
    "# synthetic safety fixture; preserve this header byte-for-byte\n"
    "approvals:\n"
    "  mode: \"manual\"\n"
    "  timeout: 321  # unrelated approval setting\n"
    "  cron_mode: \"deny\"\n"
    "memory:\n"
    "  write_approval: false\n"
    "  provider: \"\"  # unrelated memory setting\n"
    "agent:\n"
    "  verify_on_stop: auto\n"
    "unrelated:\n"
    "  quoted: \"keep:exact\"\n"
    "  flow: [one, \"two\"]\n"
    "# synthetic tail sentinel\n"
)


def _mask_transition_values(raw: bytes) -> bytes:
    """Mask only the three authorized scalar values for byte comparison."""
    patterns = (
        (rb"(?m)^(  mode:)[^\r\n]*(\r?)$", rb"\1 <TARGET>\2"),
        (
            rb"(?m)^(  write_approval:)[^\r\n]*(\r?)$",
            rb"\1 <TARGET>\2",
        ),
        (
            rb"(?m)^(  verify_on_stop:)[^\r\n]*(\r?)$",
            rb"\1 <TARGET>\2",
        ),
    )
    for pattern, replacement in patterns:
        raw = re.sub(pattern, replacement, raw, count=1)
    return raw


@pytest.fixture
def synthetic_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    config_path = home / "config.yaml"
    config_path.write_text(_INITIAL_CONFIG, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _apply_safety_transition() -> None:
    set_config_value("approvals.mode", "smart")
    set_config_value("memory.write_approval", "true")
    set_config_value("agent.verify_on_stop", "true")


def test_safety_transition_round_trip_preserves_unrelated_bytes(synthetic_home):
    config_path = synthetic_home / "config.yaml"
    original = config_path.read_bytes()

    _apply_safety_transition()

    transitioned = config_path.read_bytes()
    loaded = yaml.safe_load(transitioned)
    assert loaded["approvals"]["mode"] == "smart"
    assert loaded["memory"]["write_approval"] is True
    assert loaded["agent"]["verify_on_stop"] is True
    assert loaded["approvals"]["cron_mode"] == "deny"
    assert _mask_transition_values(transitioned) == _mask_transition_values(original)

    set_config_value("approvals.mode", "manual")
    set_config_value("memory.write_approval", "false")
    set_config_value("agent.verify_on_stop", "auto")

    assert config_path.read_bytes() == original


def test_verify_on_stop_exit_without_acceptance_stays_incomplete(synthetic_home):
    from run_agent import AIAgent

    _apply_safety_transition()
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            session_id="synthetic-safety-transition",
            api_key="test-key",
            base_url="https://example.invalid/v1",
            provider="openai-compat",
            model="test/model",
            max_iterations=1,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent._cached_system_prompt = "stable synthetic prompt"
    agent._session_db = None
    agent._session_json_enabled = False
    agent.save_trajectories = False
    agent.compression_enabled = False
    agent._cleanup_task_resources = lambda *_a, **_kw: None
    agent._save_trajectory = lambda *_a, **_kw: None

    def model_call(_api_kwargs):
        agent._turn_file_mutation_paths = {"changed.py"}
        message = SimpleNamespace(content="exit-only candidate", tool_calls=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")],
            model="test/model",
            usage=None,
        )

    agent._interruptible_api_call = model_call
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")

    with (
        patch("agent.verification_stop.build_verify_on_stop_nudge", return_value="verify it"),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("edit changed.py")

    assert result["final_response"] == "exit-only candidate"
    assert result["turn_exit_reason"] == "max_iterations_reached(1/1)"
    assert result["completed"] is False
    agent._handle_max_iterations.assert_not_called()


def test_durable_memory_promotion_stops_at_approval_with_zero_writes(synthetic_home):
    from tools import write_approval as wa
    from tools.memory_tool import MemoryStore, memory_tool
    from tools.terminal_tool import set_approval_callback

    _apply_safety_transition()
    approval_requests = []

    def deny(command, description, **_kwargs):
        approval_requests.append((command, description))
        return "deny"

    set_approval_callback(deny)
    try:
        store = MemoryStore()
        store.load_from_disk()
        result = json.loads(
            memory_tool("add", "memory", "synthetic durable fact", store=store)
        )
    finally:
        set_approval_callback(None)

    assert result["success"] is False
    assert "denied" in result["error"].lower()
    assert len(approval_requests) == 1
    assert store.memory_entries == []
    assert wa.pending_count("memory") == 0
    assert not (synthetic_home / "MEMORY.md").exists()
    assert not (synthetic_home / "USER.md").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode contract")
@pytest.mark.parametrize("writer", ["update", "save"])
def test_roundtrip_writer_applies_original_mode_before_atomic_replace(
    tmp_path, monkeypatch, writer
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("setting: old\n", encoding="utf-8")
    config_path.chmod(0o640)
    observed_modes = []
    real_atomic_replace = utils.atomic_replace

    def record_mode_before_replace(tmp_file, target):
        observed_modes.append(stat.S_IMODE(Path(tmp_file).stat().st_mode))
        return real_atomic_replace(tmp_file, target)

    monkeypatch.setattr(utils, "atomic_replace", record_mode_before_replace)
    if writer == "update":
        utils.atomic_roundtrip_yaml_update(config_path, "setting", "new")
    else:
        utils.atomic_roundtrip_yaml_save(config_path, {"setting": "new"})

    assert observed_modes == [0o640]
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o640


def test_numeric_config_update_preserves_yaml11_ambiguous_list_strings(
    synthetic_home,
):
    config_path = synthetic_home / "config.yaml"
    config_path.write_text(
        "custom_providers:\n"
        "- name: provider-a\n"
        "  api_key: old-key\n"
        "unrelated:\n"
        '  tokens: ["off", "yes", "null", "~"]\n'
        "  items:\n"
        '  - duration: "1:20"\n',
        encoding="utf-8",
    )

    set_config_value("custom_providers.0.api_key", "new-key")

    loaded = yaml.safe_load(config_path.read_bytes())
    assert loaded["custom_providers"][0]["api_key"] == "new-key"
    assert loaded["unrelated"]["tokens"] == ["off", "yes", "null", "~"]
    assert all(isinstance(value, str) for value in loaded["unrelated"]["tokens"])
    assert loaded["unrelated"]["items"][0]["duration"] == "1:20"
    assert type(loaded["unrelated"]["items"][0]["duration"]) is str


def test_safety_transition_round_trip_preserves_crlf_bytes(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    config_path = home / "config.yaml"
    original = _INITIAL_CONFIG.replace("\n", "\r\n").encode("utf-8")
    config_path.write_bytes(original)
    monkeypatch.setenv("HERMES_HOME", str(home))

    _apply_safety_transition()

    transitioned = config_path.read_bytes()
    loaded = yaml.safe_load(transitioned)
    assert loaded["approvals"]["mode"] == "smart"
    assert loaded["memory"]["write_approval"] is True
    assert loaded["agent"]["verify_on_stop"] is True
    assert loaded["approvals"]["cron_mode"] == "deny"
    assert _mask_transition_values(transitioned) == _mask_transition_values(original)

    set_config_value("approvals.mode", "manual")
    set_config_value("memory.write_approval", "false")
    set_config_value("agent.verify_on_stop", "auto")

    assert config_path.read_bytes() == original


def _mask_setting_value(raw: bytes) -> bytes:
    return re.sub(
        rb"(?m)^(setting:)[^\r\n]*(\r?)$",
        rb"\1 <TARGET>\2",
        raw,
        count=1,
    )


@pytest.mark.parametrize("writer", ["update", "save"])
def test_roundtrip_writer_preserves_mixed_line_endings(tmp_path, writer):
    config_path = tmp_path / "config.yaml"
    original = (
        b"# unrelated bare-LF header\n"
        b"setting: old\r\n"
        b"# unrelated CRLF comment\r\n"
        b"unrelated: keep\n"
    )
    config_path.write_bytes(original)

    if writer == "update":
        utils.atomic_roundtrip_yaml_update(config_path, "setting", "new")
    else:
        utils.atomic_roundtrip_yaml_save(
            config_path,
            {"setting": "new", "unrelated": "keep"},
        )

    assert _mask_setting_value(config_path.read_bytes()) == _mask_setting_value(
        original
    )

    if writer == "update":
        utils.atomic_roundtrip_yaml_update(config_path, "setting", "old")
    else:
        utils.atomic_roundtrip_yaml_save(
            config_path,
            {"setting": "old", "unrelated": "keep"},
        )
    assert config_path.read_bytes() == original


@pytest.mark.parametrize("writer", ["update", "save"])
def test_roundtrip_writer_avoids_windows_crlf_double_translation(
    tmp_path, monkeypatch, writer
):
    config_path = tmp_path / "config.yaml"
    original = b"# header\r\nsetting: old\r\nunrelated: keep\r\n"
    config_path.write_bytes(original)
    real_fdopen = utils.os.fdopen

    def simulate_windows_fdopen(fd, mode, *args, **kwargs):
        if "b" not in mode and "newline" not in kwargs:
            kwargs["newline"] = "\r\n"
        return real_fdopen(fd, mode, *args, **kwargs)

    monkeypatch.setattr(utils.os, "fdopen", simulate_windows_fdopen)
    if writer == "update":
        utils.atomic_roundtrip_yaml_update(config_path, "setting", "new")
    else:
        utils.atomic_roundtrip_yaml_save(
            config_path,
            {"setting": "new", "unrelated": "keep"},
        )

    transitioned = config_path.read_bytes()
    assert b"\r\r\n" not in transitioned
    assert re.search(rb"(?<!\r)\n", transitioned) is None
    assert _mask_setting_value(transitioned) == _mask_setting_value(original)

    if writer == "update":
        utils.atomic_roundtrip_yaml_update(config_path, "setting", "old")
    else:
        utils.atomic_roundtrip_yaml_save(
            config_path,
            {"setting": "old", "unrelated": "keep"},
        )
    assert config_path.read_bytes() == original


@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
def test_safety_transition_round_trip_preserves_mixed_line_endings(
    tmp_path, monkeypatch, bom
):
    home = tmp_path / ".hermes"
    home.mkdir()
    config_path = home / "config.yaml"
    original = bom + b"".join(
        line.encode("utf-8") + (b"\n" if line.startswith("#") else b"\r\n")
        for line in _INITIAL_CONFIG.splitlines()
    )
    config_path.write_bytes(original)
    monkeypatch.setenv("HERMES_HOME", str(home))

    _apply_safety_transition()
    transitioned = config_path.read_bytes()
    assert _mask_transition_values(transitioned) == _mask_transition_values(original)

    set_config_value("approvals.mode", "manual")
    set_config_value("memory.write_approval", "false")
    set_config_value("agent.verify_on_stop", "auto")
    assert config_path.read_bytes() == original


@pytest.mark.parametrize("writer", ["update", "save"])
@pytest.mark.parametrize("key", ["off", "yes", "null", "~", "1:20"])
def test_roundtrip_writer_quotes_yaml11_mapping_keys(tmp_path, writer, key):
    path = tmp_path / "config.yaml"
    path.write_text("{}\n", encoding="utf-8")
    state = {"providers": {key: {"command": "run"}}}
    if writer == "update":
        utils.atomic_roundtrip_yaml_update(path, f"providers.{key}.command", "run")
    else:
        utils.atomic_roundtrip_yaml_save(path, state)
    loaded = yaml.safe_load(path.read_bytes())
    actual_key = next(iter(loaded["providers"]))
    assert actual_key == key
    assert type(actual_key) is str


@pytest.mark.parametrize("writer", ["update", "save"])
def test_roundtrip_writer_preserves_bom_and_indentless_sequence(tmp_path, writer):
    path = tmp_path / "config.yaml"
    original = (
        b"\xef\xbb\xbf# header\nsetting: old\r\nunrelated:\n"
        b"- one\r\n- two\n"
    )
    path.write_bytes(original)
    state = {"setting": "new", "unrelated": ["one", "two"]}
    if writer == "update":
        utils.atomic_roundtrip_yaml_update(path, "setting", "new")
    else:
        utils.atomic_roundtrip_yaml_save(path, state)
    assert _mask_setting_value(path.read_bytes()) == _mask_setting_value(original)
    state["setting"] = "old"
    if writer == "update":
        utils.atomic_roundtrip_yaml_update(path, "setting", "old")
    else:
        utils.atomic_roundtrip_yaml_save(path, state)
    assert path.read_bytes() == original


def test_safety_transition_preserves_indentless_sequence(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    path = home / "config.yaml"
    original = _INITIAL_CONFIG.replace(
        "# synthetic tail sentinel\n",
        "unrelated_list:\n- one\r\n- two\n# synthetic tail sentinel\n",
    ).encode("utf-8")
    path.write_bytes(original)
    monkeypatch.setenv("HERMES_HOME", str(home))
    _apply_safety_transition()
    assert _mask_transition_values(path.read_bytes()) == _mask_transition_values(original)
    set_config_value("approvals.mode", "manual")
    set_config_value("memory.write_approval", "false")
    set_config_value("agent.verify_on_stop", "auto")
    assert path.read_bytes() == original


@pytest.mark.skipif(not hasattr(os, "fchown"), reason="requires POSIX fchown")
@pytest.mark.parametrize("writer", ["update", "save"])
def test_roundtrip_writer_applies_owner_before_replace(tmp_path, monkeypatch, writer):
    path = tmp_path / "config.yaml"
    path.write_text("setting: old\n", encoding="utf-8")
    calls = []
    real_replace = utils.atomic_replace
    monkeypatch.setattr(utils, "_preserve_file_owner", lambda _path: (123, 456))
    monkeypatch.setattr(utils, "_restore_file_owner", lambda *_args: None)
    monkeypatch.setattr(utils.os, "fchown", lambda _fd, uid, gid: calls.append((uid, gid)))

    def assert_owned_before_replace(source, target):
        assert calls == [(123, 456)]
        return real_replace(source, target)
    monkeypatch.setattr(utils, "atomic_replace", assert_owned_before_replace)
    if writer == "update":
        utils.atomic_roundtrip_yaml_update(path, "setting", "new")
    else:
        utils.atomic_roundtrip_yaml_save(path, {"setting": "new"})


@pytest.mark.skipif(not hasattr(os, "fchown"), reason="requires POSIX fchown")
@pytest.mark.parametrize("writer", ["update", "save"])
def test_roundtrip_writer_accepts_rejected_redundant_fchown(tmp_path, monkeypatch, writer):
    path = tmp_path / "config.yaml"
    path.write_text("setting: old\n", encoding="utf-8")
    owner = (path.stat().st_uid, path.stat().st_gid)

    def reject_redundant(fd, uid, gid):
        temp_stat = os.fstat(fd)
        assert (temp_stat.st_uid, temp_stat.st_gid) == (uid, gid) == owner
        raise PermissionError("redundant fchown rejected")
    monkeypatch.setattr(utils.os, "fchown", reject_redundant)
    if writer == "update":
        utils.atomic_roundtrip_yaml_update(path, "setting", "new")
    else:
        utils.atomic_roundtrip_yaml_save(path, {"setting": "new"})
    assert yaml.safe_load(path.read_bytes())["setting"] == "new"


@pytest.mark.skipif(not hasattr(os, "fchown"), reason="requires POSIX fchown")
@pytest.mark.parametrize("writer", ["update", "save"])
def test_roundtrip_writer_fails_closed_when_fchown_fails_and_target_owner_changes(
    tmp_path, monkeypatch, writer
):
    path = tmp_path / "config.yaml"
    original = b"setting: old\nunrelated: keep\n"
    path.write_bytes(original)
    owner_a = (123, 456)
    owner_b = SimpleNamespace(st_uid=234, st_gid=567)
    owner_c = SimpleNamespace(
        st_uid=345, st_gid=678, st_mode=path.stat().st_mode
    )
    real_stat = Path.stat
    replace_mock = MagicMock()
    monkeypatch.setattr(utils, "_preserve_file_owner", lambda _path: owner_a)
    monkeypatch.setattr(utils.os, "fstat", lambda _fd: owner_b)
    monkeypatch.setattr(utils.os, "fchown", MagicMock(side_effect=PermissionError("denied")))

    def changed_target_stat(self, *args, **kwargs):
        if self == path:
            return owner_c
        return real_stat(self, *args, **kwargs)
    monkeypatch.setattr(utils.Path, "stat", changed_target_stat)
    monkeypatch.setattr(utils, "atomic_replace", replace_mock)
    with pytest.raises(PermissionError, match="denied"):
        if writer == "update":
            utils.atomic_roundtrip_yaml_update(path, "setting", "new")
        else:
            utils.atomic_roundtrip_yaml_save(path, {"setting": "new", "unrelated": "keep"})
    replace_mock.assert_not_called()
    assert path.read_bytes() == original


def test_numeric_list_update_preserves_distinct_yaml12_ambiguous_keys(synthetic_home):
    from ruamel.yaml import YAML

    path = synthetic_home / "config.yaml"
    path.write_text(
        "custom_providers:\n"
        "- name: provider-a\n"
        "  api_key: old-key\n"
        "unrelated:\n"
        "  off: string-key\n"
        "  false: boolean-key\n",
        encoding="utf-8",
    )

    set_config_value("custom_providers.0.api_key", "new-key")

    rendered = path.read_text(encoding="utf-8")
    yaml12 = YAML(typ="safe")
    yaml12.version = (1, 2)
    loaded12 = yaml12.load(rendered)
    assert loaded12["custom_providers"][0]["api_key"] == "new-key"
    assert loaded12["unrelated"] == {"off": "string-key", False: "boolean-key"}
    loaded11 = yaml.safe_load(rendered)
    assert loaded11["unrelated"] == {"off": "string-key", False: "boolean-key"}


@pytest.mark.skipif(not hasattr(os, "fchown"), reason="requires POSIX fchown")
@pytest.mark.parametrize("writer", ["update", "save"])
def test_roundtrip_writer_reapplies_mode_after_fchown_before_replace(
    tmp_path, monkeypatch, writer
):
    path = tmp_path / "config.yaml"
    path.write_text("setting: old\n", encoding="utf-8")
    os.chown(path, -1, os.getgid())
    os.chmod(path, 0o2750)
    monkeypatch.setattr(utils, "_preserve_file_owner", lambda _path: (123, 456))
    real_fchown = os.fchown

    def fchown_that_clears_setid(fd, _uid, _gid):
        real_fchown(fd, -1, os.getgid())
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
        os.fchmod(fd, mode & ~(stat.S_ISUID | stat.S_ISGID))

    monkeypatch.setattr(utils.os, "fchown", fchown_that_clears_setid)
    real_replace = utils.atomic_replace

    def assert_complete_metadata_before_replace(source, target):
        assert stat.S_IMODE(Path(source).stat().st_mode) == 0o2750
        return real_replace(source, target)

    monkeypatch.setattr(utils, "atomic_replace", assert_complete_metadata_before_replace)
    if writer == "update":
        utils.atomic_roundtrip_yaml_update(path, "setting", "new")
    else:
        utils.atomic_roundtrip_yaml_save(path, {"setting": "new"})


def test_roundtrip_update_quotes_equal_existing_yaml11_mapping_key(tmp_path):
    from ruamel.yaml import YAML

    path = tmp_path / "config.yaml"
    path.write_text(
        "providers:\n"
        "  off:  # preserve-key-comment\n"
        "    command: old\n"
        "  false:\n"
        "    command: boolean\n",
        encoding="utf-8",
    )

    utils.atomic_roundtrip_yaml_update(path, "providers.off.command", "new")

    rendered = path.read_text(encoding="utf-8")
    assert "# preserve-key-comment" in rendered
    yaml12 = YAML(typ="safe")
    yaml12.version = (1, 2)
    loaded12 = yaml12.load(rendered)
    assert loaded12["providers"]["off"]["command"] == "new"
    assert loaded12["providers"][False]["command"] == "boolean"
    loaded11 = yaml.safe_load(rendered)
    assert loaded11["providers"]["off"]["command"] == "new"
    assert loaded11["providers"][False]["command"] == "boolean"


def test_numeric_list_update_preserves_unrelated_plain_yaml11_boolean(synthetic_home):
    path = synthetic_home / "config.yaml"
    path.write_text(
        "custom_providers:\n"
        "- name: provider-a\n"
        "  api_key: old-key\n"
        "cron:\n"
        "  model_drift_guard: off\n",
        encoding="utf-8",
    )

    set_config_value("custom_providers.0.api_key", "new-key")

    rendered = path.read_text(encoding="utf-8")
    assert "model_drift_guard: off" in rendered
    loaded = yaml.safe_load(rendered)
    assert loaded["cron"]["model_drift_guard"] is False


def test_numeric_list_update_preserves_list_item_comments(synthetic_home):
    path = synthetic_home / "config.yaml"
    path.write_text(
        "custom_providers:\n"
        "- name: provider-a  # preserve-item-comment\n"
        "  api_key: old-key  # preserve-api-comment\n"
        "unrelated: keep\n",
        encoding="utf-8",
    )

    set_config_value("custom_providers.0.api_key", "new-key")

    rendered = path.read_text(encoding="utf-8")
    assert "# preserve-item-comment" in rendered
    assert "# preserve-api-comment" in rendered
    loaded = yaml.safe_load(rendered)
    assert loaded["custom_providers"][0]["api_key"] == "new-key"


def test_numeric_list_update_applies_equal_but_type_distinct_value(synthetic_home):
    path = synthetic_home / "config.yaml"
    path.write_text(
        "custom_providers:\n"
        "- name: provider-a\n"
        "  enabled: false\n",
        encoding="utf-8",
    )

    set_config_value("custom_providers.0.enabled", "0")

    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert type(loaded["custom_providers"][0]["enabled"]) is int
    assert loaded["custom_providers"][0]["enabled"] == 0


@pytest.mark.parametrize("writer", ["update", "save"])
def test_roundtrip_writer_does_not_mutate_metadata_after_replace(
    tmp_path, monkeypatch, writer
):
    path = tmp_path / "config.yaml"
    path.write_text("setting: old\n", encoding="utf-8")
    os.chown(path, -1, os.getgid())
    os.chmod(path, 0o2750)
    published = False
    post_publish_calls = []

    def publish(tmp_name, target):
        nonlocal published
        os.replace(tmp_name, target)
        published = True
        return str(target)

    def record_chown(*_args):
        if published:
            post_publish_calls.append("chown")

    def record_chmod(*_args):
        if published:
            post_publish_calls.append("chmod")

    monkeypatch.setattr(utils, "atomic_replace", publish)
    monkeypatch.setattr(utils.os, "chown", record_chown)
    monkeypatch.setattr(utils.os, "chmod", record_chmod)

    if writer == "update":
        utils.atomic_roundtrip_yaml_update(path, "setting", "new")
    else:
        utils.atomic_roundtrip_yaml_save(path, {"setting": "new"})

    assert post_publish_calls == []
    assert stat.S_IMODE(path.stat().st_mode) == 0o2750
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["setting"] == "new"
