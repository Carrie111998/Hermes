import stat
import subprocess

import hermes_constants



def test_resolve_agent_browser_candidate_uses_native_sibling_when_shim_is_not_runnable(
    tmp_path, monkeypatch
):
    package_bin = tmp_path / "node_modules" / "agent-browser" / "bin"
    package_bin.mkdir(parents=True)
    shim = package_bin / "agent-browser.js"
    shim.write_text("#!/usr/bin/env node\n")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    native = package_bin / "agent-browser-linux-x64"
    native.write_text("#!/bin/sh\nexit 0\n")
    native.chmod(native.stat().st_mode | stat.S_IXUSR)
    shim_link = tmp_path / "bin" / "agent-browser"
    shim_link.parent.mkdir()
    shim_link.symlink_to(shim)

    monkeypatch.setattr(
        hermes_constants,
        "agent_browser_runnable",
        lambda path, **_: path == str(native),
    )

    assert hermes_constants.resolve_agent_browser_candidate(str(shim_link)) == str(native)


def test_agent_browser_native_binary_names_cover_linux_x64(monkeypatch):
    monkeypatch.setattr(hermes_constants.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(hermes_constants.sys, "platform", "linux")

    assert hermes_constants.agent_browser_native_binary_names() == (
        "agent-browser-linux-x64",
        "agent-browser-linux-musl-x64",
    )


def test_find_node_executable_on_path_accepts_explicit_path(tmp_path):
    node = tmp_path / "node"
    node.write_text("#!/bin/sh\nexit 0\n")
    node.chmod(node.stat().st_mode | stat.S_IXUSR)

    assert hermes_constants.find_node_executable_on_path("node", str(tmp_path)) == str(node)


def test_agent_browser_candidate_presence_mode_does_not_execute(tmp_path, monkeypatch):
    candidate = tmp_path / "agent-browser"
    candidate.write_text("#!/bin/sh\nexit 0\n")
    candidate.chmod(candidate.stat().st_mode | stat.S_IXUSR)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("presence-only resolution must not execute a candidate")

    monkeypatch.setattr(hermes_constants, "agent_browser_runnable", fail_if_called)
    assert hermes_constants.resolve_agent_browser_candidate(str(candidate), validate=False) == str(candidate)


def test_agent_browser_native_sibling_candidates_follow_resolved_bin_symlink(tmp_path):
    package_bin = tmp_path / "node_modules" / "agent-browser" / "bin"
    package_bin.mkdir(parents=True)
    shim = package_bin / "agent-browser.js"
    shim.write_text("#!/usr/bin/env node\n")
    link = tmp_path / "bin" / "agent-browser"
    link.parent.mkdir()
    link.symlink_to(shim)

    candidates = hermes_constants.agent_browser_native_sibling_candidates(str(link))

    assert package_bin / "agent-browser-linux-x64" in candidates


def test_agent_browser_native_binary_names_cover_windows_arm64(monkeypatch):
    monkeypatch.setattr(hermes_constants.platform, "machine", lambda: "ARM64")
    monkeypatch.setattr(hermes_constants.sys, "platform", "win32")

    assert hermes_constants.agent_browser_native_binary_names() == (
        "agent-browser-win32-arm64.exe",
        "agent-browser-windows-arm64.exe",
    )


def test_agent_browser_native_binary_names_skip_unknown_architecture(monkeypatch):
    monkeypatch.setattr(hermes_constants.platform, "machine", lambda: "riscv64")
    monkeypatch.setattr(hermes_constants.sys, "platform", "linux")

    assert hermes_constants.agent_browser_native_binary_names() == ()


def test_agent_browser_candidate_presence_mode_accepts_native_binary(tmp_path, monkeypatch):
    native = tmp_path / "agent-browser-linux-x64"
    native.write_text("not executed")
    native.chmod(native.stat().st_mode | stat.S_IXUSR)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("presence-only resolution must not execute a candidate")

    monkeypatch.setattr(hermes_constants, "agent_browser_runnable", fail_if_called)
    assert hermes_constants.resolve_agent_browser_candidate(str(native), validate=False) == str(native)


def test_agent_browser_runnable_accepts_explicit_environment(monkeypatch, tmp_path):
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return type("Completed", (), {"returncode": 0})()

    candidate = tmp_path / "agent-browser"
    candidate.write_text("#!/bin/sh\nexit 0\n")
    candidate.chmod(candidate.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert hermes_constants.agent_browser_runnable(str(candidate), env={"PATH": "/custom"}) is True
    assert captured["env"]["PATH"].startswith("/custom")


def test_agent_browser_native_candidate_is_made_executable(tmp_path):
    native = tmp_path / "agent-browser-linux-x64"
    native.write_text("#!/bin/sh\nexit 0\n")
    native.chmod(stat.S_IRUSR | stat.S_IWUSR)

    assert hermes_constants.prepare_agent_browser_native_candidate(native) is True
    assert native.stat().st_mode & stat.S_IXUSR


def test_agent_browser_native_candidate_rejects_missing_file(tmp_path):
    assert hermes_constants.prepare_agent_browser_native_candidate(
        tmp_path / "agent-browser-linux-x64"
    ) is False


def test_resolve_agent_browser_candidate_accepts_runnable_path_candidate(tmp_path):
    candidate = tmp_path / "agent-browser-helper"
    candidate.write_text("#!/bin/sh\nexit 0\n")
    candidate.chmod(candidate.stat().st_mode | stat.S_IXUSR)

    assert hermes_constants.resolve_agent_browser_candidate(str(candidate)) == str(candidate)


def test_native_sibling_candidates_are_deduplicated(tmp_path):
    package_bin = tmp_path / "node_modules" / "agent-browser" / "bin"
    package_bin.mkdir(parents=True)
    shim = package_bin / "agent-browser.js"
    shim.write_text("#!/usr/bin/env node\n")
    link = package_bin / ".bin" / "agent-browser"
    link.parent.mkdir()
    link.symlink_to(shim)

    candidates = hermes_constants.agent_browser_native_sibling_candidates(str(link))

    assert len(candidates) == len(set(candidates))


def test_agent_browser_native_binary_names_cover_macos(monkeypatch):
    monkeypatch.setattr(hermes_constants.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(hermes_constants.sys, "platform", "darwin")

    assert hermes_constants.agent_browser_native_binary_names() == (
        "agent-browser-darwin-arm64",
        "agent-browser-macos-arm64",
    )


def test_prepare_native_candidate_preserves_existing_permissions(tmp_path):
    native = tmp_path / "agent-browser-linux-x64"
    native.write_text("#!/bin/sh\nexit 0\n")
    native.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    before = native.stat().st_mode

    assert hermes_constants.prepare_agent_browser_native_candidate(native) is True
    assert native.stat().st_mode == before


def test_resolve_agent_browser_candidate_passes_probe_environment(tmp_path, monkeypatch):
    candidate = tmp_path / "agent-browser"
    candidate.write_text("#!/bin/sh\nexit 0\n")
    candidate.chmod(candidate.stat().st_mode | stat.S_IXUSR)
    seen = {}

    def fake_runnable(path, **kwargs):
        seen.update(kwargs)
        return True

    monkeypatch.setattr(hermes_constants, "agent_browser_runnable", fake_runnable)
    assert hermes_constants.resolve_agent_browser_candidate(
        str(candidate), env={"PATH": "/probe"}
    ) == str(candidate)
    assert seen["env"] == {"PATH": "/probe"}


def test_native_sibling_resolution_returns_none_when_all_probes_fail(tmp_path, monkeypatch):
    package_bin = tmp_path / "node_modules" / "agent-browser" / "bin"
    package_bin.mkdir(parents=True)
    shim = package_bin / "agent-browser.js"
    shim.write_text("#!/usr/bin/env node\n")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    native = package_bin / "agent-browser-linux-x64"
    native.write_text("not runnable")
    native.chmod(native.stat().st_mode | stat.S_IXUSR)
    link = tmp_path / "bin" / "agent-browser"
    link.parent.mkdir()
    link.symlink_to(shim)

    monkeypatch.setattr(hermes_constants, "agent_browser_runnable", lambda *_args, **_kwargs: False)
    assert hermes_constants.resolve_agent_browser_candidate(str(link)) is None
