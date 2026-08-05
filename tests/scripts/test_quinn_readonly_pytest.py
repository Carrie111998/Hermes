"""RED regressions for Quinn's exact-tree, read-only pytest runner."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts" / "quinn_readonly_pytest.py"

CONTENT_FREE_RESULT_KEYS = {
    "schema_version",
    "status",
    "error_class",
    "failed_stage",
    "exit_code",
    "return_code",
    "pytest_exit_code",
    "capability_exit_code",
    "duration_ms",
    "test_counts",
    "tree",
    "reviewed_head",
    "reviewed_head_tree",
    "repo_dirty_at_start",
    "materialization",
    "source_unchanged",
    "source_read_only",
    "source_read_only_before",
    "source_read_only_after",
    "sandbox_backend",
    "sandbox_probe",
    "sandbox_policy",
    "interpreter",
    "interpreter_resolved",
    "hermes_python",
    "interpreter_provenance",
    "pytest_arg_count",
    "test_node_args",
}


def _load_runner_module():
    spec = importlib.util.spec_from_file_location("quinn_readonly_pytest", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def _selected_pytest_python() -> str:
    """Preserve the reviewer-selected venv across nested runner regressions."""
    return os.environ.get("HERMES_PYTHON", "").strip() or sys.executable


def _tiny_repo(tmp_path: Path, default_home: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "review-source"
    repo.mkdir()
    _git(repo, "init", "-q")
    test_dir = repo / "tests"
    test_dir.mkdir()
    test_source = """import errno
import json
import os
from pathlib import Path
import socket
import subprocess
import sys


DENIED = {errno.EPERM, errno.EACCES}
HOST_DEFAULT_HOME = Path(__HOST_DEFAULT_HOME__)


def _operation_errno(callback):
    try:
        callback()
    except OSError as exc:
        return exc.errno
    return 0


