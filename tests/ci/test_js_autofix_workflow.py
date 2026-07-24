from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "js-autofix.yml"


def test_app_token_authenticates_the_autofix_push():
    """The privileged push must not use checkout's persisted GITHUB_TOKEN."""
    text = WORKFLOW.read_text(encoding="utf-8")
    apply_job = text.split("  apply-patch:", 1)[1]
    push_step = apply_job.split(
        "      - name: Apply patch and push to bot branch", 1
    )[1].split("      - name: Create/update PR and enable auto-merge", 1)[0]

    assert "persist-credentials: false" in apply_job
    assert "GH_TOKEN: ${{ steps.app-token.outputs.token }}" in push_step
    assert "gh auth setup-git" in push_step
    assert "git push --force origin" in push_step
    assert "x-access-token:${" not in push_step
