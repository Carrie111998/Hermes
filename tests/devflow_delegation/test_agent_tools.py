from pathlib import Path

import pytest

from devflow_delegation.agent_tools import ToolError, list_files, read_file, write_file
from devflow_delegation.allowlist import TargetConfig


def _target(**over):
    values = dict(
        repo="fixture", checkout_path="/unused",
        allowed_globs=("src/**",), denied_globs=("**/.env", "secrets/**"),
    )
    values.update(over)
    return TargetConfig(**values)


@pytest.fixture
def worktree(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "token.txt").write_text("supersecret\n", encoding="utf-8")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-live\n", encoding="utf-8")
    return tmp_path


def test_read_file_returns_contents_inside_the_worktree(worktree):
    assert read_file(worktree, _target(), "src/app.py") == "print('hi')\n"


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "src/../../escape.txt"])
def test_read_file_refuses_paths_outside_the_worktree(worktree, path):
    with pytest.raises(ToolError):
        read_file(worktree, _target(), path)


@pytest.mark.parametrize("path", ["secrets/token.txt", ".env"])
def test_read_file_refuses_denied_paths(worktree, path):
    # Denied globs are secret-bearing by definition; the agent must never see them.
    with pytest.raises(ToolError):
        read_file(worktree, _target(), path)


def test_read_file_reports_a_missing_file_as_a_tool_error(worktree):
    with pytest.raises(ToolError):
        read_file(worktree, _target(), "src/nope.py")


def test_list_files_is_confined_to_the_worktree(tmp_path_factory):
    # A pattern that CAN climb out (unlike "**/*.py", which pathlib.Path.glob
    # never yields anything outside root for) must still return nothing from
    # outside the worktree. That containment comes entirely from the
    # ``except ValueError: continue`` guard in list_files -- delete that guard
    # and this call raises ValueError instead of returning [], failing the test.
    root = tmp_path_factory.mktemp("list_files_confinement")
    worktree_dir = root / "worktree"
    worktree_dir.mkdir()
    (worktree_dir / "inside.py").write_text("inside\n", encoding="utf-8")
    (root / "outside.py").write_text("outside\n", encoding="utf-8")

    assert list_files(worktree_dir, "../*.py") == []

    # Normal in-scope globbing still returns the expected file.
    assert list_files(worktree_dir, "**/*.py") == ["inside.py"]


def test_write_file_creates_a_file_inside_allowed_globs(worktree):
    write_file(worktree, _target(), "src/new.py", "x = 1\n")
    assert (worktree / "src" / "new.py").read_text(encoding="utf-8") == "x = 1\n"


def test_write_file_refuses_a_path_outside_allowed_globs(worktree):
    # The single most important bound: the agent may only write where the
    # operator scoped it, regardless of what the model asks for.
    with pytest.raises(ToolError):
        write_file(worktree, _target(), "tools/evil.py", "x = 1\n")
    assert not (worktree / "tools" / "evil.py").exists()


def test_write_file_refuses_a_denied_path_even_inside_an_allowed_glob(worktree):
    with pytest.raises(ToolError):
        write_file(worktree, _target(allowed_globs=("**/*",)), "secrets/token.txt", "x\n")
    assert (worktree / "secrets" / "token.txt").read_text(encoding="utf-8") == "supersecret\n"


def test_write_file_refuses_traversal(worktree):
    with pytest.raises(ToolError):
        write_file(worktree, _target(), "../escape.py", "x = 1\n")
    assert not (worktree.parent / "escape.py").exists()


def test_write_file_refuses_an_existing_directory_as_destination(worktree):
    # allowed_globs=("src/**",) lets the model name any subdirectory under
    # src/ as a "file" to write. If destination is an existing directory,
    # write_text() raises an unhandled IsADirectoryError/PermissionError --
    # a crash, not a ToolError the model can recover from.
    (worktree / "src" / "adir").mkdir()
    with pytest.raises(ToolError):
        write_file(worktree, _target(), "src/adir", "x = 1\n")
    assert (worktree / "src" / "adir").is_dir()
    assert list((worktree / "src" / "adir").iterdir()) == []


def test_write_file_refuses_when_an_intermediate_component_is_a_file(worktree):
    # mkdir(parents=True) raises NotADirectoryError/FileExistsError when a
    # path component that should be a directory is already a regular file.
    (worktree / "src" / "blocker.py").write_text("x\n", encoding="utf-8")
    with pytest.raises(ToolError):
        write_file(worktree, _target(), "src/blocker.py/inner.py", "x = 1\n")
    assert (worktree / "src" / "blocker.py").read_text(encoding="utf-8") == "x\n"


def _make_symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this machine/filesystem")


