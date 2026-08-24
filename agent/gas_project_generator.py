"""Validated modular Google Apps Script project generation contracts.

This module is deliberately request-local: it turns an LLM's proposed project
into a validated artifact without changing sessions, SessionDB, or compaction
state.  Callers can use ``build_gas_generation_prompt`` with the current
``RequestContextBudget`` before asking a model to generate source files.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from agent.request_context_budget import RequestContextBudget


_SOURCE_FILENAME_RE = re.compile(r"^(?P<index>[0-9]{2})_[A-Za-z][A-Za-z0-9_]*\.(?:js|html)$")
_PARTIAL_MARKER_RE = re.compile(
    r"(?:\.\.\.(?![A-Za-z_$])|<\s*(?:existing|rest|remaining))",
    re.IGNORECASE,
)
_BLOCK_RE = re.compile(
    r"^###\s+(?P<filename>[^\r\n]+)\r?\n"
    r"```(?P<language>[A-Za-z0-9_+-]*)\r?\n"
    r"(?P<content>.*?)\r?\n```[ \t]*$",
    re.MULTILINE | re.DOTALL,
)


class GasProjectValidationError(ValueError):
    """Raised when a generated GAS project is unsafe or incomplete."""


@dataclass(frozen=True)
class GasSourceFile:
    """One full-replacement Apps Script source file."""

    filename: str
    content: str

    def __post_init__(self) -> None:
        if not _SOURCE_FILENAME_RE.fullmatch(self.filename):
            raise GasProjectValidationError(
                "source filename must be a two-digit sequential prefix followed by a name and .js/.html"
            )
        if not self.content.strip():
            raise GasProjectValidationError(f"{self.filename} must contain complete source code")
        if _PARTIAL_MARKER_RE.search(self.content):
            raise GasProjectValidationError(f"{self.filename} contains a prohibited partial replacement marker")

    @property
    def index(self) -> int:
        match = _SOURCE_FILENAME_RE.fullmatch(self.filename)
        assert match is not None
        return int(match.group("index"))

    @property
    def fence_language(self) -> str:
        return "html" if self.filename.endswith(".html") else "javascript"


@dataclass(frozen=True)
class ClaspValidationResult:
    """Result of an optional local ``clasp status`` validation."""

    available: bool
    valid: bool
    stdout: str
    stderr: str


@dataclass(frozen=True)
class GasProject:
    """A complete copy-pasteable Apps Script project plus manifest."""

    source_files: Sequence[GasSourceFile]
    manifest: Mapping[str, object]

    def __post_init__(self) -> None:
        source_files = tuple(self.source_files)
        object.__setattr__(self, "source_files", source_files)
        if not source_files:
            raise GasProjectValidationError("at least one numbered GAS source file is required")
        names = [source.filename for source in source_files]
        if len(names) != len(set(names)):
            raise GasProjectValidationError("source filenames must be unique")
        indices = [source.index for source in source_files]
        if indices != list(range(1, len(source_files) + 1)):
            raise GasProjectValidationError("source filenames must be sequential starting at 01")
        manifest_errors = self.validate_manifest()
        if manifest_errors:
            raise GasProjectValidationError("invalid appsscript.json: " + "; ".join(manifest_errors))

    def validate_manifest(self) -> list[str]:
        """Return deterministic manifest-contract errors without calling Google."""
        errors: list[str] = []
        time_zone = self.manifest.get("timeZone")
        if not isinstance(time_zone, str) or not time_zone.strip():
            errors.append("timeZone must be a non-empty string")
        exception_logging = self.manifest.get("exceptionLogging")
        if exception_logging not in {"STACKDRIVER", "CLOUD_LOGGING", "NONE"}:
            errors.append("exceptionLogging must be STACKDRIVER, CLOUD_LOGGING, or NONE")
        runtime_version = self.manifest.get("runtimeVersion")
        if runtime_version is not None and runtime_version != "V8":
            errors.append("runtimeVersion must be V8 when specified")
        scopes = self.manifest.get("oauthScopes")
        if scopes is not None and (
            not isinstance(scopes, list) or not all(isinstance(scope, str) and scope for scope in scopes)
        ):
            errors.append("oauthScopes must be a list of non-empty strings")
        return errors

    def to_markdown(self) -> str:
        """Render only full-file replacement blocks, in upload order."""
        blocks = [
            f"### {source.filename}\n```{source.fence_language}\n{source.content.rstrip()}\n```"
            for source in self.source_files
        ]
        manifest = json.dumps(dict(self.manifest), ensure_ascii=False, indent=2, sort_keys=True)
        blocks.append(f"### appsscript.json\n```json\n{manifest}\n```")
        return "\n\n".join(blocks)

    def write_to(self, project_dir: Path) -> None:
        """Write the validated clasp-compatible project layout to an empty/safe directory."""
        project_dir.mkdir(parents=True, exist_ok=True)
        for source in self.source_files:
            (project_dir / source.filename).write_text(source.content.rstrip() + "\n", encoding="utf-8")
        manifest = json.dumps(dict(self.manifest), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        (project_dir / "appsscript.json").write_text(manifest, encoding="utf-8")


ClaspRunner = Callable[[list[str], Path], tuple[int, str, str]]


def _subprocess_clasp_runner(command: list[str], cwd: Path) -> tuple[int, str, str]:
    completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    return completed.returncode, completed.stdout, completed.stderr


def validate_clasp_project(project_dir: Path, *, runner: ClaspRunner | None = None) -> ClaspValidationResult:
    """Run the non-mutating ``clasp status`` check when clasp is available.

    Authentication and project binding remain the user's responsibility.  This
    function never pushes, pulls, or changes a remote Apps Script project.
    """
    manifest_path = project_dir / "appsscript.json"
    if not manifest_path.is_file():
        return ClaspValidationResult(False, False, "", "appsscript.json is missing")
    if runner is None and shutil.which("clasp") is None:
        return ClaspValidationResult(False, False, "", "clasp is not installed")
    active_runner = runner or _subprocess_clasp_runner
    return_code, stdout, stderr = active_runner(["clasp", "status"], project_dir)
    return ClaspValidationResult(True, return_code == 0, stdout, stderr)


def parse_generated_gas_project(markdown: str) -> GasProject:
    """Parse an LLM response that follows the full-replacement output contract."""
    blocks = list(_BLOCK_RE.finditer(markdown))
    if not blocks:
        raise GasProjectValidationError("no named fenced GAS file blocks were found")

    source_files: list[GasSourceFile] = []
    manifest: dict[str, object] | None = None
    seen: set[str] = set()
    for position, block in enumerate(blocks):
        filename = block.group("filename").strip()
        language = block.group("language").lower()
        content = block.group("content")
        if filename in seen:
            raise GasProjectValidationError(f"duplicate output block: {filename}")
        seen.add(filename)
        if filename == "appsscript.json":
            if position != len(blocks) - 1:
                raise GasProjectValidationError("appsscript.json must be the final output block")
            if language != "json":
                raise GasProjectValidationError("appsscript.json must use a json fenced-block language")
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                raise GasProjectValidationError(f"appsscript.json is invalid JSON: {exc.msg}") from exc
            if not isinstance(parsed, dict):
                raise GasProjectValidationError("appsscript.json must be a JSON object")
            manifest = parsed
            continue
        expected_language = "html" if filename.endswith(".html") else "javascript"
        accepted_languages = {expected_language}
        if expected_language == "javascript":
            accepted_languages.add("js")
        if language not in accepted_languages:
            raise GasProjectValidationError(
                f"{filename} must use a {expected_language} fenced-block language"
            )
        source_files.append(GasSourceFile(filename, content))

    if manifest is None:
        raise GasProjectValidationError("appsscript.json full replacement block is required")
    return GasProject(source_files=tuple(source_files), manifest=manifest)


def build_gas_generation_prompt(requirements: str, *, budget: RequestContextBudget) -> str:
    """Build a budget-aware system/developer prompt for a GAS generation turn."""
    if not requirements.strip():
        raise ValueError("requirements must not be blank")
    return f"""Generate a complete Google Apps Script project for the following requirements:
{requirements.strip()}

Output contract (non-negotiable):
1. Split responsibilities into numbered source files named exactly `01_Name.js`, `02_Name.js`, and so on. Start at `01` and use every number without gaps.
2. For every source file, output `### filename` followed by one fenced `javascript` or `html` block containing the full replacement content of that file. Never emit a diff, ellipsis, placeholder, or instructions to merge with existing code.
3. Output one final `### appsscript.json` fenced `json` block. It must be a complete JSON object with a non-empty `timeZone`, valid `exceptionLogging`, and `runtimeVersion: "V8"` when specified.
4. Keep code Apps Script V8 compatible. Declare only the minimum OAuth scopes needed by the requested APIs.
5. Before finalizing, self-check sequential filenames, cross-file references, manifest JSON, and that every block is a full replacement.

Request context guard:
- This request has {budget.history_budget_tokens} history tokens available after reserving output, active system prompt, and tool schemas.
- Budget confidence is `{budget.confidence}`. Prefer concise modules; if requirements cannot fit safely, finish the current complete project rather than truncating a file.
"""
