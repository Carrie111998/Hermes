"""Metadata invariants for deterministic website tooling."""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WEBSITE_PACKAGE = ROOT / "website" / "package.json"
DOCS_WORKFLOW = ROOT / ".github" / "workflows" / "docs-site-checks.yml"


def test_diagram_lint_self_provisions_pinned_ascii_guard():
    package = json.loads(WEBSITE_PACKAGE.read_text(encoding="utf-8"))
    command = package["scripts"]["lint:diagrams"]

    assert command == (
        "uvx --from ascii-guard==2.3.0 --with pyyaml==6.0.3 "
        "ascii-guard lint --exclude-code-blocks docs"
    )

    workflow = DOCS_WORKFLOW.read_text(encoding="utf-8")
    assert "astral-sh/setup-uv@fac544c07dec837d0ccb6301d7b5580bf5edae39" in workflow
    assert 'version: "0.9.28"' in workflow
    assert "python -m pip install ascii-guard" not in workflow
