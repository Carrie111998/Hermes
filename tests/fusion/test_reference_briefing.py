import subprocess
from pathlib import Path

from agent.fusion.briefing import build_reference_brief, classify_fusion_task
from agent.fusion.context import build_context_packet
from agent.fusion.models import FusionRequest


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )


def _init_repo(repo: Path) -> Path:
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "fusion@example.test")
    _git(repo, "config", "user.name", "Fusion Test")
    (repo / "README.md").write_text("# Demo\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


def test_classify_bug_request_requires_locate():
    route = classify_fusion_task("why is login broken with traceback?")
    assert route["task_kind"] == "bug_unknown_root"
    assert route["locate_required"] is True
    assert any("broken" in item or "traceback" in item for item in route["rationale"])


def test_classify_design_request_uses_wide_solution_path():
    route = classify_fusion_task("plan implementation of the new auth flow")
    assert route["task_kind"] == "design_wide_solution"
    assert route["locate_required"] is False


def test_build_reference_brief_includes_git_tree_docs_and_layers(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    context = build_context_packet(str(repo))
    request = FusionRequest(mode="plan", task="plan implementation", repo_path=str(repo))
    brief = build_reference_brief(request, context, routing=classify_fusion_task(request.task))

    assert brief["schema"] == "fusion-reference-brief/v1"
    assert brief["git_head"] != "unknown"
    assert "README.md" in brief["repo_tree"]
    assert any(doc["path"] == "README.md" for doc in brief["docs"])
    assert "tests" in brief["layers"]["covered"]
    assert "# Fusion Evidence Brief" in brief["markdown"]
    assert "Layers covered / Layers NOT covered" in brief["markdown"]
