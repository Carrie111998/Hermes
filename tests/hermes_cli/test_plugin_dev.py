from __future__ import annotations

import argparse
import errno
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from hermes_cli.subcommands.plugins import build_plugins_parser


def _parse_plugins_args(*argv: str):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_plugins_parser(subparsers, cmd_plugins=lambda args: None)
    return parser.parse_args(["plugins", *argv])


def test_plugins_parser_exposes_doctor() -> None:
    doctor = _parse_plugins_args("doctor", "sample", "--ci")

    assert (doctor.plugins_action, doctor.target, doctor.ci) == (
        "doctor",
        "sample",
        True,
    )


def test_plugins_parser_requires_explicit_doctor_target() -> None:
    with pytest.raises(SystemExit):
        _parse_plugins_args("doctor")


def _minimal_plugin(root: Path, name: str = "minimal") -> Path:
    plugin = root / name
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text(f"name: {name}\n", encoding="utf-8")
    (plugin / "__init__.py").write_text(
        "def register(ctx):\n    pass\n", encoding="utf-8"
    )
    return plugin


def test_doctor_without_target_rejects_home_before_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hermes_cli.plugin_dev as plugin_dev

    monkeypatch.chdir(Path.home())
    monkeypatch.setattr(
        plugin_dev,
        "_copy_plugin_tree",
        lambda *_args, **_kwargs: pytest.fail(
            "Doctor attempted to copy the home directory"
        ),
    )

    report = plugin_dev.doctor_plugin(None)

    assert report.ok is False
    assert "explicit plugin path or installed plugin id" in report.format_text()


@pytest.mark.parametrize(
    ("target", "message"),
    [(Path.home(), "home directory"), (Path("/"), "filesystem root")],
)
def test_doctor_rejects_broad_targets_before_copy(
    monkeypatch: pytest.MonkeyPatch, target: Path, message: str
) -> None:
    import hermes_cli.plugin_dev as plugin_dev

    monkeypatch.setattr(
        plugin_dev,
        "_copy_plugin_tree",
        lambda *_args, **_kwargs: pytest.fail(f"Doctor attempted to copy {target}"),
    )

    report = plugin_dev.doctor_plugin(target)

    assert report.ok is False
    assert message in report.format_text()


def test_doctor_rejects_non_plugin_before_tree_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hermes_cli.plugin_dev as plugin_dev

    broad_directory = tmp_path / "not-a-plugin"
    broad_directory.mkdir()
    (broad_directory / "nested").mkdir()
    monkeypatch.setattr(
        plugin_dev,
        "_validate_plugin_symlinks",
        lambda _path: pytest.fail("Doctor walked a non-plugin directory tree"),
    )

    report = plugin_dev.doctor_plugin(broad_directory)

    assert report.ok is False
    assert "broad or non-plugin target" in report.format_text()


@pytest.mark.skipif(
    os.name == "nt", reason="native Windows symlink creation is optional"
)
def test_doctor_rejects_symlink_that_escapes_without_opening_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hermes_cli.plugin_dev as plugin_dev

    plugin = _minimal_plugin(tmp_path)
    (plugin / "escape").symlink_to(Path("/"), target_is_directory=True)
    real_open = os.open

    def reject_escape_open(path, flags, *args, **kwargs):
        if path == "escape" and kwargs.get("dir_fd") is not None:
            pytest.fail("Doctor dereferenced an escaping plugin symlink")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(plugin_dev.os, "open", reject_escape_open)

    report = plugin_dev.doctor_plugin(plugin)

    assert report.ok is False
    assert "symlink escapes plugin root" in report.format_text()


@pytest.mark.skipif(
    os.name == "nt", reason="native Windows symlink creation is optional"
)
def test_doctor_accepts_relative_symlink_inside_plugin(tmp_path: Path) -> None:
    from hermes_cli.plugin_dev import doctor_plugin

    plugin = _minimal_plugin(tmp_path)
    (plugin / "data.txt").write_text("safe", encoding="utf-8")
    (plugin / "data-link.txt").symlink_to("data.txt")

    report = doctor_plugin(plugin)

    assert report.ok, report.format_text()


@pytest.mark.skipif(
    os.name == "nt", reason="native Windows symlink creation is optional"
)
def test_doctor_rejects_root_symlink_swap_before_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hermes_cli.plugin_dev as plugin_dev

    source = _minimal_plugin(tmp_path / "source", "safe")
    outside = _minimal_plugin(tmp_path / "outside", "outside")
    moved = tmp_path / "moved-safe"
    real_open = os.open
    swap_attempted = False

    def swap_before_root_open(path, flags, *args, **kwargs):
        nonlocal swap_attempted
        if (
            not swap_attempted
            and kwargs.get("dir_fd") is None
            and os.fspath(path) == os.fspath(source)
        ):
            source.rename(moved)
            source.symlink_to(outside, target_is_directory=True)
            swap_attempted = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(plugin_dev.os, "open", swap_before_root_open)

    report = plugin_dev.doctor_plugin(source)

    assert swap_attempted, "Doctor did not pin the validated plugin root before copying"
    assert report.ok is False
    assert report.manifest is None or report.manifest.name != "outside"
    assert "symlink" in report.format_text().lower()


