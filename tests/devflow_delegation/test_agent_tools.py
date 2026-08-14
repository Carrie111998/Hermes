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


def test_list_files_is_confined_to_the_worktree(worktree):
    listed = list_files(worktree, "**/*.py")
    assert "src/app.py" in listed
    assert all(not item.startswith("..") for item in listed)


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