def test_read_file_refuses_a_symlink_that_escapes_the_worktree(worktree, tmp_path_factory):
    outside_dir = tmp_path_factory.mktemp("read_symlink_escape_target")
    secret = outside_dir / "secret.txt"
    secret.write_text("top-secret\n", encoding="utf-8")
    link = worktree / "src" / "escape_read.txt"
    _make_symlink_or_skip(link, secret)

    with pytest.raises(ToolError):
        read_file(worktree, _target(), "src/escape_read.txt")
    assert secret.read_text(encoding="utf-8") == "top-secret\n"


def test_write_file_refuses_a_symlink_that_escapes_the_worktree(worktree, tmp_path_factory):
    outside_dir = tmp_path_factory.mktemp("write_symlink_escape_target")
    victim = outside_dir / "victim.txt"
    victim.write_text("original\n", encoding="utf-8")
    link = worktree / "src" / "escape_write.txt"
    _make_symlink_or_skip(link, victim)

    with pytest.raises(ToolError):
        write_file(worktree, _target(), "src/escape_write.txt", "pwned\n")
    assert victim.read_text(encoding="utf-8") == "original\n"


from devflow_delegation.agent_tools import TOOL_SCHEMAS, run_tests


def test_run_tests_executes_the_targets_configured_commands(worktree):
    target = _target(test_commands=(("python", "-c", "print('tests passed')"),))
    assert "tests passed" in run_tests(worktree, target)


def test_run_tests_reports_failure_without_raising(worktree):
    # A failing suite is information the agent must act on, not a crash.
    target = _target(test_commands=(("python", "-c", "import sys; print('boom'); sys.exit(1)"),))
    result = run_tests(worktree, target)
    assert "FAILED" in result and "boom" in result


def test_run_tests_refuses_a_target_with_no_test_commands(worktree):
    with pytest.raises(ToolError):
        run_tests(worktree, _target(test_commands=()))


def test_run_tests_rejects_shell_string_commands(worktree):
    # Stage-2 validator semantics: string commands are invalid, never shelled out.
    with pytest.raises(ToolError):
        run_tests(worktree, _target(test_commands=("python -c \"print(1)\"",)))


def test_tool_schemas_cover_exactly_the_four_bounded_tools():
    names = {schema["function"]["name"] for schema in TOOL_SCHEMAS}
    assert names == {"read_file", "list_files", "write_file", "run_tests"}


# --- F3: list_files had no output cap, unlike read_file (100k) and run_tests
# (20k). On a large repo "**/*" returns thousands of paths, appended as a tool
# message and re-sent every iteration -- unbounded prompt growth with no live
# token bound (compounded by the fact that a provider omitting `usage` also
# used to leave the token ceiling permanently inert -- see test_agent_runner).


def test_list_files_caps_the_number_of_entries_returned(tmp_path):
    from devflow_delegation.agent_tools import MAX_LIST_FILES_ENTRIES

    many = MAX_LIST_FILES_ENTRIES + 50
    for index in range(many):
        (tmp_path / f"f{index:05d}.txt").write_text("x", encoding="utf-8")

    result = list_files(tmp_path, "*.txt")

    # One extra entry for the truncation marker; the count must not just grow
    # with the number of files on disk.
    assert len(result) == MAX_LIST_FILES_ENTRIES + 1
    assert "truncated" in result[-1]
    assert str(many) in result[-1]


def test_list_files_does_not_truncate_when_under_the_cap(worktree):
    result = list_files(worktree, "**/*.py")
    assert result == ["src/app.py"]
    assert not any("truncated" in entry for entry in result)


# --- F6: run_tests truncated with output[:MAX_TEST_OUTPUT_CHARS], which keeps
# only the HEAD. pytest puts the failure summary at the END, so a large
# failing suite handed the agent collection noise and nothing about what
# broke. Truncation must keep both ends.


def test_run_tests_truncation_preserves_the_tail_not_just_the_head(worktree):
    from devflow_delegation.agent_tools import MAX_TEST_OUTPUT_CHARS

    # Emit enough head noise to blow past the cap, then a distinctive marker
    # right at the end -- the exact spot pytest would put "FAILED test_x".
    script = (
        "import sys\n"
        f"sys.stdout.write('n' * {MAX_TEST_OUTPUT_CHARS * 2})\n"
        "print('FAILED test_the_actual_break - AssertionError: boom')\n"
    )
    target = _target(test_commands=(("python", "-c", script),))

    result = run_tests(worktree, target)

    assert len(result) <= MAX_TEST_OUTPUT_CHARS + 200  # head + marker + tail, bounded
    assert "FAILED test_the_actual_break - AssertionError: boom" in result
    assert "truncated" in result
    # The head is still present too -- this isn't a tail-only truncation.
    assert "n" * 100 in result
