"""Spec 042 §5 — the ``workflow_ref`` resolver for the workflow catalog.

House pattern, same shape as ``contracts/agents.json`` /
``contracts/models.json``: ONE specification file
(``contracts/workflows.json`` in the talaryst umbrella), ONE
implementation (this module), and pytest drift guards
(``tests/hermes_cli/test_workflow_catalog_drift.py``) diffing the
resolver's supported vocabulary against the catalog rows and the
filesystem artifacts the rows cite.

The anti-pattern this replaces: ``factory/hooks/validate-work-type.sh``
hand-copied every downstream constraint into a sidecar script, the
registry it checked moved away, and the script exited 65 on every
invocation for two months while mistyped rows silently warn-dropped at
dispatch. Here the constraints live in ONE loader; the drift guard
diffs the loader's vocabulary against the tracked file, so a catalog
that drifts fails a test — loudly, at PR time, naming the row — instead
of degrading at dispatch.

Resolution posture:

* ``resolve(None)`` → ``None``. A card without ``workflow_ref`` is the
  classic free-prompt worker; absence of a binding is not an error.
* An unknown ref raises :class:`UnknownWorkflowError` naming the ref
  and the keys the catalog DOES know — the clean failure. Never a
  warn-drop.
* A missing or malformed catalog raises :class:`WorkflowCatalogError`.
  The engine runs installed (``~/.hermes/hermes-agent``) as often as it
  runs inside the umbrella checkout, so the honest absence of the
  contract is a first-class outcome, never a traceback.

Catalog location, in order:

1. the explicit ``path`` argument,
2. ``$HERMES_WORKFLOW_CATALOG`` (deployed/dispatcher pin),
3. a walk up from this file for ``contracts/workflows.json`` — the
   engine-as-submodule layout (``harness/hermes/engine`` inside the
   talaryst umbrella) finds the repo copy this way.

``workflow_args`` are validated against the row's ``args_schema`` at
resolve time (kanban_db stores them as a canonical JSON object at
filing; the schema subset the catalog uses — object type, ``required``,
per-property ``type``/``enum``/``pattern``/``default`` — is enforced
here, with defaults applied to the returned copy).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

# ---------------------------------------------------------------------------
# The resolver's supported vocabulary (spec 042 §5, closed enums).
#
# These sets ARE the "supported work types" the drift guard diffs against
# the catalog rows: a row carrying a value outside its field's set is a
# row this resolver cannot honour, and the drift test fails naming it.
# Adding to the catalog's vocabulary means editing these sets in the same
# PR — one implementation, one vocabulary.
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1

KINDS = frozenset({"script", "skill", "agent-preset", "prompt", "loop"})
DIALECTS = frozenset({
    "claude-js", "hermes-py", "omp-js", "pi-js", "lobster-yaml",
    "omp-md", "kimi-yaml", "hermes-preset", "claude-skill",
})
RESOLUTIONS = frozenset({
    "name-ladder", "tracked-path", "frontmatter-name", "registry-row",
})
#: Catalog runner vocabulary. ``any`` is the lobster affinity marker, not
#: a spawnable runner — distinct from kanban_db.VALID_RUNNERS on purpose.
RUNNERS = frozenset({"hermes", "kimi", "claude", "pi", "omp", "any"})
GATE_KINDS = frozenset({"none", "approval", "resume-token"})
HONORS = frozenset({"enforced", "guidance"})

ROW_KEYS = frozenset({
    "key", "kind", "dialect", "runner_affinity", "resolution",
    "description", "args_schema", "capabilities", "honors", "source",
})
CAPABILITY_KEYS = frozenset({
    "launchable", "has_turn_cap", "has_cost", "gate_kind", "needs_worktree",
})
_BOOL_CAPABILITY_KEYS = CAPABILITY_KEYS - {"gate_kind"}

#: Environment pin for deployments where the engine runs outside the
#: umbrella checkout (e.g. the deployed copy at ~/.hermes/hermes-agent).
CATALOG_ENV_VAR = "HERMES_WORKFLOW_CATALOG"

_SOURCE_RE = re.compile(r"^(?P<path>.+):(?P<line>\d+)$")

#: JSON-schema type name → python types. bool is an int subclass, so
#: "integer" must exclude it explicitly — see _check_type.
_JSON_TYPES = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "boolean": (bool,),
    "integer": (int,),
    "number": (int, float),
}


class WorkflowCatalogError(Exception):
    """The catalog file is missing, unreadable, or malformed."""


class UnknownWorkflowError(WorkflowCatalogError):
    """A ``workflow_ref`` no catalog row knows — the clean failure.

    Carries the offending ``ref`` and the sorted ``available`` keys so
    the dispatcher (and the operator) see the real vocabulary instead of
    a warn-drop.
    """

    def __init__(self, ref: str, available: list[str]):
        self.ref = ref
        self.available = available
        known = ", ".join(available) if available else "(catalog is empty)"
        super().__init__(
            f"unknown workflow_ref {ref!r} — no row in the workflow "
            f"catalog has this key (known keys: {known})"
        )


class WorkflowArgsError(WorkflowCatalogError):
    """``workflow_args`` fail the row's ``args_schema`` at resolve time."""