def test_doctor_refuses_unsupported_secure_staging_before_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hermes_cli.plugin_dev as plugin_dev

    plugin = _minimal_plugin(tmp_path / "source")
    doctor_temp = tmp_path / "doctor-temp"
    doctor_temp.mkdir()
    monkeypatch.setattr(plugin_dev.tempfile, "tempdir", str(doctor_temp))
    monkeypatch.setattr(plugin_dev, "_MISSING_SECURE_STAGING_APIS", ("os.scandir(fd)",))

    report = plugin_dev.doctor_plugin(plugin)

    assert report.ok is False
    assert "secure plugin staging is unavailable" in report.format_text().lower()
    assert not list(doctor_temp.glob("hermes-plugin-doctor-*"))


def test_doctor_blocks_temp_destination_inside_plugin_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hermes_cli.plugin_dev as plugin_dev

    plugin = _minimal_plugin(tmp_path)
    monkeypatch.setattr(plugin_dev.tempfile, "tempdir", str(plugin))
    copy_called = False

    def unexpected_copy(*_args, **_kwargs):
        nonlocal copy_called
        copy_called = True
        pytest.fail("Doctor attempted a recursive copy into its own source")

    monkeypatch.setattr(plugin_dev, "_copy_plugin_tree", unexpected_copy)

    report = plugin_dev.doctor_plugin(plugin)

    assert report.ok is False
    assert "temporary destination is inside the plugin source" in report.format_text()
    assert copy_called is False
    assert not list(plugin.glob("hermes-plugin-doctor-*"))


@pytest.mark.parametrize(
    "failure",
    [
        OSError(errno.ENOSPC, "No space left on device"),
        RuntimeError("copy failed"),
        KeyboardInterrupt(),
    ],
    ids=["enospc", "exception", "keyboard-interrupt"],
)
def test_doctor_cleans_temp_when_copy_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    import hermes_cli.plugin_dev as plugin_dev

    plugin = _minimal_plugin(tmp_path / "source")
    doctor_temp = tmp_path / "doctor-temp"
    doctor_temp.mkdir()
    monkeypatch.setattr(plugin_dev.tempfile, "tempdir", str(doctor_temp))

    def interrupted_copy(_source: Path, destination: Path, **_kwargs) -> None:
        destination.mkdir()
        locked = destination / "locked"
        locked.mkdir()
        (locked / "partial").write_text("partial", encoding="utf-8")
        locked.chmod(0o500)
        raise failure

    monkeypatch.setattr(plugin_dev, "_copy_plugin_tree", interrupted_copy)

    if isinstance(failure, Exception):
        report = plugin_dev.doctor_plugin(plugin)
        assert report.ok is False
    else:
        with pytest.raises(type(failure)):
            plugin_dev.doctor_plugin(plugin)

    assert not list(doctor_temp.glob("hermes-plugin-doctor-*"))


def test_doctor_cleans_temp_on_sigterm(tmp_path: Path) -> None:
    plugin = _minimal_plugin(tmp_path / "source")
    doctor_temp = tmp_path / "doctor-temp"
    doctor_temp.mkdir()
    previous_handler_marker = tmp_path / "previous-handler-ran"
    script = textwrap.dedent(
        f"""
        import os
        import signal
        import tempfile
        from pathlib import Path
        import hermes_cli.plugin_dev as plugin_dev

        tempfile.tempdir = {str(doctor_temp)!r}

        def previous_handler(_signum, _frame):
            Path({str(previous_handler_marker)!r}).write_text("ran", encoding="utf-8")

        signal.signal(signal.SIGTERM, previous_handler)

        def terminate(_source, destination, **_kwargs):
            Path(destination).mkdir()
            os.kill(os.getpid(), signal.SIGTERM)

        plugin_dev._copy_plugin_tree = terminate
        plugin_dev.doctor_plugin(Path({str(plugin)!r}))
        """
    )

    completed = subprocess.run([sys.executable, "-c", script], check=False, timeout=30)

    assert completed.returncode != 0
    assert not list(doctor_temp.glob("hermes-plugin-doctor-*"))
    assert previous_handler_marker.read_text(encoding="utf-8") == "ran"


def test_doctor_cleans_temp_after_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hermes_cli.plugin_dev as plugin_dev

    plugin = _minimal_plugin(tmp_path / "source")
    doctor_temp = tmp_path / "doctor-temp"
    doctor_temp.mkdir()
    monkeypatch.setattr(plugin_dev.tempfile, "tempdir", str(doctor_temp))

    report = plugin_dev.doctor_plugin(plugin)

    assert report.ok, report.format_text()
    assert not list(doctor_temp.glob("hermes-plugin-doctor-*"))