def test_quinn_environment_is_isolated_and_source_read_only():
    source = Path(__file__).resolve()
    assert source.stat().st_mode & 0o222 == 0
    assert source.parent.stat().st_mode & 0o222 == 0
    assert \"quinn-pytest-\" in os.environ[\"HOME\"]
    assert \"quinn-pytest-\" in os.environ[\"HERMES_HOME\"]
    assert Path.home() != HOST_DEFAULT_HOME
    assert os.environ[\"PYTHONDONTWRITEBYTECODE\"] == \"1\"
    assert os.environ[\"PYTHONHASHSEED\"] == \"0\"
    assert os.environ[\"TZ\"] == \"UTC\"


def test_kernel_sandbox_denies_tcp_udp_and_descendant_network():
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        assert tcp.connect_ex((\"127.0.0.1\", 9)) in DENIED
    finally:
        tcp.close()

    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        assert _operation_errno(
            lambda: udp.sendto(b\"quinn\", (\"127.0.0.1\", 9))
        ) in DENIED
    finally:
        udp.close()

    child_code = (
        \"import json,socket;\"
        \"s=socket.socket(socket.AF_INET,socket.SOCK_STREAM);\"
        \"r=s.connect_ex(('127.0.0.1',9));s.close();\"
        \"print(json.dumps({'tcp_errno':r}))\"
    )
    child = subprocess.run(
        [sys.executable, \"-I\", \"-c\", child_code],
        check=False,
        capture_output=True,
        text=True,
    )
    assert child.returncode == 0, child.stderr
    assert int(json.loads(child.stdout)[\"tcp_errno\"]) in DENIED


def test_reviewed_source_denies_write_chmod_rename_and_symlink_escapes(tmp_path):
    source = Path(__file__).resolve()
    original = source.read_bytes()
    assert _operation_errno(lambda: source.write_bytes(b\"mutated\")) in DENIED
    assert _operation_errno(lambda: source.chmod(0o644)) in DENIED
    assert _operation_errno(lambda: source.parent.chmod(0o755)) in DENIED
    assert _operation_errno(
        lambda: source.rename(tmp_path / \"renamed-source.py\")
    ) in DENIED

    writable_link = tmp_path / \"reviewed-source-link\"
    writable_link.symlink_to(source)
    assert writable_link.is_symlink()
    assert _operation_errno(lambda: writable_link.write_bytes(b\"via-link\")) in DENIED
    assert _operation_errno(
        lambda: (source.parent / \"source-created-link\").symlink_to(tmp_path)
    ) in DENIED
    assert source.read_bytes() == original


def test_default_home_is_untouched_and_not_an_escape_target():
    escape = HOST_DEFAULT_HOME / \"sandbox-escape\"
    assert _operation_errno(lambda: escape.write_text(\"forbidden\")) in DENIED
    assert not escape.exists()


def test_isolated_pytest_home_and_hermes_outputs_remain_writable(tmp_path):
    outputs = [
        tmp_path / \"pytest-output\",
        Path.home() / \"home-output\",
        Path(os.environ[\"HERMES_HOME\"]) / \"hermes-output\",
        Path(os.environ[\"TMPDIR\"]) / \"tmp-output\",
    ]
    for output in outputs:
        output.write_text(\"allowed\", encoding=\"utf-8\")
        assert output.read_text(encoding=\"utf-8\") == \"allowed\"
""".replace("__HOST_DEFAULT_HOME__", json.dumps(str(default_home.resolve())))
    (test_dir / "test_environment.py").write_text(
        test_source,
        encoding="utf-8",
    )
    _git(repo, "add", "tests/test_environment.py")
    _git(
        repo,
        "-c",
        "user.name=Quinn Test",
        "-c",
        "user.email=quinn-test@example.invalid",
        "commit",
        "-q",
        "-m",
        "fixture",
    )
    return (
        repo,
        _git(repo, "rev-parse", "HEAD"),
        _git(repo, "rev-parse", "HEAD^{tree}"),
    )


def _python_wrapper(path: Path, *, reject_pytest_import: bool = False) -> Path:
    rejection = (
        'case "$*" in *"import pytest,pytest_asyncio"*) exit 86;; esac\n'
        if reject_pytest_import
        else ""
    )
    path.write_text(
        "#!/bin/sh\n"
        + rejection
        + f"exec {shlex.quote(_selected_pytest_python())} \"$@\"\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_runner_requires_existing_hermes_python(tmp_path):
    env = os.environ.copy()
    env.pop("HERMES_PYTHON", None)
    result = subprocess.run(
        [sys.executable, str(RUNNER), "--repo", str(tmp_path), "--tree", "HEAD", "--", "-q"],
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2
    assert "HERMES_PYTHON" in result.stderr


def test_runner_tests_exact_tree_in_read_only_isolated_environment(tmp_path):
    default_home = tmp_path / "default-home"
    default_home.mkdir()
    marker = default_home / "marker"
    marker.write_text("unchanged", encoding="utf-8")
    repo, commit_hash, tree_hash = _tiny_repo(tmp_path, default_home)
    # Dirty the mutable checkout after the commit. A runner that tests cwd bytes
    # instead of the requested tree would now fail.
    (repo / "tests" / "test_environment.py").write_text(
        "def test_dirty_checkout_must_not_run():\n    assert False\n",
        encoding="utf-8",
    )
    output = tmp_path / "quinn-pytest-result.json"
    env = os.environ.copy()
    wrapper = _python_wrapper(tmp_path / "hermes-python-wrapper")
    env["HERMES_PYTHON"] = str(wrapper)
    env["HOME"] = str(default_home)

    result = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--repo",
            str(repo),
            "--tree",
            tree_hash,
            "--output",
            str(output),
            "--",
            "tests/test_environment.py",
            "-q",
        ],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert set(payload) <= CONTENT_FREE_RESULT_KEYS
    assert payload["schema_version"] == 3
    assert payload["tree"] == tree_hash
    assert payload["reviewed_head"] == commit_hash
    assert payload["reviewed_head_tree"] == tree_hash
    assert payload["repo_dirty_at_start"] is True
    assert payload["return_code"] == 0
    assert payload["status"] == "passed"
    assert payload["source_read_only"] is True
    assert payload["source_unchanged"] is True
    assert payload["sandbox_backend"] == "macos_sandbox_exec"
    assert payload["sandbox_probe"]["enforced"] is True
    assert payload["sandbox_probe"]["protected_unchanged"] is True
    assert payload["sandbox_probe"]["source_unchanged"] is True
    assert payload["sandbox_probe"]["source_write_errno"] in {1, 13}
    assert payload["sandbox_probe"]["source_chmod_errno"] in {1, 13}
    assert payload["sandbox_probe"]["source_parent_chmod_errno"] in {1, 13}
    assert payload["sandbox_probe"]["source_rename_errno"] in {1, 13}
    assert payload["sandbox_probe"]["writable_symlink_created"] is True
    assert payload["sandbox_probe"]["symlink_escape_write_errno"] in {1, 13}
    assert payload["sandbox_probe"]["source_symlink_create_errno"] in {1, 13}
    assert payload["sandbox_probe"]["default_home_write_errno"] in {1, 13}
    assert payload["sandbox_probe"]["default_home_untouched"] is True
    assert payload["sandbox_probe"]["scratch_write_allowed"] is True
    assert payload["sandbox_policy"]["descendant_inherited"] is True
    assert payload["sandbox_policy"]["network_denied"] is True
    assert payload["interpreter"] == str(wrapper)
    assert payload["interpreter_resolved"] == str(wrapper.resolve())
    assert payload["hermes_python"] == str(wrapper.resolve())
    assert "quinn_network_guard" not in json.dumps(payload)
    assert payload["test_node_args"] == ["tests/test_environment.py"]
    assert payload["test_counts"]["passed"] == 5
    assert payload["materialization"]["method"] == "git_ls_tree_cat_file_batch"
    assert payload["materialization"]["verified_before_pytest"] is True
    assert payload["materialization"]["verified_after_pytest"] is True
    assert re.fullmatch(
        r"[0-9a-f]{64}", payload["materialization"]["manifest_digest_sha256"]
    )
    forbidden = {
        "pytest_output",
        "stdout_path",
        "stderr_path",
        "pytest_command",
        "sandbox_command",
        "archive_source_digest",
        "source_digest_before",
        "source_digest_after",
    }
    assert forbidden.isdisjoint(payload)
    assert "sandbox=macos_sandbox_exec" in result.stderr
    assert marker.read_text(encoding="utf-8") == "unchanged"
    assert sorted(path.name for path in default_home.iterdir()) == ["marker"]
    # Result artifact itself is caller-owned and remains writable.
    assert output.stat().st_mode & stat.S_IWUSR
    assert not output.with_suffix(output.suffix + ".stdout.txt").exists()
    assert not output.with_suffix(output.suffix + ".stderr.txt").exists()


def test_runner_fails_clearly_when_selected_interpreter_cannot_import_pytest(tmp_path):
    default_home = tmp_path / "default-home"
    default_home.mkdir()
    repo, _, tree_hash = _tiny_repo(tmp_path, default_home)
    output = tmp_path / "missing-pytest.json"
    wrapper = _python_wrapper(
        tmp_path / "python-without-pytest",
        reject_pytest_import=True,
    )
    env = os.environ.copy()
    env["HERMES_PYTHON"] = str(wrapper)

    result = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--repo",
            str(repo),
            "--tree",
            tree_hash,
            "--output",
            str(output),
            "--",
            "-q",
        ],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 2
    assert "cannot import pytest and pytest_asyncio" in result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert set(payload) <= CONTENT_FREE_RESULT_KEYS
    assert payload["schema_version"] == 3
    assert payload["status"] == "interpreter_unavailable"
    assert payload["error_class"] == "pytest_import_failed"
    assert payload["sandbox_probe"]["enforced"] is True
    assert not output.with_suffix(output.suffix + ".stdout.txt").exists()
    assert not output.with_suffix(output.suffix + ".stderr.txt").exists()


def test_runner_rejects_git_object_symlink_escape_before_materialization(tmp_path):
    default_home = tmp_path / "default-home"
    default_home.mkdir()
    host_target = default_home / "outside.txt"
    host_target.write_text("unchanged", encoding="utf-8")
    repo, _commit, _tree = _tiny_repo(tmp_path, default_home)
    (repo / "escape").symlink_to(host_target)
    _git(repo, "add", "escape")
    _git(
        repo,
        "-c",
        "user.name=Quinn Runner Test",
        "-c",
        "user.email=quinn-runner@example.invalid",
        "commit",
        "-m",
        "add unsafe symlink",
    )
    output = tmp_path / "unsafe-archive.json"
    env = os.environ.copy()
    env["HERMES_PYTHON"] = _selected_pytest_python()

    completed = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--repo",
            str(repo),
            "--tree",
            "HEAD",
            "--output",
            str(output),
            "--",
            "-q",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 3
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "infrastructure_error"
    assert payload["failed_stage"] == "materialize_git_tree"
    assert host_target.read_text(encoding="utf-8") == "unchanged"


def test_failing_pytest_output_is_never_persisted_or_relayed(tmp_path):
    default_home = tmp_path / "default-home"
    default_home.mkdir()
    repo, _commit, _tree = _tiny_repo(tmp_path, default_home)
    sentinel = f"runtime-secret-{uuid.uuid4().hex}"
    failing_test = repo / "tests" / "test_runtime_failure.py"
    failing_test.write_text(
        "def test_runtime_failure():\n"
        f"    print({sentinel!r})\n"
        f"    raise RuntimeError({sentinel!r})\n",
        encoding="utf-8",
    )
    _git(repo, "add", "tests/test_runtime_failure.py")
    _git(
        repo,
        "-c",
        "user.name=Quinn Runner Test",
        "-c",
        "user.email=quinn-runner@example.invalid",
        "commit",
        "-q",
        "-m",
        "runtime failure fixture",
    )
    tree_hash = _git(repo, "rev-parse", "HEAD^{tree}")
    output_dir = tmp_path / "runner-output"
    output_dir.mkdir()
    output = output_dir / "result.json"
    env = os.environ.copy()
    env["HERMES_PYTHON"] = _selected_pytest_python()

    completed = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--repo",
            str(repo),
            "--tree",
            tree_hash,
            "--output",
            str(output),
            "--",
            "tests/test_runtime_failure.py",
            "-q",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert set(payload) <= CONTENT_FREE_RESULT_KEYS
    assert payload["status"] == "failed"
    assert payload["pytest_exit_code"] == 1
    assert payload["test_counts"]["failed"] == 1
    assert payload["test_node_args"] == ["tests/test_runtime_failure.py"]
    assert sentinel not in json.dumps(payload, sort_keys=True)
    assert sentinel not in completed.stdout
    assert sentinel not in completed.stderr
    assert "RuntimeError" not in completed.stdout + completed.stderr
    assert len(completed.stdout) + len(completed.stderr) < 4096
    files = sorted(path for path in output_dir.rglob("*") if path.is_file())
    assert files == [output]
    for path in files:
        assert sentinel.encode() not in path.read_bytes()
    forbidden = {
        "pytest_output",
        "stdout_path",
        "stderr_path",
        "traceback",
        "exception_text",
        "output_hash",
    }
    assert forbidden.isdisjoint(payload)


def test_git_attributes_executable_and_safe_symlink_materialize_exact_objects(tmp_path):
    default_home = tmp_path / "default-home"
    default_home.mkdir()
    repo, _commit, _tree = _tiny_repo(tmp_path, default_home)
    (repo / ".gitattributes").write_text(
        "ignored.txt export-ignore\nsubstituted.txt export-subst\n",
        encoding="utf-8",
    )
    (repo / "ignored.txt").write_bytes(b"ignored-object-bytes\n")
    (repo / "substituted.txt").write_bytes(b"$Format:%H$\n")
    executable = repo / "exact-mode.sh"
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    (repo / "ordinary-link").symlink_to("ignored.txt")
    exact_test = repo / "tests" / "test_git_object_projection.py"
    exact_test.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "import stat\n\n"
        "def test_exact_git_object_projection():\n"
        "    root = Path(__file__).resolve().parents[1]\n"
        "    assert (root / 'ignored.txt').read_bytes() == b'ignored-object-bytes\\n'\n"
        "    assert (root / 'substituted.txt').read_bytes() == b'$Format:%H$\\n'\n"
        "    assert stat.S_IMODE((root / 'exact-mode.sh').stat().st_mode) == 0o555\n"
        "    assert (root / 'ordinary-link').is_symlink()\n"
        "    assert os.readlink(root / 'ordinary-link') == 'ignored.txt'\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".gitattributes", "ignored.txt", "substituted.txt")
    _git(repo, "add", "exact-mode.sh", "ordinary-link", "tests/test_git_object_projection.py")
    _git(
        repo,
        "-c",
        "user.name=Quinn Runner Test",
        "-c",
        "user.email=quinn-runner@example.invalid",
        "commit",
        "-q",
        "-m",
        "attribute and mode fixtures",
    )
    tree_hash = _git(repo, "rev-parse", "HEAD^{tree}")
    output = tmp_path / "attributes-result.json"
    env = os.environ.copy()
    env["HERMES_PYTHON"] = _selected_pytest_python()

    completed = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--repo",
            str(repo),
            "--tree",
            tree_hash,
            "--output",
            str(output),
            "--",
            "tests/test_git_object_projection.py",
            "-q",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "passed"
    assert payload["test_counts"]["passed"] == 1
    provenance = payload["materialization"]
    assert provenance["method"] == "git_ls_tree_cat_file_batch"
    assert provenance["executable_blob_count"] >= 1
    assert provenance["symlink_count"] == 1
    assert provenance["verified_before_pytest"] is True
    assert provenance["verified_after_pytest"] is True
    assert "archive" not in json.dumps(provenance, sort_keys=True).lower()


def test_runner_rejects_gitlink_before_pytest(tmp_path):
    default_home = tmp_path / "default-home"
    default_home.mkdir()
    repo, commit_hash, _tree = _tiny_repo(tmp_path, default_home)
    _git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{commit_hash},vendor/submodule",
    )
    _git(
        repo,
        "-c",
        "user.name=Quinn Runner Test",
        "-c",
        "user.email=quinn-runner@example.invalid",
        "commit",
        "-q",
        "-m",
        "unsupported gitlink",
    )
    output = tmp_path / "gitlink-result.json"
    env = os.environ.copy()
    env["HERMES_PYTHON"] = _selected_pytest_python()

    completed = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--repo",
            str(repo),
            "--tree",
            "HEAD",
            "--output",
            str(output),
            "--",
            "tests/test_environment.py",
            "-q",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 3
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "infrastructure_error"
    assert payload["failed_stage"] == "materialize_git_tree"
    assert payload["error_class"] == "unsupported_git_entry"
    assert payload["pytest_exit_code"] is None


@pytest.mark.parametrize(
    "listing",
    [
        (
            b"100644 blob " + (b"1" * 40) + b"\tduplicate\0"
            b"100644 blob " + (b"2" * 40) + b"\tduplicate\0"
        ),
        (
            b"120000 blob " + (b"1" * 40) + b"\tconflict\0"
            b"100644 blob " + (b"2" * 40) + b"\tconflict/child\0"
        ),
        b"100644 blob " + (b"1" * 40) + b"\t../escape\0",
    ],
)
def test_git_manifest_rejects_duplicate_conflict_and_traversal(listing):
    runner = _load_runner_module()
    with pytest.raises(RuntimeError):
        runner._parse_ls_tree_manifest(listing)


def test_exact_materialization_verifier_rejects_extra_mode_and_byte_mismatch(tmp_path):
    runner = _load_runner_module()
    repo = tmp_path / "manifest-source"
    repo.mkdir()
    _git(repo, "init", "-q")
    regular = repo / "regular.txt"
    regular.write_bytes(b"exact regular bytes\n")
    executable = repo / "executable.sh"
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    (repo / "link").symlink_to("regular.txt")
    _git(repo, "add", "regular.txt", "executable.sh", "link")
    _git(
        repo,
        "-c",
        "user.name=Quinn Runner Test",
        "-c",
        "user.email=quinn-runner@example.invalid",
        "commit",
        "-q",
        "-m",
        "exact manifest",
    )
    tree_hash = _git(repo, "rev-parse", "HEAD^{tree}")
    manifest = runner._read_git_manifest(repo, tree_hash)
    destination = tmp_path / "materialized"
    provenance = runner._materialize_git_tree(repo, manifest, destination)

    assert provenance["verified_before_pytest"] is True
    assert stat.S_IMODE((destination / "regular.txt").stat().st_mode) == 0o644
    assert stat.S_IMODE((destination / "executable.sh").stat().st_mode) == 0o755
    assert (destination / "link").is_symlink()
    assert runner._verify_materialized_tree(
        repo, manifest, destination, read_only=False
    )

    extra = destination / "extra"
    extra.write_bytes(b"extra")
    with pytest.raises(RuntimeError):
        runner._verify_materialized_tree(repo, manifest, destination, read_only=False)
    extra.unlink()

    (destination / "regular.txt").chmod(0o755)
    with pytest.raises(RuntimeError):
        runner._verify_materialized_tree(repo, manifest, destination, read_only=False)
    (destination / "regular.txt").chmod(0o644)

    (destination / "regular.txt").write_bytes(b"mismatch")
    with pytest.raises(RuntimeError):
        runner._verify_materialized_tree(repo, manifest, destination, read_only=False)


def test_git_object_read_failure_is_rejected_before_test_execution(tmp_path):
    runner = _load_runner_module()
    repo = tmp_path / "missing-object-source"
    repo.mkdir()
    _git(repo, "init", "-q")
    manifest = runner._parse_ls_tree_manifest(
        b"100644 blob " + (b"f" * 40) + b"\tmissing.txt\0"
    )
    with pytest.raises(RuntimeError):
        runner._materialize_git_tree(repo, manifest, tmp_path / "missing-materialized")
