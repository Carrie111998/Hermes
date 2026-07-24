"""Tests for scripts/ci/emit_review_status.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "emit_review_status.py"
_SUPPLY_CHAIN_WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "supply-chain-audit.yml"
)
_spec = importlib.util.spec_from_file_location("emit_review_status", _PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load emit_review_status.py")
_mod = importlib.util.module_from_spec(_spec)
sys.modules["emit_review_status"] = _mod
_spec.loader.exec_module(_mod)


def test_ci_review_status_links_to_each_sensitive_file_change():
    results = _mod.build_results(
        ci_review=True,
        mcp_catalog=False,
        supply_chain=False,
        label_present=False,
        ci_review_files='[".github/workflows/ci.yml", "apps/desktop/eslint.config.mjs"]',
        repo_url="https://github.com/nousresearch/hermes-agent",
        base_sha="base456",
        head_sha="abc123",
    )

    assert results[0]["detail"] == (
        "**Sensitive files changed:**\n"
        "- [`.github/workflows/ci.yml`](https://github.com/nousresearch/hermes-agent/compare/base456...abc123#diff-b803fcb7f17ed9235f1e5cb1fcd2f5d3b2838429d4368ae4c57ce4436577f03f)\n"
        "- [`apps/desktop/eslint.config.mjs`](https://github.com/nousresearch/hermes-agent/compare/base456...abc123#diff-a45471520795db6e46840d1ba2a82c1f8a2841039bd60fb50624488c5f192438)"
    )


def test_approved_ci_review_is_visible_info():
    results = _mod.build_results(
        ci_review=True,
        mcp_catalog=False,
        supply_chain=False,
        label_present=True,
        ci_review_files='[".github/workflows/ci.yml"]',
        repo_url="https://github.com/nousresearch/hermes-agent",
        base_sha="base456",
        head_sha="abc123",
    )

    assert results == [{
        "kind": "info",
        "title": "CI-sensitive file review",
        "summary": (
            "PR touches sensitive files, but the `ci-reviewed` label has been "
            "added, approving them."
        ),
        "detail": (
            "**Sensitive files changed:**\n"
            "- [`.github/workflows/ci.yml`](https://github.com/nousresearch/hermes-agent/compare/base456...abc123#diff-b803fcb7f17ed9235f1e5cb1fcd2f5d3b2838429d4368ae4c57ce4436577f03f)"
        ),
    }]


def test_supply_chain_status_surfaces_exact_scanner_evidence():
    findings = (
        "### CRITICAL: base64 decode + exec/eval combo\n\n"
        "**Matches:**\n```\n+exec(base64.b64decode(payload))\n```"
    )

    statuses = _mod.build_supply_chain_status(findings)

    assert statuses == [{
        "source": "supply-chain-audit",
        "results": [{
            "kind": "action_required",
            "title": "Critical supply chain risk",
            "summary": "Critical supply chain risk patterns were detected in this PR.",
            "detail": findings,
            "how_to_fix": (
                "Review the flagged code carefully. If it is intentional, add the "
                "`ci-reviewed` label to confirm maintainer review."
            ),
        }],
    }]


def test_reviewed_supply_chain_evidence_remains_visible_without_action_required():
    findings = "### CRITICAL\nflagged.py:12"

    statuses = _mod.build_supply_chain_status(findings, label_present=True)

    assert statuses == [{
        "source": "supply-chain-audit",
        "results": [{
            "kind": "info",
            "title": "Critical supply chain risk",
            "summary": (
                "Critical supply chain risk patterns were detected and the "
                "`ci-reviewed` label confirms maintainer review."
            ),
            "detail": findings,
        }],
    }]


def test_supply_chain_workflow_publishes_findings_file():
    workflow = _SUPPLY_CHAIN_WORKFLOW.read_text(encoding="utf-8")

    assert "--supply-chain-findings-file /tmp/findings.md" in workflow
    assert 'args+=(--label-present)' in workflow
