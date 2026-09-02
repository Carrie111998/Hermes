"""Tests for modular, full-replacement Google Apps Script generation."""

import json
from pathlib import Path

import pytest

from agent.gas_project_generator import (
    GasProject,
    GasProjectValidationError,
    GasSourceFile,
    build_gas_generation_prompt,
    parse_generated_gas_project,
    validate_clasp_project,
)
from agent.request_context_budget import RequestContextBudget


SAMPLE_GENERATION = """### 01_Config.js
```javascript
const CONFIG = Object.freeze({
  SHEET_NAME: 'Orders',
  WEBHOOK_URL: 'https://example.invalid/webhook',
});
```

### 02_SpreadsheetService.js
```javascript
function appendOrder_(order) {
  SpreadsheetApp.getActive().getSheetByName(CONFIG.SHEET_NAME)
    .appendRow([order.id, order.status]);
}
```

### 03_WebhookService.js
```javascript
function notifyWebhook_(payload) {
  UrlFetchApp.fetch(CONFIG.WEBHOOK_URL, {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify(payload),
  });
}
```

### appsscript.json
```json
{
  "timeZone": "Asia/Bangkok",
  "exceptionLogging": "STACKDRIVER",
  "runtimeVersion": "V8"
}
```
"""


def test_parses_sample_into_sequential_full_replacement_files():
    project = parse_generated_gas_project(SAMPLE_GENERATION)

    assert [source.filename for source in project.source_files] == [
        "01_Config.js",
        "02_SpreadsheetService.js",
        "03_WebhookService.js",
    ]
    assert "appendOrder_" in project.source_files[1].content
    assert project.manifest["runtimeVersion"] == "V8"


def test_renders_each_source_as_a_complete_named_code_block():
    project = parse_generated_gas_project(SAMPLE_GENERATION)

    rendered = project.to_markdown()

    assert "### 01_Config.js\n```javascript" in rendered
    assert "### 02_SpreadsheetService.js\n```javascript" in rendered
    assert "### appsscript.json\n```json" in rendered
    assert "// ... existing code ..." not in rendered


def test_rejects_nonsequential_source_file_prefixes():
    with pytest.raises(GasProjectValidationError, match="sequential"):
        GasProject(
            source_files=(
                GasSourceFile("01_Config.js", "const CONFIG = {};"),
                GasSourceFile("03_Main.js", "function run() {}"),
            ),
            manifest={"timeZone": "Etc/UTC", "exceptionLogging": "STACKDRIVER"},
        )


def test_rejects_partial_replacement_markers():
    with pytest.raises(GasProjectValidationError, match="partial"):
        GasSourceFile("01_Config.js", "const CONFIG = {};\n// ... existing code ...")


def test_rejects_bare_ellipsis_in_source_block():
    incomplete = SAMPLE_GENERATION.replace(
        "const CONFIG = Object.freeze({", "const CONFIG = ...;", 1
    )

    with pytest.raises(GasProjectValidationError, match="partial"):
        parse_generated_gas_project(incomplete)


def test_rejects_wrong_fence_language_for_javascript_source():
    wrong_language = SAMPLE_GENERATION.replace("```javascript", "```python", 1)

    with pytest.raises(GasProjectValidationError, match="language"):
        parse_generated_gas_project(wrong_language)


def test_rejects_manifest_block_when_it_is_not_final():
    manifest_start = SAMPLE_GENERATION.index("### appsscript.json")
    manifest_first = SAMPLE_GENERATION[manifest_start:] + "\n" + SAMPLE_GENERATION[:manifest_start]

    with pytest.raises(GasProjectValidationError, match="final"):
        parse_generated_gas_project(manifest_first)


def test_writes_clasp_layout_and_validates_manifest(tmp_path: Path):
    project = parse_generated_gas_project(SAMPLE_GENERATION)

    project.write_to(tmp_path)

    assert (tmp_path / "01_Config.js").read_text(encoding="utf-8").startswith("const CONFIG")
    assert (tmp_path / "appsscript.json").exists()
    assert project.validate_manifest() == []


def test_clasp_validation_uses_injected_runner_without_network(tmp_path: Path):
    project = parse_generated_gas_project(SAMPLE_GENERATION)
    project.write_to(tmp_path)
    calls: list[tuple[list[str], Path]] = []

    result = validate_clasp_project(
        tmp_path,
        runner=lambda command, cwd: calls.append((command, cwd)) or (0, "ok", ""),
    )

    assert result.available is True
    assert result.valid is True
    assert calls == [(["clasp", "status"], tmp_path)]


def test_checked_in_webhook_example_is_a_valid_clasp_project():
    example_dir = Path(__file__).parents[2] / "examples" / "gas-order-webhook"
    source_files = tuple(
        GasSourceFile(path.name, path.read_text(encoding="utf-8"))
        for path in sorted(example_dir.glob("[0-9][0-9]_*.js"))
    )
    manifest = json.loads((example_dir / "appsscript.json").read_text(encoding="utf-8"))

    project = GasProject(source_files=source_files, manifest=manifest)

    assert project.validate_manifest() == []
    assert [source.filename for source in project.source_files] == [
        "01_Config.js",
        "02_SpreadsheetService.js",
        "03_WebhookService.js",
        "04_Main.js",
    ]


def test_generation_prompt_includes_budget_and_nonnegotiable_output_contract():
    budget = RequestContextBudget(
        context_window_tokens=16_000,
        reserved_output_tokens=2_000,
        system_prompt_tokens=1_000,
        tool_schema_tokens=500,
        confidence="rough",
    )

    prompt = build_gas_generation_prompt(
        "Create a spreadsheet order intake flow with webhook notification.", budget=budget
    )

    assert "12500" in prompt
    assert "01_" in prompt
    assert "full replacement" in prompt.lower()
    assert "appsscript.json" in prompt