def test_doctor_uses_registration_to_reject_bad_hook_and_callback_signature(
    tmp_path: Path,
) -> None:
    from hermes_cli.plugin_dev import doctor_plugin

    plugin = tmp_path / "bad-plugin"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text(
        "\n".join([
            "name: bad-plugin",
            "version: 0.1.0",
            "description: broken contract",
            "provides_hooks:",
            "  - typo_hook",
            "  - pre_tool_call",
        ])
        + "\n",
        encoding="utf-8",
    )
    (plugin / "__init__.py").write_text(
        "def callback(tool_name):\n"
        "    return None\n\n"
        "def register(ctx):\n"
        "    ctx.register_hook('typo_hook', callback)\n"
        "    ctx.register_hook('pre_tool_call', callback)\n",
        encoding="utf-8",
    )

    report = doctor_plugin(plugin)
    messages = "\n".join(f.message for f in report.findings)
    assert report.ok is False
    assert "unknown hook 'typo_hook'" in messages
    assert "must accept **kwargs" in messages


def test_doctor_accepts_manifest_defaults_from_runtime_parser(tmp_path: Path) -> None:
    from hermes_cli.plugin_dev import doctor_plugin

    plugin = tmp_path / "minimal"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text("name: minimal\n", encoding="utf-8")
    (plugin / "__init__.py").write_text(
        "def register(ctx):\n    pass\n", encoding="utf-8"
    )

    report = doctor_plugin(plugin)
    assert report.ok, report.format_text()
    assert report.manifest is not None
    assert report.manifest.kind == "standalone"


def test_doctor_accepts_plugin_yml_manifest(tmp_path: Path) -> None:
    from hermes_cli.plugin_dev import doctor_plugin

    plugin = tmp_path / "yml-plugin"
    plugin.mkdir()
    (plugin / "plugin.yml").write_text("name: yml-plugin\n", encoding="utf-8")
    (plugin / "__init__.py").write_text(
        "def register(ctx):\n    pass\n", encoding="utf-8"
    )

    report = doctor_plugin(plugin)

    assert report.ok, report.format_text()


def test_doctor_accepts_portable_plugin_json(tmp_path: Path) -> None:
    from hermes_cli.agent_plugins import PLUGIN_SCHEMA_V1
    from hermes_cli.plugin_dev import doctor_plugin

    plugin = tmp_path / "portable-plugin"
    plugin.mkdir()
    (plugin / "plugin.json").write_text(
        json.dumps({"$schema": PLUGIN_SCHEMA_V1, "name": "portable.test"}),
        encoding="utf-8",
    )

    report = doctor_plugin(plugin)

    assert report.ok, report.format_text()


def test_doctor_restores_global_tool_policy_and_module_state(tmp_path: Path) -> None:
    import sys

    from hermes_cli.plugin_dev import doctor_plugin
    from tools.registry import registry

    target = tmp_path / "cleanup-plugin"
    target.mkdir()
    (target / "plugin.yaml").write_text(
        "name: cleanup-plugin\nprovides_tools: [cleanup_plugin_ping]\n",
        encoding="utf-8",
    )
    (target / "__init__.py").write_text(
        "import json\n\n"
        "def ping(args, **kwargs):\n    return json.dumps({'ok': True})\n\n"
        "def register(ctx):\n"
        "    ctx.register_tool(name='cleanup_plugin_ping', toolset='cleanup', "
        "schema={'name': 'cleanup_plugin_ping', 'description': 'test', "
        "'parameters': {'type': 'object'}}, handler=ping)\n",
        encoding="utf-8",
    )
    before_policy = dict(registry._plugin_override_policy)
    before_modules = {
        name
        for name in sys.modules
        if name == "hermes_plugins" or name.startswith("hermes_plugins.")
    }

    report = doctor_plugin(target)

    assert report.ok, report.format_text()
    assert report.registered_tools == ("cleanup_plugin_ping",)
    assert registry.get_entry("cleanup_plugin_ping") is None
    assert registry._plugin_override_policy == before_policy
    after_modules = {
        name
        for name in sys.modules
        if name == "hermes_plugins" or name.startswith("hermes_plugins.")
    }
    assert after_modules == before_modules


def test_doctor_blocks_live_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hermes_cli.plugin_dev as plugin_dev

    plugin = tmp_path / "network-plugin"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text("name: network-plugin\n", encoding="utf-8")
    (plugin / "__init__.py").write_text(
        "import socket\n\n"
        "def register(ctx):\n"
        "    socket.create_connection(('example.com', 443))\n",
        encoding="utf-8",
    )
    doctor_temp = tmp_path / "doctor-temp"
    doctor_temp.mkdir()
    monkeypatch.setattr(plugin_dev.tempfile, "tempdir", str(doctor_temp))

    report = plugin_dev.doctor_plugin(plugin)

    assert report.ok is False
    assert "network access is disabled while Plugin Doctor runs" in report.format_text()
    assert not list(doctor_temp.glob("hermes-plugin-doctor-*"))