@dataclass(frozen=True)
class WorkflowRow:
    """One catalog row, validated."""

    key: str
    kind: str
    dialect: str
    runner_affinity: tuple[str, ...]
    resolution: str
    description: str
    args_schema: Mapping[str, Any] = field(default_factory=dict)
    capabilities: Mapping[str, Any] = field(default_factory=dict)
    honors: str = "guidance"
    source: str = ""

    @property
    def launchable(self) -> bool:
        return bool(self.capabilities.get("launchable"))

    @property
    def gate_kind(self) -> str:
        return str(self.capabilities.get("gate_kind", "none"))

    @property
    def source_path(self) -> str:
        """The path half of ``source`` (``path:line``)."""
        m = _SOURCE_RE.match(self.source)
        return m.group("path") if m else self.source

    @property
    def source_line(self) -> Optional[int]:
        m = _SOURCE_RE.match(self.source)
        return int(m.group("line")) if m else None


@dataclass(frozen=True)
class ResolvedWorkflow:
    """The resolver's answer: the catalog row plus the effective args
    (defaults applied, schema-validated)."""

    row: WorkflowRow
    args: Mapping[str, Any]


class Catalog:
    """A parsed, validated ``contracts/workflows.json``.

    Rows are keyed by their stable slug; duplicate keys are a load-time
    error, not a last-write-wins.
    """

    def __init__(self, rows: list[WorkflowRow], path: Path, updated_at: str):
        self._rows = {r.key: r for r in rows}
        self.path = path
        self.updated_at = updated_at

    def __contains__(self, key: str) -> bool:
        return key in self._rows

    def __len__(self) -> int:
        return len(self._rows)

    def keys(self) -> list[str]:
        return sorted(self._rows)

    def rows(self) -> list[WorkflowRow]:
        return [self._rows[k] for k in sorted(self._rows)]

    def get(self, key: str) -> WorkflowRow:
        """Return the row for ``key`` or raise UnknownWorkflowError."""
        try:
            return self._rows[key]
        except KeyError:
            raise UnknownWorkflowError(key, self.keys()) from None


# ---------------------------------------------------------------------------
# Catalog location
# ---------------------------------------------------------------------------


def find_catalog(start: Optional[Path] = None) -> Optional[Path]:
    """Walk up from ``start`` (default: this package) looking for
    ``contracts/workflows.json``. Returns None when no ancestor carries
    the contract — the engine-as-submodule layout finds the umbrella
    copy; a standalone or installed engine honestly does not."""
    here = Path(start) if start is not None else Path(__file__)
    here = here.resolve()
    for parent in (here, *here.parents):
        candidate = parent / "contracts" / "workflows.json"
        if candidate.is_file():
            return candidate
    return None


def default_catalog_path() -> Path:
    """Resolve the catalog path: env pin, then the walk-up. Raises
    WorkflowCatalogError when neither finds one."""
    pinned = os.environ.get(CATALOG_ENV_VAR, "").strip()
    if pinned:
        path = Path(pinned)
        if not path.is_file():
            raise WorkflowCatalogError(
                f"{CATALOG_ENV_VAR} points at {path}, which is not a file"
            )
        return path
    found = find_catalog()
    if found is None:
        raise WorkflowCatalogError(
            "no workflow catalog found: set "
            f"{CATALOG_ENV_VAR} or run inside the talaryst umbrella "
            "(contracts/workflows.json)"
        )
    return found


# ---------------------------------------------------------------------------
# Loading + validation
# ---------------------------------------------------------------------------


def _fail(path: Path, msg: str) -> WorkflowCatalogError:
    return WorkflowCatalogError(f"{path}: {msg}")


