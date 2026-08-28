"""Adversarial execution-authority tests for webhook filters and scripts."""

from __future__ import annotations

import json
import mmap
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

import gateway.platforms.webhook_filters as webhook_filters
from gateway.platforms.webhook_filters import (
    BoundedFileSnapshotChanged,
    MAX_FILTER_FILE_SNAPSHOT_BYTES,
    MAX_SCRIPT_OUTPUT_STREAM_BYTES,
    MAX_SCRIPT_SNAPSHOT_BYTES,
    WebhookRouteProcessor,
    WebhookScriptDisposition,
    read_bounded_regular_file_snapshot,
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


def _prepare(
    tmp_path: Path,
    *,
    name: str,
    source: str,
) -> tuple[WebhookRouteProcessor, object, Path]:
    scripts = tmp_path / "scripts"
    scripts.mkdir(exist_ok=True)
    script = scripts / name
    script.write_text(source, encoding="utf-8")
    processor = WebhookRouteProcessor(script_timeout_seconds=5)
    prepared, error = processor.prepare_route_script(name)
    if prepared is None and name.endswith((".sh", ".bash")):
        pytest.skip(error or "bash is unavailable")
    assert error is None
    assert prepared is not None
    return processor, prepared, script


def test_python_script_never_rejoins_live_process_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("CUSTOM_WEBHOOK_SECRET", "BASH_ENV", "ENV", "PYTHONPATH"):
        monkeypatch.setenv(name, f"before-{name}")
    processor, prepared, _script = _prepare(
        tmp_path,
        name="environment.py",
        source=(
            "import json, os\n"
            "names = ('CUSTOM_WEBHOOK_SECRET', 'BASH_ENV', 'ENV', 'PYTHONPATH')\n"
            "result = {name: os.environ.get(name) for name in names}\n"
            "result.update({\n"
            "    'HOME': os.environ.get('HOME'),\n"
            "    'HERMES_HOME': os.environ.get('HERMES_HOME'),\n"
            "    'PATH': os.environ.get('PATH'),\n"
            "    'cwd': os.getcwd(),\n"
            "})\n"
            "print(json.dumps(result))\n"
        ),
    )

    for name in ("CUSTOM_WEBHOOK_SECRET", "BASH_ENV", "ENV", "PYTHONPATH"):
        monkeypatch.setenv(name, f"after-{name}")
    monkeypatch.setenv("PATH", str(tmp_path / "attacker-bin"))

    result = processor.run_prepared_script(prepared, {})

    assert result.disposition is WebhookScriptDisposition.CONTINUE
    assert result.payload is not None
    assert result.payload == {
        "CUSTOM_WEBHOOK_SECRET": None,
        "BASH_ENV": None,
        "ENV": None,
        "PYTHONPATH": None,
        "HOME": result.payload["cwd"],
        "HERMES_HOME": None,
        "PATH": os.defpath,
        "cwd": result.payload["cwd"],
    }
    assert not Path(result.payload["cwd"]).exists()


def test_bash_script_ignores_late_startup_hooks_and_relative_helpers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processor, prepared, script = _prepare(
        tmp_path,
        name="transform.sh",
        source=(
            "if [ -f ./helper.sh ]; then\n"
            "  . ./helper.sh\n"
            "else\n"
            "  printf '%s\\n' '{\"shell\":\"clean\"}'\n"
            "fi\n"
        ),
    )
    marker = tmp_path / "startup-hook-ran"
    hook = tmp_path / "injected-bash-env.sh"
    hook.write_text(f"touch {shlex.quote(str(marker))}\n", encoding="utf-8")
    (script.parent / "helper.sh").write_text(
        "printf '%s\\n' '{\"shell\":\"helper-loaded\"}'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BASH_ENV", str(hook))
    monkeypatch.setenv("ENV", str(hook))
    monkeypatch.chdir(script.parent)

    result = processor.run_prepared_script(prepared, {})

    assert result.disposition is WebhookScriptDisposition.CONTINUE
    assert result.payload == {"shell": "clean"}
    assert not marker.exists()


def test_python_script_ignores_replaced_cwd_helpers_and_pythonpath(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper_name = "webhook_mutated_helper_6f7c78"
    processor, prepared, script = _prepare(
        tmp_path,
        name="isolated.py",
        source=(
            "import json, os\n"
            "try:\n"
            f"    __import__({helper_name!r})\n"
            "except ModuleNotFoundError:\n"
            "    helper = 'isolated'\n"
            "else:\n"
            "    helper = 'loaded'\n"
            "print(json.dumps({\n"
            "    'helper': helper,\n"
            "    'cwd': os.getcwd(),\n"
            "    'file': __file__,\n"
            "}))\n"
        ),
    )

    original_scripts = tmp_path / "scripts-original"
    script.parent.rename(original_scripts)
    replacement_scripts = tmp_path / "scripts"
    replacement_scripts.mkdir()
    (replacement_scripts / f"{helper_name}.py").write_text(
        "raise RuntimeError('replacement cwd helper executed')\n",
        encoding="utf-8",
    )
    attacker_path = tmp_path / "attacker-pythonpath"
    attacker_path.mkdir()
    (attacker_path / f"{helper_name}.py").write_text(
        "raise RuntimeError('PYTHONPATH helper executed')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(attacker_path))
    monkeypatch.chdir(replacement_scripts)

    result = processor.run_prepared_script(prepared, {})

    assert result.disposition is WebhookScriptDisposition.CONTINUE
    assert result.payload is not None
    assert result.payload["helper"] == "isolated"
    assert result.payload["file"].startswith("hermes-webhook-script:")
    assert str(script) not in result.payload["file"]
    assert result.payload["cwd"] not in {
        str(original_scripts),
        str(replacement_scripts),
        str(attacker_path),
    }
    assert not Path(result.payload["cwd"]).exists()


def test_python_script_cannot_reopen_replaced_original_file(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "python-replacement-ran"
    processor, prepared, script = _prepare(
        tmp_path,
        name="self-reopen.py",
        source=(
            "from pathlib import Path\n"
            "try:\n"
            "    replacement = Path(__file__).read_text(encoding='utf-8')\n"
            "except OSError:\n"
            "    print('{\"reopened\":false}')\n"
            "else:\n"
            "    exec(compile(replacement, __file__, 'exec'))\n"
        ),
    )
    script.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n"
        "print('{\"reopened\":true}')\n",
        encoding="utf-8",
    )

    result = processor.run_prepared_script(prepared, {})

    assert result.disposition is WebhookScriptDisposition.CONTINUE
    assert result.payload == {"reopened": False}
    assert not marker.exists()


def test_python_snapshot_preserves_module_preamble_semantics(
    tmp_path: Path,
) -> None:
    processor, prepared, _script = _prepare(
        tmp_path,
        name="future-import.py",
        source=(
            '"""frozen transform docstring"""\n'
            "from __future__ import annotations\n"
            "import json\n"
            "def transform(value: MissingType) -> MissingResult:\n"
            "    return value\n"
            "print(json.dumps({\n"
            "    'doc': __doc__,\n"
            "    'annotation': transform.__annotations__['value'],\n"
            "}))\n"
        ),
    )

    result = processor.run_prepared_script(prepared, {})

    assert result.disposition is WebhookScriptDisposition.CONTINUE
    assert result.payload == {
        "doc": "frozen transform docstring",
        "annotation": "MissingType",
    }


def test_bash_script_cannot_source_replaced_original_file(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "bash-replacement-ran"
    processor, prepared, script = _prepare(
        tmp_path,
        name="self-source.sh",
        source=(
            'if [ -r "$0" ]; then\n'
            '  . "$0"\n'
            "else\n"
            "  printf '%s\\n' '{\"reopened\":false}'\n"
            "fi\n"
        ),
    )
    script.write_text(
        f"touch {shlex.quote(str(marker))}\nprintf '%s\\n' '{{\"reopened\":true}}'\n",
        encoding="utf-8",
    )

    result = processor.run_prepared_script(prepared, {})

    assert result.disposition is WebhookScriptDisposition.CONTINUE
    assert result.payload == {"reopened": False}
    assert not marker.exists()


def test_python_script_has_no_mutable_site_or_pythonpath_imports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pythonpath_module = "webhook_pythonpath_helper_84ea23"
    user_site_module = "webhook_user_site_helper_84ea23"
    pythonpath = tmp_path / "late-pythonpath"
    pythonpath.mkdir()
    (pythonpath / f"{pythonpath_module}.py").write_text(
        "raise RuntimeError('PYTHONPATH helper ran')\n",
        encoding="utf-8",
    )
    user_site = (
        tmp_path
        / ".local"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    user_site.mkdir(parents=True)
    (user_site / f"{user_site_module}.py").write_text(
        "raise RuntimeError('user-site helper ran')\n",
        encoding="utf-8",
    )
    processor, prepared, _script = _prepare(
        tmp_path,
        name="site-isolated.py",
        source=(
            "import importlib, json, sys\n"
            "def probe(name):\n"
            "    try:\n"
            "        importlib.import_module(name)\n"
            "    except ModuleNotFoundError:\n"
            "        return 'blocked'\n"
            "    return 'loaded'\n"
            f"names = ({pythonpath_module!r}, {user_site_module!r}, 'aiohttp')\n"
            "print(json.dumps({\n"
            "    'isolated': sys.flags.isolated,\n"
            "    'no_site': sys.flags.no_site,\n"
            "    'imports': {name: probe(name) for name in names},\n"
            "}))\n"
        ),
    )
    monkeypatch.setenv("PYTHONPATH", str(pythonpath))

    result = processor.run_prepared_script(prepared, {})

    assert result.disposition is WebhookScriptDisposition.CONTINUE
    assert result.payload == {
        "isolated": 1,
        "no_site": 1,
        "imports": {
            pythonpath_module: "blocked",
            user_site_module: "blocked",
            "aiohttp": "blocked",
        },
    }


def _pid_is_effectively_alive(pid: int) -> bool:
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        fields = proc_stat.read_text(encoding="utf-8").split()
    except OSError:
        fields = []
    if len(fields) >= 3 and fields[2] == "Z":
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.skipif(os.name != "posix", reason="process groups require POSIX")
@pytest.mark.live_system_guard_bypass
def test_timeout_kills_descendant_that_ignores_sigterm(
    tmp_path: Path,
) -> None:
    child_pid_file = tmp_path / "sigterm-ignoring-child.pid"
    child_source = (
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(child_pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "time.sleep(60)\n"
    )
    processor, prepared, _script = _prepare(
        tmp_path,
        name="spawn-child.py",
        source=(
            "import subprocess, sys, time\n"
            "from pathlib import Path\n"
            f"marker = Path({str(child_pid_file)!r})\n"
            f"subprocess.Popen([sys.executable, '-I', '-S', '-c', {child_source!r}])\n"
            "deadline = time.monotonic() + 2\n"
            "while not marker.exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
            "time.sleep(60)\n"
        ),
    )
    processor.script_timeout_seconds = 1

    started = time.monotonic()
    result = processor.run_prepared_script(prepared, {})
    elapsed = time.monotonic() - started

    assert result.disposition is WebhookScriptDisposition.INDETERMINATE
    assert "timed out" in str(result.error)
    # The canonical runner executes this process-heavy file beside 17 other
    # subprocesses.  Preserve a hard bound that catches the 60-second leak
    # without treating scheduler contention as a product failure.
    assert elapsed < 20
    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    try:
        while _pid_is_effectively_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not _pid_is_effectively_alive(child_pid)
    finally:
        if _pid_is_effectively_alive(child_pid):
            os.kill(child_pid, signal.SIGKILL)


@pytest.mark.skipif(os.name != "posix", reason="process groups require POSIX")
@pytest.mark.live_system_guard_bypass
def test_normal_exit_kills_descendant_that_closed_output_pipes(
    tmp_path: Path,
) -> None:
    child_pid_file = tmp_path / "normal-exit-child.pid"
    child_source = (
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(child_pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "time.sleep(60)\n"
    )
    processor, prepared, _script = _prepare(
        tmp_path,
        name="exit-after-child.py",
        source=(
            "import subprocess, sys, time\n"
            "from pathlib import Path\n"
            f"marker = Path({str(child_pid_file)!r})\n"
            "subprocess.Popen(\n"
            f"    [sys.executable, '-I', '-S', '-c', {child_source!r}],\n"
            "    stdin=subprocess.DEVNULL,\n"
            "    stdout=subprocess.DEVNULL,\n"
            "    stderr=subprocess.DEVNULL,\n"
            ")\n"
            "deadline = time.monotonic() + 2\n"
            "while not marker.exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
            "print('{}')\n"
        ),
    )

    started = time.monotonic()
    result = processor.run_prepared_script(prepared, {})
    elapsed = time.monotonic() - started

    assert result.disposition is WebhookScriptDisposition.CONTINUE
    assert result.payload == {}
    assert elapsed < 2
    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    try:
        while _pid_is_effectively_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not _pid_is_effectively_alive(child_pid)
    finally:
        if _pid_is_effectively_alive(child_pid):
            os.kill(child_pid, signal.SIGKILL)


def test_prepared_interpreter_contract_cannot_be_substituted(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "substituted-interpreter-ran"
    processor, prepared, _script = _prepare(
        tmp_path,
        name="exact.py",
        source=(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('ran')\n"
            "print('{}')\n"
        ),
    )

    result = processor.run_prepared_script(
        replace(prepared, interpreter_kind="bash"),
        {},
    )

    assert result.disposition is WebhookScriptDisposition.FAILED
    assert "snapshot digest" in str(result.error)
    assert not marker.exists()


def _write_fifo_once(path: Path) -> None:
    try:
        fd = os.open(path, os.O_WRONLY | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return
    try:
        os.write(fd, b"\n")
    finally:
        os.close(fd)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires os.mkfifo")
def test_compatibility_filter_file_fifo_fails_promptly(
    tmp_path: Path,
) -> None:
    fifo = tmp_path / "filter.fifo"
    os.mkfifo(fifo)
    escape = threading.Timer(2.0, _write_fifo_once, args=(fifo,))
    escape.daemon = True
    escape.start()
    started = time.monotonic()
    try:
        values = webhook_filters._load_filter_file_values(str(fifo))
    finally:
        escape.cancel()

    assert time.monotonic() - started < 1.0
    assert values is None


@pytest.mark.skipif(os.name != "posix", reason="device-file check is POSIX-only")
def test_compatibility_filter_file_device_fails_closed() -> None:
    assert webhook_filters._load_filter_file_values(os.devnull) is None


def test_compatibility_filter_file_oversize_fails_closed(
    tmp_path: Path,
) -> None:
    oversized = tmp_path / "oversized-filter.json"
    oversized.write_bytes(b"x" * (MAX_FILTER_FILE_SNAPSHOT_BYTES + 1))

    assert webhook_filters._load_filter_file_values(str(oversized)) is None


def test_compatibility_filter_file_deep_json_fails_closed(
    tmp_path: Path,
) -> None:
    deeply_nested = tmp_path / "deep-filter.json"
    deeply_nested.write_text("[" * 2000 + "0" + "]" * 2000, encoding="utf-8")

    assert webhook_filters._load_filter_file_values(str(deeply_nested)) is None


def test_compatibility_filter_file_symlink_to_regular_remains_supported(
    tmp_path: Path,
) -> None:
    target = tmp_path / "filter-values.json"
    target.write_text('["one", "two"]', encoding="utf-8")
    link = tmp_path / "filter-link.json"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    assert webhook_filters._load_filter_file_values(str(link)) == ["one", "two"]


def test_bounded_snapshot_rejects_file_changed_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "changing-filter.json"
    target.write_bytes(b'["before"]')
    real_read = webhook_filters.os.read
    changed = False

    def mutate_after_first_read(fd: int, size: int) -> bytes:
        nonlocal changed
        chunk = real_read(fd, size)
        if chunk and not changed:
            changed = True
            with target.open("ab") as handle:
                handle.write(b" ")
                handle.flush()
                os.fsync(handle.fileno())
        return chunk

    monkeypatch.setattr(webhook_filters.os, "read", mutate_after_first_read)

    with pytest.raises(BoundedFileSnapshotChanged, match="changed while being read"):
        read_bounded_regular_file_snapshot(target, max_bytes=1024)


def test_bounded_snapshot_double_read_detects_metadata_invisible_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "metadata-invisible.bin"
    target.write_bytes(b"a" * (128 * 1024))
    real_read = webhook_filters.os.read
    changed = False

    def rewrite_after_first_chunk(fd: int, size: int) -> bytes:
        nonlocal changed
        chunk = real_read(fd, size)
        if chunk and not changed:
            changed = True
            target.write_bytes(b"b" * (128 * 1024))
        return chunk

    monkeypatch.setattr(webhook_filters.os, "read", rewrite_after_first_chunk)
    monkeypatch.setattr(webhook_filters, "_stat_identity", lambda _value: (1,))

    with pytest.raises(BoundedFileSnapshotChanged, match="changed while being read"):
        read_bounded_regular_file_snapshot(target, max_bytes=128 * 1024)


def test_bounded_snapshot_double_read_rejects_mmap_torn_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "mmap-changing.bin"
    target.write_bytes(b"a" * (128 * 1024))
    real_read = webhook_filters.os.read
    changed = False

    with target.open("r+b") as handle, mmap.mmap(handle.fileno(), 0) as mapping:
        mapping[0:1] = b"a"

        def mutate_after_first_chunk(fd: int, size: int) -> bytes:
            nonlocal changed
            chunk = real_read(fd, size)
            if chunk and not changed:
                changed = True
                mapping[:] = b"b" * len(mapping)
            return chunk

        monkeypatch.setattr(webhook_filters.os, "read", mutate_after_first_chunk)
        monkeypatch.setattr(webhook_filters, "_stat_identity", lambda _value: (1,))

        with pytest.raises(
            BoundedFileSnapshotChanged,
            match="changed while being read",
        ):
            read_bounded_regular_file_snapshot(target, max_bytes=128 * 1024)


def test_regex_worker_uses_isolated_minimal_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    real_popen = webhook_filters.subprocess.Popen

    def capture_popen(argv: list[str], **kwargs: object):
        captured["argv"] = argv
        captured.update(kwargs)
        return real_popen(argv, **kwargs)

    monkeypatch.setenv("REGEX_WORKER_SECRET", "must-not-leak")
    monkeypatch.setenv("LD_PRELOAD", str(tmp_path / "attacker.so"))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "attacker-pythonpath"))
    monkeypatch.setattr(webhook_filters.subprocess, "Popen", capture_popen)

    assert webhook_filters._bounded_regex_search("^safe$", "safe") is True

    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[1:6] == ["-I", "-S", "-X", "utf8", "-c"]
    if os.name == "posix":
        assert argv[0].startswith(("/proc/self/fd/", "/dev/fd/"))
        pass_fds = captured["pass_fds"]
        assert isinstance(pass_fds, tuple)
        assert len(pass_fds) == 1
        assert argv[0].endswith(f"/{pass_fds[0]}")
    else:
        assert "pass_fds" not in captured
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert "REGEX_WORKER_SECRET" not in environment
    assert "LD_PRELOAD" not in environment
    assert "PYTHONPATH" not in environment
    cwd = captured["cwd"]
    assert isinstance(cwd, str)
    assert environment["HOME"] == cwd
    assert not Path(cwd).exists()


def test_script_resolution_race_cannot_follow_outside_root_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "route.py"
    script.write_text('print(\'{"origin":"inside"}\')\n', encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text('print(\'{"origin":"outside"}\')\n', encoding="utf-8")
    original_resolver = webhook_filters._resolve_script_path

    def replace_after_resolution(value: object):
        resolved, error = original_resolver(value)
        assert resolved == script
        assert error is None
        script.unlink()
        try:
            script.symlink_to(outside)
        except OSError as exc:
            pytest.skip(f"symlink creation is unavailable: {exc}")
        return resolved, error

    monkeypatch.setattr(
        webhook_filters,
        "_resolve_script_path",
        replace_after_resolution,
    )

    prepared, error = WebhookRouteProcessor().prepare_route_script("route.py")

    assert prepared is None
    assert "cannot be read" in str(error)


def test_script_root_symlink_loop_race_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "route.py"
    script.write_text("print('{}')\n", encoding="utf-8")
    original_resolver = webhook_filters._resolve_script_path

    def loop_root_after_resolution(value: object):
        resolved, error = original_resolver(value)
        assert resolved == script
        assert error is None
        scripts.rename(tmp_path / "original-scripts")
        try:
            scripts.symlink_to("scripts", target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlink creation is unavailable: {exc}")
        return resolved, error

    monkeypatch.setattr(
        webhook_filters,
        "_resolve_script_path",
        loop_root_after_resolution,
    )

    prepared, error = WebhookRouteProcessor().prepare_route_script("route.py")

    assert prepared is None
    assert "root cannot be resolved" in str(error)


def test_bash_resolution_ignores_mutable_process_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_bash = webhook_filters.shutil.which("bash", path=os.defpath)
    if default_bash is None:
        pytest.skip("bash is unavailable on the platform default path")
    attacker_bin = tmp_path / "attacker-bin"
    attacker_bin.mkdir()
    attacker_bash = attacker_bin / "bash"
    attacker_bash.write_text(
        "#!/bin/sh\nprintf '%s\\n' '{\"interpreter\":\"attacker\"}'\n",
        encoding="utf-8",
    )
    attacker_bash.chmod(0o700)
    monkeypatch.setenv("PATH", str(attacker_bin))

    processor, prepared, _script = _prepare(
        tmp_path,
        name="route.sh",
        source="printf '%s\\n' '{\"interpreter\":\"default\"}'\n",
    )

    assert prepared.interpreter != str(attacker_bash)
    result = processor.run_prepared_script(prepared, {})
    assert result.disposition is WebhookScriptDisposition.CONTINUE
    assert result.payload == {"interpreter": "default"}


def test_bash_interpreter_symlink_loop_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interpreter = tmp_path / "loop-bash"
    try:
        interpreter.symlink_to(interpreter.name)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    monkeypatch.setattr(
        webhook_filters.shutil,
        "which",
        lambda _name, path=None: str(interpreter),
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "route.sh").write_text("printf '{}\\n'\n", encoding="utf-8")

    prepared, error = WebhookRouteProcessor().prepare_route_script("route.sh")

    assert prepared is None
    assert "interpreter is unavailable" in str(error)


@pytest.mark.skipif(os.name != "posix", reason="execute-by-fd requires POSIX")
def test_interpreter_replacement_at_spawn_executes_verified_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_bash = tmp_path / "frozen-bash"
    fake_bash.write_text(
        "#!/bin/sh\nprintf '%s\\n' '{\"interpreter\":\"v1\"}'\n",
        encoding="utf-8",
    )
    fake_bash.chmod(0o700)
    monkeypatch.setattr(
        webhook_filters.shutil,
        "which",
        lambda _name, path=None: str(fake_bash),
    )
    processor, prepared, _script = _prepare(
        tmp_path,
        name="route.sh",
        source="printf '%s\\n' '{\"source\":\"route\"}'\n",
    )
    real_popen = webhook_filters.subprocess.Popen
    replaced = False

    def replace_then_spawn(*args, **kwargs):
        nonlocal replaced
        if not replaced:
            replacement = tmp_path / "replacement-bash"
            replacement.write_text(
                "#!/bin/sh\nprintf '%s\\n' '{\"interpreter\":\"v2\"}'\n",
                encoding="utf-8",
            )
            replacement.chmod(0o700)
            os.replace(replacement, fake_bash)
            replaced = True
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(webhook_filters.subprocess, "Popen", replace_then_spawn)

    result = processor.run_prepared_script(prepared, {})

    assert result.disposition is WebhookScriptDisposition.CONTINUE
    assert result.payload == {"interpreter": "v1"}
    rejected = processor.run_prepared_script(prepared, {})
    assert rejected.disposition is WebhookScriptDisposition.FAILED
    assert "interpreter content changed" in str(rejected.error)


@pytest.mark.skipif(os.name != "posix", reason="execute-by-fd requires POSIX")
def test_interpreter_in_place_overwrite_at_spawn_executes_sealed_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_bash = tmp_path / "mutable-bash"
    fake_bash.write_text(
        "#!/bin/sh\nprintf '%s\\n' '{\"interpreter\":\"v1\"}'\n",
        encoding="utf-8",
    )
    fake_bash.chmod(0o700)
    monkeypatch.setattr(
        webhook_filters.shutil,
        "which",
        lambda _name, path=None: str(fake_bash),
    )
    processor, prepared, _script = _prepare(
        tmp_path,
        name="route.sh",
        source="printf '%s\\n' '{\"source\":\"route\"}'\n",
    )
    real_popen = webhook_filters.subprocess.Popen
    overwritten = False

    def overwrite_then_spawn(*args, **kwargs):
        nonlocal overwritten
        if not overwritten:
            fake_bash.write_text(
                "#!/bin/sh\nprintf '%s\\n' '{\"interpreter\":\"v2\"}'\n",
                encoding="utf-8",
            )
            fake_bash.chmod(0o700)
            overwritten = True
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(webhook_filters.subprocess, "Popen", overwrite_then_spawn)

    result = processor.run_prepared_script(prepared, {})

    assert result.disposition is WebhookScriptDisposition.CONTINUE
    assert result.payload == {"interpreter": "v1"}


@pytest.mark.skipif(os.name != "posix", reason="execute-by-fd requires POSIX")
def test_no_memfd_interpreter_snapshot_fallback_executes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not hasattr(webhook_filters.os, "memfd_create"):
        pytest.skip("memfd fallback is already active")
    monkeypatch.setattr(
        webhook_filters.os,
        "memfd_create",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("unsupported")),
    )
    processor, prepared, _script = _prepare(
        tmp_path,
        name="fallback.sh",
        source="printf '%s\\n' '{\"fallback\":true}'\n",
    )

    result = processor.run_prepared_script(prepared, {})

    assert result.disposition is WebhookScriptDisposition.CONTINUE
    assert result.payload == {"fallback": True}


def test_full_size_python_snapshot_executes_without_argv_expansion(
    tmp_path: Path,
) -> None:
    tail = 'print(\'{"size":"accepted"}\')\n'
    source = "\n" * (MAX_SCRIPT_SNAPSHOT_BYTES - len(tail)) + tail
    assert len(source.encode("utf-8")) == MAX_SCRIPT_SNAPSHOT_BYTES
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "full-size.py").write_bytes(source.encode("utf-8"))
    processor = WebhookRouteProcessor(script_timeout_seconds=5)
    prepared, error = processor.prepare_route_script("full-size.py")
    assert error is None
    assert prepared is not None

    result = processor.run_prepared_script(prepared, {})

    assert result.disposition is WebhookScriptDisposition.CONTINUE
    assert result.payload == {"size": "accepted"}


@pytest.mark.live_system_guard_bypass
def test_cancellation_event_kills_running_script_promptly(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "cancelled-script.pid"
    processor, prepared, _script = _prepare(
        tmp_path,
        name="cancel.py",
        source=(
            "import os, time\n"
            "from pathlib import Path\n"
            f"Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
            "time.sleep(60)\n"
        ),
    )
    cancellation_event = threading.Event()
    observed: dict[str, object] = {}

    def execute() -> None:
        observed["result"] = processor.run_prepared_script(
            prepared,
            {},
            cancellation_event=cancellation_event,
        )

    worker = threading.Thread(target=execute)
    worker.start()
    deadline = time.monotonic() + 5
    pid_text = ""
    while time.monotonic() < deadline:
        try:
            pid_text = pid_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            pass
        if pid_text:
            break
        time.sleep(0.01)
    assert pid_text
    pid = int(pid_text)
    cancellation_event.set()
    worker.join(timeout=3)

    assert not worker.is_alive()
    result = observed["result"]
    assert result.disposition is WebhookScriptDisposition.INDETERMINATE
    assert "cancelled" in str(result.error)
    assert not _pid_is_effectively_alive(pid)


@pytest.mark.live_system_guard_bypass
def test_cancellation_after_leader_exit_is_still_indeterminate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processor, prepared, _script = _prepare(
        tmp_path,
        name="fast-exit.py",
        source="print('{}')\n",
    )
    cancellation_event = threading.Event()
    reaped = threading.Event()
    release = threading.Event()
    observed: dict[str, object] = {}
    real_reap = webhook_filters._reap_finished_script_process_group

    def gate_after_reap(process) -> None:
        real_reap(process)
        reaped.set()
        assert release.wait(timeout=3)

    monkeypatch.setattr(
        webhook_filters,
        "_reap_finished_script_process_group",
        gate_after_reap,
    )

    worker = threading.Thread(
        target=lambda: observed.setdefault(
            "result",
            processor.run_prepared_script(
                prepared,
                {},
                cancellation_event=cancellation_event,
            ),
        )
    )
    worker.start()
    assert reaped.wait(timeout=3)
    cancellation_event.set()
    release.set()
    worker.join(timeout=3)

    assert not worker.is_alive()
    result = observed["result"]
    assert result.disposition is WebhookScriptDisposition.INDETERMINATE
    assert "cancelled" in str(result.error)


def test_cancellation_during_input_handoff_prevents_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processor, prepared, _script = _prepare(
        tmp_path,
        name="cancel-before-spawn.py",
        source="print('{}')\n",
    )
    cancellation_event = threading.Event()
    handoff_started = threading.Event()
    release = threading.Event()
    observed: dict[str, object] = {}
    real_write_all = webhook_filters._write_all_to_fd

    def gated_write(fd: int, content: bytes) -> None:
        handoff_started.set()
        assert release.wait(timeout=3)
        real_write_all(fd, content)

    def unexpected_spawn(*_args, **_kwargs):
        raise AssertionError("cancelled script was spawned")

    monkeypatch.setattr(webhook_filters, "_write_all_to_fd", gated_write)
    monkeypatch.setattr(webhook_filters.subprocess, "Popen", unexpected_spawn)
    worker = threading.Thread(
        target=lambda: observed.setdefault(
            "result",
            processor.run_prepared_script(
                prepared,
                {"content": "x" * 128_000},
                cancellation_event=cancellation_event,
            ),
        )
    )
    worker.start()
    assert handoff_started.wait(timeout=3)
    cancellation_event.set()
    release.set()
    worker.join(timeout=3)

    assert not worker.is_alive()
    result = observed["result"]
    assert result.disposition is WebhookScriptDisposition.INDETERMINATE
    assert "cancelled" in str(result.error)


@pytest.mark.parametrize(
    ("pattern", "value"),
    [("\ud800", "safe"), ("safe", "\ud800")],
)
def test_regex_lone_surrogate_fails_closed(pattern: str, value: str) -> None:
    assert webhook_filters._bounded_regex_search(pattern, value) is None


@pytest.mark.parametrize(
    "value",
    [["\ud800"], {"nested": "\ud800"}],
)
def test_regex_nested_lone_surrogate_fails_closed(value: object) -> None:
    assert not WebhookRouteProcessor().filter_matches(
        {"field": "value", "regex": r"ud800"},
        {"value": value},
        "push",
        {},
    )


def test_regex_worker_preserves_non_ascii_unicode_semantics() -> None:
    assert webhook_filters._bounded_regex_search(r"^\w+$", "é") is True


@pytest.mark.skipif(os.name != "posix", reason="execute-by-fd requires POSIX")
def test_regex_interpreter_replacement_at_spawn_uses_verified_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interpreter_link = tmp_path / "regex-python"
    try:
        interpreter_link.symlink_to(Path(sys.executable).resolve())
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    marker = tmp_path / "replacement-interpreter-ran"
    replacement = tmp_path / "replacement-python"
    replacement.write_text(
        f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nprintf 1\n",
        encoding="utf-8",
    )
    replacement.chmod(0o700)
    monkeypatch.setattr(
        webhook_filters,
        "_REGEX_INTERPRETER_PATH",
        str(interpreter_link),
    )
    real_popen = webhook_filters.subprocess.Popen

    def replace_then_popen(*args, **kwargs):
        os.replace(replacement, interpreter_link)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(webhook_filters.subprocess, "Popen", replace_then_popen)

    assert webhook_filters._bounded_regex_search("does-not-match", "safe") is False
    assert not marker.exists()


def test_regex_mutable_launch_path_is_not_reopened_before_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "precall-replacement-ran"
    replacement = tmp_path / "replacement-python"
    replacement.write_text(
        f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nprintf 1\n",
        encoding="utf-8",
    )
    replacement.chmod(0o700)
    monkeypatch.setattr(
        webhook_filters,
        "_REGEX_INTERPRETER_PATH",
        str(replacement),
    )

    assert webhook_filters._bounded_regex_search("does-not-match", "safe") is False
    assert not marker.exists()


def test_regex_retained_interpreter_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_spawn(*_args, **_kwargs):
        raise AssertionError("regex worker spawned without interpreter authority")

    monkeypatch.setattr(webhook_filters, "_REGEX_INTERPRETER_FD", -1)
    monkeypatch.setattr(webhook_filters.subprocess, "Popen", unexpected_spawn)

    assert webhook_filters._bounded_regex_search("safe", "safe") is None


def test_regex_nodes_do_not_rehash_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_rehash(*_args, **_kwargs):
        raise AssertionError("regex node rehashed the retained interpreter")

    monkeypatch.setattr(
        webhook_filters,
        "_open_stable_regular_file_digest",
        unexpected_rehash,
    )

    assert webhook_filters._bounded_regex_search("safe", "safe") is True
    assert webhook_filters._bounded_regex_search("other", "safe") is False


def test_regex_worker_startup_delay_is_not_charged_to_match_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        webhook_filters,
        "_REGEX_WORKER",
        "import time\ntime.sleep(0.25)\n" + webhook_filters._REGEX_WORKER,
    )

    assert webhook_filters._bounded_regex_search(r"^safe$", "safe") is True


def test_regex_worker_match_delay_exceeds_post_ready_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_line = "pattern, value = json.load(sys.stdin)\n"
    assert input_line in webhook_filters._REGEX_WORKER
    monkeypatch.setattr(
        webhook_filters,
        "_REGEX_WORKER",
        webhook_filters._REGEX_WORKER.replace(
            input_line,
            input_line + "import time\ntime.sleep(0.25)\n",
        ),
    )
    started = time.monotonic()

    assert webhook_filters._bounded_regex_search(r"^safe$", "safe") is None
    assert time.monotonic() - started < 1.0


def test_regex_worker_startup_deadline_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        webhook_filters,
        "FILTER_REGEX_STARTUP_TIMEOUT_SECONDS",
        0.05,
    )
    monkeypatch.setattr(
        webhook_filters,
        "_REGEX_WORKER",
        "import time\ntime.sleep(0.25)\n" + webhook_filters._REGEX_WORKER,
    )
    started = time.monotonic()

    assert webhook_filters._bounded_regex_search(r"^safe$", "safe") is None
    assert time.monotonic() - started < 1.0


def test_regex_timeout_reaps_worker_before_working_directory_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_line = "pattern, value = json.load(sys.stdin)\n"
    monkeypatch.setattr(
        webhook_filters,
        "_REGEX_WORKER",
        webhook_filters._REGEX_WORKER.replace(
            input_line,
            input_line + "import time\ntime.sleep(0.25)\n",
        ),
    )
    real_popen = webhook_filters.subprocess.Popen
    real_temporary_directory = webhook_filters.tempfile.TemporaryDirectory
    spawned: dict[str, subprocess.Popen[bytes]] = {}
    cleanup_calls = 0

    def capture_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        spawned["process"] = process
        return process

    class ActiveCwdRejectingTemporaryDirectory:
        def __init__(self, **kwargs: object) -> None:
            self._inner = real_temporary_directory(**kwargs)
            self.name = self._inner.name

        def __enter__(self) -> str:
            return self.name

        def __exit__(self, *_args: object) -> None:
            self.cleanup()

        def cleanup(self) -> None:
            nonlocal cleanup_calls
            process = spawned["process"]
            if process.poll() is None:
                raise PermissionError("cannot remove a live process working directory")
            cleanup_calls += 1
            self._inner.cleanup()

    monkeypatch.setattr(webhook_filters.subprocess, "Popen", capture_popen)
    monkeypatch.setattr(
        webhook_filters.tempfile,
        "TemporaryDirectory",
        ActiveCwdRejectingTemporaryDirectory,
    )

    assert webhook_filters._bounded_regex_search(r"^safe$", "safe") is None
    assert cleanup_calls == 1
    assert spawned["process"].poll() is not None


def test_oversized_interpreter_is_rejected_before_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interpreter = tmp_path / "oversized-interpreter"
    with interpreter.open("wb") as handle:
        handle.truncate(webhook_filters.MAX_SCRIPT_INTERPRETER_SNAPSHOT_BYTES + 1)

    def unexpected_read(_fd: int, _size: int) -> bytes:
        raise AssertionError("oversized interpreter content was read")

    monkeypatch.setattr(webhook_filters.os, "read", unexpected_read)

    with pytest.raises(webhook_filters.BoundedFileSnapshotTooLarge):
        webhook_filters._open_stable_regular_file_digest(interpreter)


@pytest.mark.parametrize("kind", ["cycle", "deep"])
def test_regex_unserializable_filter_value_fails_closed(kind: str) -> None:
    value: list[object] = []
    if kind == "cycle":
        value.append(value)
    else:
        cursor = value
        for _ in range(10_000):
            nested: list[object] = []
            cursor.append(nested)
            cursor = nested

    assert not WebhookRouteProcessor().filter_matches(
        {"field": "value", "regex": "safe"},
        {"value": value},
        "push",
        {},
    )


@pytest.mark.parametrize(
    "value",
    [
        b"\xff",
        bytearray(b"\xff"),
        memoryview(b"\xff"),
        float("nan"),
        float("inf"),
        float("-inf"),
        ("tuple",),
        {"set"},
    ],
)
def test_regex_non_json_scalar_or_container_fails_closed(value: object) -> None:
    assert not WebhookRouteProcessor().filter_matches(
        {"field": "value", "regex": ".*"},
        {"value": value},
        "push",
        {},
    )


def test_regex_decimal_and_custom_object_fail_closed() -> None:
    from decimal import Decimal

    class MatchableObject:
        def __str__(self) -> str:
            return "matchable"

    processor = WebhookRouteProcessor()
    for value in (Decimal("1.25"), MatchableObject()):
        assert not processor.filter_matches(
            {"field": "value", "regex": ".*"},
            {"value": value},
            "push",
            {},
        )


@pytest.mark.parametrize(
    ("value", "pattern"),
    [
        (None, r"^None$"),
        (True, r"^True$"),
        (42, r"^42$"),
        (1.25, r"^1\.25$"),
        ("é", r"^é$"),
    ],
)
def test_regex_json_scalar_stringification_is_preserved(
    value: object,
    pattern: str,
) -> None:
    assert WebhookRouteProcessor().filter_matches(
        {"field": "value", "regex": pattern},
        {"value": value},
        "push",
        {},
    )


def test_redaction_failure_after_silent_script_is_indeterminate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processor, prepared, _script = _prepare(
        tmp_path,
        name="silent.py",
        source="print('[SILENT]')\n",
    )

    def fail_redaction(_text: str) -> str:
        raise RuntimeError("injected redaction failure")

    monkeypatch.setattr("agent.redact.redact_sensitive_text", fail_redaction)

    result = processor.run_prepared_script(prepared, {"original": True})

    assert result.disposition is WebhookScriptDisposition.INDETERMINATE
    assert "redaction failed" in str(result.error)


@pytest.mark.parametrize("invalid_result", [None, 0, b""])
def test_redaction_non_text_result_after_execution_is_indeterminate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_result: object,
) -> None:
    processor, prepared, _script = _prepare(
        tmp_path,
        name="redaction-contract.py",
        source="print('[SILENT]')\n",
    )
    monkeypatch.setattr(
        "agent.redact.redact_sensitive_text",
        lambda _text: invalid_result,
    )

    result = processor.run_prepared_script(prepared, {})

    assert result.disposition is WebhookScriptDisposition.INDETERMINATE
    assert "redaction failed" in str(result.error)


def test_compatibility_filter_file_oversized_integer_fails_closed(
    tmp_path: Path,
) -> None:
    values = tmp_path / "oversized-integer.json"
    values.write_text("9" * 5_000, encoding="utf-8")

    assert webhook_filters._load_filter_file_values(str(values)) is None


def test_embedded_nul_paths_fail_closed() -> None:
    prepared, error = WebhookRouteProcessor().prepare_route_script("bad\0path.py")

    assert prepared is None
    assert "invalid" in str(error)
    assert webhook_filters._load_filter_file_values("bad\0path") is None


def test_expanduser_runtime_errors_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_expanduser = Path.expanduser

    def fail_unknown_user(path: Path) -> Path:
        if str(path).startswith("~unknown_webhook_user"):
            raise RuntimeError("unknown user")
        return real_expanduser(path)

    monkeypatch.setattr(Path, "expanduser", fail_unknown_user)

    prepared, error = WebhookRouteProcessor().prepare_route_script(
        "~unknown_webhook_user/route.py"
    )
    assert prepared is None
    assert "invalid" in str(error)
    assert (
        webhook_filters._load_filter_file_values(
            "~unknown_webhook_user/filter-values.json"
        )
        is None
    )


@pytest.mark.skipif(os.name != "posix", reason="setsid regression requires POSIX")
@pytest.mark.live_system_guard_bypass
def test_escaped_descendant_retaining_pipes_leaves_no_pipe_workers(
    tmp_path: Path,
) -> None:
    child_pid_file = tmp_path / "escaped-pipe-child.pid"
    child_source = (
        "import os, time\n"
        "from pathlib import Path\n"
        f"Path({str(child_pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "time.sleep(60)\n"
    )
    processor, prepared, _script = _prepare(
        tmp_path,
        name="escaped-pipes.py",
        source=(
            "import subprocess, sys, time\n"
            "from pathlib import Path\n"
            f"marker = Path({str(child_pid_file)!r})\n"
            "subprocess.Popen(\n"
            f"    [sys.executable, '-I', '-S', '-c', {child_source!r}],\n"
            "    start_new_session=True,\n"
            ")\n"
            "deadline = time.monotonic() + 2\n"
            "while not marker.exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
            "print('{}')\n"
        ),
    )

    result = processor.run_prepared_script(prepared, {})
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    try:
        assert result.disposition is WebhookScriptDisposition.CONTINUE
        assert not any(
            thread.is_alive() and thread.name.startswith("webhook-script-")
            for thread in threading.enumerate()
        )
    finally:
        if _pid_is_effectively_alive(child_pid):
            os.kill(child_pid, signal.SIGKILL)


def test_delayed_output_accounting_preserves_output_limit_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processor, prepared, _script = _prepare(
        tmp_path,
        name="output-boundary.py",
        source=(
            "import sys\n"
            f"sys.stdout.buffer.write(b'x' * {MAX_SCRIPT_OUTPUT_STREAM_BYTES + 1})\n"
            "sys.stdout.flush()\n"
        ),
    )
    real_read_available = webhook_filters._read_available_script_pipe
    release = threading.Event()
    observed_stdout = 0
    gated = False

    def delayed_read(pipe, *, nonblocking: bool):
        nonlocal observed_stdout, gated
        chunk = real_read_available(pipe, nonblocking=nonblocking)
        if chunk:
            observed_stdout += len(chunk)
            if observed_stdout > MAX_SCRIPT_OUTPUT_STREAM_BYTES and not gated:
                gated = True
                assert release.wait(timeout=2)
        return chunk

    monkeypatch.setattr(
        webhook_filters,
        "_read_available_script_pipe",
        delayed_read,
    )
    release_timer = threading.Timer(0.75, release.set)
    release_timer.start()
    try:
        result = processor.run_prepared_script(prepared, {})
    finally:
        release.set()
        release_timer.cancel()

    assert gated
    assert result.disposition is WebhookScriptDisposition.INDETERMINATE
    assert "output exceeded" in str(result.error)
    assert not any(
        thread.is_alive() and thread.name.startswith("webhook-script-")
        for thread in threading.enumerate()
    )


def test_unsupported_nonblocking_pipes_use_compatible_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processor, prepared, _script = _prepare(
        tmp_path,
        name="blocking-pipe-fallback.py",
        source="print('{\"fallback\":true}')\n",
    )

    def unsupported_set_blocking(_fd: int, _blocking: bool) -> None:
        raise OSError("anonymous pipe nonblocking mode is unsupported")

    monkeypatch.setattr(webhook_filters.os, "set_blocking", unsupported_set_blocking)

    result = processor.run_prepared_script(prepared, {})

    assert result.disposition is WebhookScriptDisposition.CONTINUE
    assert result.payload == {"fallback": True}
    assert not any(
        thread.is_alive() and thread.name.startswith("webhook-script-")
        for thread in threading.enumerate()
    )


def test_unsupported_nonblocking_fallback_preserves_output_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processor, prepared, _script = _prepare(
        tmp_path,
        name="blocking-output-limit.py",
        source=(
            "import sys\n"
            f"sys.stdout.buffer.write(b'x' * {MAX_SCRIPT_OUTPUT_STREAM_BYTES + 1})\n"
        ),
    )
    monkeypatch.setattr(
        webhook_filters.os,
        "set_blocking",
        lambda *_args: (_ for _ in ()).throw(OSError("unsupported")),
    )

    result = processor.run_prepared_script(prepared, {})

    assert result.disposition is WebhookScriptDisposition.INDETERMINATE
    assert "output exceeded" in str(result.error)


def test_large_payload_uses_nonblocking_stdin_handoff(tmp_path: Path) -> None:
    processor, prepared, _script = _prepare(
        tmp_path,
        name="large-stdin.py",
        source=(
            "import json, sys\n"
            "content = sys.stdin.buffer.read()\n"
            "print(json.dumps({'bytes': len(content)}))\n"
        ),
    )
    payload = {"content": "x" * (256 * 1024)}

    result = processor.run_prepared_script(prepared, payload)

    assert result.disposition is WebhookScriptDisposition.CONTINUE
    assert result.payload == {"bytes": len(json.dumps(payload).encode())}


def test_pipe_value_error_is_typed_and_retires_all_workers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processor, prepared, _script = _prepare(
        tmp_path,
        name="pipe-close-race.py",
        source="import time\ntime.sleep(60)\n",
    )
    injected = threading.Event()
    real_read_available = webhook_filters._read_available_script_pipe

    def concurrent_close_read(pipe, *, nonblocking: bool):
        if not injected.is_set():
            injected.set()
            raise ValueError("I/O operation on closed file")
        return real_read_available(pipe, nonblocking=nonblocking)

    monkeypatch.setattr(
        webhook_filters,
        "_read_available_script_pipe",
        concurrent_close_read,
    )

    result = processor.run_prepared_script(prepared, {})

    assert injected.is_set()
    assert result.disposition is WebhookScriptDisposition.INDETERMINATE
    assert "pipe failed" in str(result.error)
    assert not any(
        thread.is_alive() and thread.name.startswith("webhook-script-")
        for thread in threading.enumerate()
    )


def test_reaped_leader_is_not_targeted_by_process_group_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReapedProcess:
        pid = 424242
        returncode: int | None = None

        def poll(self) -> int:
            self.returncode = 0
            return 0

        def wait(self, *, timeout: float) -> int:
            assert timeout == webhook_filters._SCRIPT_TERMINATE_GRACE_SECONDS
            assert self.returncode == 0
            return 0

    process = ReapedProcess()
    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        webhook_filters,
        "_can_observe_posix_exit_without_reaping",
        lambda: False,
    )
    monkeypatch.setattr(
        webhook_filters.os,
        "killpg",
        lambda pid, sig: killpg_calls.append((pid, sig)),
        raising=False,
    )

    assert webhook_filters._script_process_has_exited(process)
    webhook_filters._reap_finished_script_process_group(process)

    assert process.returncode == 0
    assert killpg_calls == []


@pytest.mark.skipif(os.name != "posix", reason="simulates non-POSIX branch")
def test_windows_final_interpreter_path_failure_is_typed_and_closes_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processor, prepared, _script = _prepare(
        tmp_path,
        name="windows-path-failure.py",
        source="print('{}')\n",
    )
    interpreter_fd = os.open(sys.executable, os.O_RDONLY)
    monkeypatch.setattr(webhook_filters.os, "name", "nt")
    monkeypatch.setattr(webhook_filters, "Path", lambda value: value)
    monkeypatch.setattr(
        webhook_filters,
        "_open_stable_regular_file_digest",
        lambda _path: (interpreter_fd, prepared.interpreter_sha256),
    )
    monkeypatch.setattr(
        webhook_filters,
        "_windows_final_path_from_fd",
        lambda _fd: (_ for _ in ()).throw(OSError("injected final-path failure")),
    )

    result = processor.run_prepared_script(prepared, {})

    assert result.disposition is WebhookScriptDisposition.FAILED
    assert "cannot be executed safely" in str(result.error)
    with pytest.raises(OSError):
        os.fstat(interpreter_fd)


def test_regex_startup_capture_failure_closes_provisional_fd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interpreter_fd = os.open(sys.executable, os.O_RDONLY)
    monkeypatch.setattr(
        webhook_filters,
        "_open_stable_regular_file_digest",
        lambda _path: (interpreter_fd, "a" * 64),
    )
    monkeypatch.setattr(
        webhook_filters,
        "_windows_final_path_from_fd",
        lambda _fd: (_ for _ in ()).throw(OSError("injected capture failure")),
    )

    with pytest.raises(OSError, match="injected capture failure"):
        webhook_filters._capture_regex_interpreter_authority(platform_name="nt")

    with pytest.raises(OSError):
        os.fstat(interpreter_fd)


@pytest.mark.windows_only
def test_windows_locked_file_and_interpreter_spawn_contract(
    tmp_path: Path,
) -> None:
    target = tmp_path / "locked.exe"
    target.write_bytes(b"original")
    replacement = tmp_path / "replacement.exe"
    replacement.write_bytes(b"replacement")
    descriptor = webhook_filters._open_windows_read_locked(target)
    try:
        with pytest.raises(OSError):
            os.replace(replacement, target)
    finally:
        os.close(descriptor)
    os.replace(replacement, target)
    assert target.read_bytes() == b"replacement"

    assert webhook_filters._bounded_regex_search(r"^\w+$", "é") is True
    processor, prepared, _script = _prepare(
        tmp_path,
        name="windows-spawn.py",
        source="print('{\"windows\":true}')\n",
    )
    result = processor.run_prepared_script(prepared, {})
    assert result.disposition is WebhookScriptDisposition.CONTINUE
    assert result.payload == {"windows": True}
