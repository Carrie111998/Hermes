from __future__ import annotations

from pathlib import Path


WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
SUPPLY_CHAIN_WORKFLOW = WORKFLOWS / "supply-chain-audit.yml"
REVIEW_LABELS_WORKFLOW = WORKFLOWS / "review-labels.yml"


def _workflow_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_install_hook_review_is_routed_through_the_consolidated_label_gate():
    supply_chain = _workflow_text(SUPPLY_CHAIN_WORKFLOW)
    review_labels = _workflow_text(REVIEW_LABELS_WORKFLOW)

    assert "SETUP_HITS=$(git diff --diff-filter=d --name-only" in supply_chain
    assert "CI_REVIEWED" in supply_chain
    assert "Install-hook file added or modified" in supply_chain

    assert "inputs.supply_chain" in review_labels
    assert "grep -Fxq 'ci-reviewed'" in review_labels


def test_consolidated_label_does_not_bypass_other_critical_patterns():
    text = _workflow_text(SUPPLY_CHAIN_WORKFLOW)

    setup_start = text.index("SETUP_HITS=$(git diff --diff-filter=d --name-only")
    pre_setup = text[:setup_start]

    # The maintainer-review label must not appear in the .pth, base64+exec/eval,
    # or obfuscated subprocess checks. Those remain unconditional critical
    # findings when they match.
    assert 'if [ "$CI_REVIEWED" != "true" ]' not in pre_setup
    assert "PTH_FILES=$(git diff --diff-filter=d --name-only" in pre_setup
    assert "B64_EXEC_HITS=$(echo \"$DIFF\"" in pre_setup
    assert "PROC_HITS=$(echo \"$DIFF\"" in pre_setup


def test_mcp_catalog_review_uses_the_consolidated_label_gate():
    text = _workflow_text(REVIEW_LABELS_WORKFLOW)

    assert "inputs.mcp_catalog" in text
    assert "grep -Fxq 'ci-reviewed'" in text
    assert "MCP_CATALOG" in text