def _validate_row(raw: Any, path: Path, index: int) -> WorkflowRow:
    where = f"workflows[{index}]"
    if not isinstance(raw, dict):
        raise _fail(path, f"{where} must be an object")
    key = raw.get("key")
    if isinstance(key, str) and key.strip():
        where = f"row {key.strip()!r}"
    missing = sorted(ROW_KEYS - raw.keys())
    if missing:
        raise _fail(path, f"{where} missing keys: {', '.join(missing)}")

    key = raw["key"]
    if not isinstance(key, str) or not key.strip():
        raise _fail(path, f"{where}: key must be a non-empty string")
    key = key.strip()

    kind = raw["kind"]
    if kind not in KINDS:
        raise _fail(
            path, f"row {key!r}: kind {kind!r} is not one of {sorted(KINDS)}"
        )
    dialect = raw["dialect"]
    if dialect not in DIALECTS:
        raise _fail(
            path,
            f"row {key!r}: dialect {dialect!r} is not one of {sorted(DIALECTS)}",
        )
    resolution = raw["resolution"]
    if resolution not in RESOLUTIONS:
        raise _fail(
            path,
            f"row {key!r}: resolution {resolution!r} is not one of "
            f"{sorted(RESOLUTIONS)}",
        )
    honors = raw["honors"]
    if honors not in HONORS:
        raise _fail(
            path, f"row {key!r}: honors {honors!r} is not one of {sorted(HONORS)}"
        )

    affinity = raw["runner_affinity"]
    if (
        not isinstance(affinity, list)
        or not affinity
        or any(a not in RUNNERS for a in affinity)
    ):
        raise _fail(
            path,
            f"row {key!r}: runner_affinity must be a non-empty subset of "
            f"{sorted(RUNNERS)}, got {affinity!r}",
        )

    caps = raw["capabilities"]
    if not isinstance(caps, dict):
        raise _fail(path, f"row {key!r}: capabilities must be an object")
    missing_caps = sorted(CAPABILITY_KEYS - caps.keys())
    if missing_caps:
        raise _fail(
            path,
            f"row {key!r}: capabilities missing keys: {', '.join(missing_caps)}",
        )
    for name in sorted(_BOOL_CAPABILITY_KEYS):
        if not isinstance(caps.get(name), bool):
            raise _fail(
                path, f"row {key!r}: capabilities.{name} must be a boolean"
            )
    if caps["gate_kind"] not in GATE_KINDS:
        raise _fail(
            path,
            f"row {key!r}: capabilities.gate_kind {caps['gate_kind']!r} is "
            f"not one of {sorted(GATE_KINDS)}",
        )

    args_schema = raw["args_schema"]
    if not isinstance(args_schema, dict):
        raise _fail(path, f"row {key!r}: args_schema must be an object")
    if args_schema.get("type", "object") != "object":
        raise _fail(
            path,
            f"row {key!r}: args_schema.type must be \"object\" — the resolver "
            "validates object schemas only",
        )

    description = raw["description"]
    if not isinstance(description, str) or not description.strip():
        raise _fail(
            path,
            f"row {key!r}: description must be non-empty — it is what "
            "routing matches on",
        )

    source = raw["source"]
    if not isinstance(source, str) or not _SOURCE_RE.match(source):
        raise _fail(
            path, f"row {key!r}: source must be \"path:line\", got {source!r}"
        )

    return WorkflowRow(
        key=key,
        kind=kind,
        dialect=dialect,
        runner_affinity=tuple(affinity),
        resolution=resolution,
        description=description,
        args_schema=args_schema,
        capabilities=caps,
        honors=honors,
        source=source,
    )


def load_catalog(path: Optional[Path] = None) -> Catalog:
    """Load and strictly validate the catalog. Raises
    WorkflowCatalogError on any structural violation."""
    catalog_path = Path(path) if path is not None else default_catalog_path()
    try:
        raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise _fail(catalog_path, "catalog file does not exist") from None
    except json.JSONDecodeError as exc:
        raise _fail(catalog_path, f"catalog is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise _fail(catalog_path, "catalog root must be an object")
    schema = raw.get("schema")
    if schema != SCHEMA_VERSION:
        raise _fail(
            catalog_path,
            f"catalog schema {schema!r} — this resolver supports "
            f"schema {SCHEMA_VERSION}",
        )
    rows_raw = raw.get("workflows")
    if not isinstance(rows_raw, list):
        raise _fail(catalog_path, "catalog.workflows must be a list")

    rows: list[WorkflowRow] = []
    seen: set[str] = set()
    for i, entry in enumerate(rows_raw):
        row = _validate_row(entry, catalog_path, i)
        if row.key in seen:
            raise _fail(
                catalog_path, f"duplicate workflow key {row.key!r} in catalog"
            )
        seen.add(row.key)
        rows.append(row)
    return Catalog(rows, catalog_path, str(raw.get("updatedAt", "")))


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _check_type(value: Any, expected: str) -> bool:
    types = _JSON_TYPES.get(expected)
    if types is None:
        return True  # unknown type names: guidance, not a gate
    if expected == "integer":
        # bool is an int subclass; JSON true/false is never an integer.
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, types)


def validate_args(
    schema: Mapping[str, Any], args: Mapping[str, Any], ref: str = ""
) -> dict:
    """Validate ``args`` against the row's ``args_schema`` and return the
    effective args (defaults applied) as a NEW dict.

    The subset enforced is exactly what the catalog uses: object type,
    ``required``, and per-property ``type`` / ``enum`` / ``pattern`` /
    ``default``. Undeclared keys pass through (no row declares
    ``additionalProperties: false`` — the schema is guidance-shaped, and
    tightening that is a catalog PR, not a resolver decision).
    """
    where = f"workflow_args for {ref!r}" if ref else "workflow_args"
    if not isinstance(args, Mapping):
        raise WorkflowArgsError(f"{where} must be a JSON object")
    effective = dict(args)

    properties = schema.get("properties") or {}
    if not isinstance(properties, dict):
        raise WorkflowArgsError(f"{where}: schema properties must be an object")

    for name in schema.get("required") or []:
        if name not in effective:
            raise WorkflowArgsError(f"{where} missing required key {name!r}")

    for name, prop in properties.items():
        if not isinstance(prop, dict):
            continue
        if name not in effective:
            if "default" in prop:
                effective[name] = prop["default"]
            continue
        value = effective[name]
        expected = prop.get("type")
        if expected and not _check_type(value, expected):
            raise WorkflowArgsError(
                f"{where}: key {name!r} must be {expected}, got "
                f"{json.dumps(value)}"
            )
        if "enum" in prop and value not in prop["enum"]:
            raise WorkflowArgsError(
                f"{where}: key {name!r} must be one of {prop['enum']}, got "
                f"{json.dumps(value)}"
            )
        pattern = prop.get("pattern")
        if pattern and isinstance(value, str) and not re.search(pattern, value):
            raise WorkflowArgsError(
                f"{where}: key {name!r} must match /{pattern}/, got {value!r}"
            )
    return effective


def resolve(ref: Optional[str], catalog: Optional[Catalog] = None) -> Optional[WorkflowRow]:
    """Map a ``workflow_ref`` to its catalog row.

    None/blank → None (no binding). Unknown ref → UnknownWorkflowError
    naming the ref and the known keys. Never a warn-drop.
    """
    ref = (ref or "").strip()
    if not ref:
        return None
    cat = catalog if catalog is not None else load_catalog()
    return cat.get(ref)


def resolve_task(
    task: Any, catalog: Optional[Catalog] = None
) -> Optional[ResolvedWorkflow]:
    """Resolve a kanban Task's workflow binding.

    ``task`` is anything with ``workflow_ref`` / ``workflow_args``
    attributes (kanban_db.Task). Returns None for unbound cards;
    otherwise the row plus the effective args — parsed from the stored
    canonical JSON, validated against the row's ``args_schema`` with
    defaults applied.
    """
    ref = (getattr(task, "workflow_ref", None) or "").strip()
    if not ref:
        return None
    row = resolve(ref, catalog)
    assert row is not None  # non-blank ref always returns a row or raises
    raw_args = (getattr(task, "workflow_args", None) or "").strip()
    args: Mapping[str, Any] = {}
    if raw_args:
        try:
            parsed = json.loads(raw_args)
        except json.JSONDecodeError:
            raise WorkflowArgsError(
                f"workflow_args for {ref!r} is not valid JSON"
            ) from None
        if not isinstance(parsed, dict):
            raise WorkflowArgsError(
                f"workflow_args for {ref!r} must be a JSON object"
            )
        args = parsed
    return ResolvedWorkflow(row=row, args=validate_args(row.args_schema, args, ref))
