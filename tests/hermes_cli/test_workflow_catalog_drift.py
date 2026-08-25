"""Spec 042 §5 — drift guards for the workflow catalog.

The house pattern's third leg: pytest tests diffing the resolver's
supported vocabulary (``hermes_cli.workflow_resolver``) against the
TRACKED ``contracts/workflows.json`` rows and the filesystem artifacts
those rows cite. This replaces the ``validate-work-type.sh`` exit-65
incident's shape — a sidecar script hand-copying constraints until the
registry moved and the check died. Here nothing is copied: the tests
load the real catalog with the real loader and walk the real tree, so
drift in ANY of the three legs (vocabulary, catalog, filesystem) fails
a test at PR time, naming the row.

These tests need the talaryst umbrella checkout (the catalog and the
artifacts live there; the engine is a submodule at
``harness/hermes/engine``). Standalone engine clones honestly do not
have it → the whole module skips with a named reason, the same posture
as ``apps/os-api/tests/test_mounts.py::ABSENT_SOURCES`` and the
``needs_observatory`` skipif in the design-system drift suite.

Guard set (all derived from spec 042 §5, nothing invented):

1. The tracked catalog loads under the strict loader and every key
   resolves.
2. Every row's enum fields stay inside the resolver's vocabulary
   (kind / dialect / resolution / honors / gate_kind /
   runner_affinity) — a value the resolver doesn't know is a row it
   can't honour.
3. Every row's ``source: path:line`` exists in the umbrella and the
   line number lands inside the file.
4. ``frontmatter-name`` rows: the cited artifact's frontmatter ``name``
   equals the row key.
5. ``hermes-preset`` rows: launchable presets appear in the hermes
   roster of ``contracts/agents.json``; non-launchable ones are the
   known unexposed set, explicitly named in the roster's honorsNote
   (today: kimi-builder — expose-it is its own card, spec 042 §5).
6. Reverse direction — every filesystem agent is catalogued:
   harness/kimi/agents/*.md, agents/omp/agents/*.md frontmatter names,
   the launchable workflow script files (workflows/*.js,
   workflows/omp/*.js, workflows/hermes/*.py, workflows/lobster/*.lobster),
   and the hermes preset roster in contracts/agents.json all have rows.
7. The never-catalogued rules: no row cites anything under
   workflows/legacy/ (retired; re-registration trap —
   workflows/README.md:205-212), no ``pi-js`` row while workflows/pi/
   does not exist, and no row cites a cron custodian
   (review-loop-sweeper.sh et al. — global sweeps, wrong shape for
   per-card assignment).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import workflow_resolver as wr

#: The umbrella root = the ancestor carrying contracts/workflows.json,
#: found by the same walk the resolver uses.
_CATALOG_PATH = wr.find_catalog()
_UMBRELLA = _CATALOG_PATH.parent.parent if _CATALOG_PATH else None

pytestmark = pytest.mark.skipif(
    _UMBRELLA is None,
    reason="no talaryst umbrella checkout above this engine — the workflow "
    "catalog and its artifacts live there",
)


@pytest.fixture(scope="module")
def umbrella() -> Path:
    assert _UMBRELLA is not None
    return _UMBRELLA


@pytest.fixture(scope="module")
def catalog() -> wr.Catalog:
    assert _CATALOG_PATH is not None
    return wr.load_catalog(_CATALOG_PATH)


def _frontmatter_name(path: Path) -> str | None:
    """The ``name:`` line of a YAML-frontmatter markdown file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            return None
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip() or None
    return None


def _agents_json(umbrella: Path) -> dict:
    return json.loads((umbrella / "contracts" / "agents.json").read_text())


# ---------------------------------------------------------------------------
# 1. Tracked catalog loads + resolves
# ---------------------------------------------------------------------------


def test_tracked_catalog_loads_and_every_key_resolves(catalog):
    assert len(catalog) > 0
    for key in catalog.keys():
        row = wr.resolve(key, catalog)
        assert row is not None and row.key == key


def test_envelope_carries_schema_and_note(catalog):
    assert catalog.updated_at
    raw = json.loads(catalog.path.read_text())
    assert raw.get("note", "").strip(), "the contract documents itself"


# ---------------------------------------------------------------------------
# 2. Rows stay inside the resolver's vocabulary
# ---------------------------------------------------------------------------


def test_row_enums_stay_inside_resolver_vocabulary(catalog):
    # load_catalog already rejects out-of-vocabulary rows; this guard is
    # the explicit diff the spec asks for — if the loader is ever
    # relaxed, this still fails naming the row and the field.
    for row in catalog.rows():
        assert row.kind in wr.KINDS, row.key
        assert row.dialect in wr.DIALECTS, row.key
        assert row.resolution in wr.RESOLUTIONS, row.key
        assert row.honors in wr.HONORS, row.key
        assert row.gate_kind in wr.GATE_KINDS, row.key
        assert set(row.runner_affinity) <= wr.RUNNERS, row.key


def test_resolver_vocabulary_matches_spec_enums(catalog):
    """The spec §5 enum lists, pinned: a catalog PR adding a value must
    land with the resolver edit in the same PR."""
    assert wr.KINDS == {"script", "skill", "agent-preset", "prompt", "loop"}
    assert wr.DIALECTS == {
        "claude-js", "hermes-py", "omp-js", "pi-js", "lobster-yaml",
        "omp-md", "kimi-yaml", "hermes-preset", "claude-skill",
    }
    assert wr.RESOLUTIONS == {
        "name-ladder", "tracked-path", "frontmatter-name", "registry-row",
    }
    assert wr.RUNNERS == {"hermes", "kimi", "claude", "pi", "omp", "any"}
    assert wr.GATE_KINDS == {"none", "approval", "resume-token"}
    assert wr.HONORS == {"enforced", "guidance"}


# ---------------------------------------------------------------------------
# 3. Row sources exist
# ---------------------------------------------------------------------------


def test_every_row_source_exists_with_valid_line(catalog, umbrella):
    for row in catalog.rows():
        source_file = umbrella / row.source_path
        assert source_file.is_file(), (
            f"row {row.key!r} cites {row.source}, which does not exist — "
            "fix the row or restore the artifact"
        )
        line_count = len(source_file.read_text(encoding="utf-8").splitlines())
        line = row.source_line
        assert line is not None and 1 <= line <= line_count, (
            f"row {row.key!r} cites {row.source} but the file is "
            f"{line_count} lines"
        )


# ---------------------------------------------------------------------------
# 4. frontmatter-name rows: artifact name == row key
# ---------------------------------------------------------------------------


def test_frontmatter_name_rows_match_their_artifact(catalog, umbrella):
    rows = [r for r in catalog.rows() if r.resolution == "frontmatter-name"]
    assert rows, "catalog carries frontmatter-name rows today"
    for row in rows:
        name = _frontmatter_name(umbrella / row.source_path)
        assert name == row.key, (
            f"row {row.key!r} resolves by frontmatter name but "
            f"{row.source_path} declares name: {name!r}"
        )


# ---------------------------------------------------------------------------
# 5. hermes-preset rows vs contracts/agents.json
# ---------------------------------------------------------------------------


def test_hermes_presets_match_agents_contract(catalog, umbrella):
    agents_json = _agents_json(umbrella)
    roster = {a["key"] for a in agents_json["harnesses"]["hermes"]["agents"]}
    note = agents_json["harnesses"]["hermes"].get("honorsNote", "")
    preset_rows = [r for r in catalog.rows() if r.dialect == "hermes-preset"]
    assert preset_rows, "catalog carries hermes-preset rows today"
    for row in preset_rows:
        if row.launchable:
            assert row.key in roster, (
                f"launchable hermes preset {row.key!r} is not in "
                "contracts/agents.json — an unknown agentType raises "
                "ChildAgentError at dispatch"
            )
        else:
            # The known unexposed set (spec 042 §5: kimi-builder ships in
            # the preset directory but stays withheld from agents.json —
            # exposing it is its own card). The withhold must be on the
            # record, not silent.
            assert row.key in note, (
                f"non-launchable hermes preset {row.key!r} is neither in "
                "the agents.json roster nor named in its honorsNote"
            )


# ---------------------------------------------------------------------------
# 6. Reverse: every filesystem agent is catalogued
# ---------------------------------------------------------------------------


def test_kimi_agents_are_catalogued(catalog, umbrella):
    agents_dir = umbrella / "harness" / "kimi" / "agents"
    assert agents_dir.is_dir(), "harness/kimi/agents/ moved — update this guard"
    for md in sorted(agents_dir.glob("*.md")):
        name = _frontmatter_name(md)
        assert name, f"{md} carries no frontmatter name"
        assert name in catalog, (
            f"kimi agent {name!r} ({md.relative_to(umbrella)}) has no "
            "catalog row — add the row or delete the agent"
        )


def test_omp_agents_are_catalogued(catalog, umbrella):
    agents_dir = umbrella / "agents" / "omp" / "agents"
    assert agents_dir.is_dir(), "agents/omp/agents/ moved — update this guard"
    for md in sorted(agents_dir.glob("*.md")):
        name = _frontmatter_name(md)
        assert name, f"{md} carries no frontmatter name"
        assert name in catalog, (
            f"omp agent {name!r} ({md.relative_to(umbrella)}) has no "
            "catalog row — add the row or delete the agent"
        )


def test_workflow_script_files_are_catalogued(catalog, umbrella):
    """Every launchable script artifact has a row. Support tooling
    (workflows/tools/) and the retired legacy/ tree are not workflows
    and stay out — guard 7 asserts legacy stays out."""
    cited = {row.source_path for row in catalog.rows()}
    script_dirs = {
        "workflows": ("*.js",),
        "workflows/omp": ("*.js",),
        "workflows/hermes": ("*.py",),
        "workflows/lobster": ("*.lobster",),
    }
    for rel_dir, globs in script_dirs.items():
        directory = umbrella / rel_dir
        if not directory.is_dir():
            continue
        for pattern in globs:
            for script in sorted(directory.glob(pattern)):
                rel = str(script.relative_to(umbrella))
                assert rel in cited, (
                    f"workflow script {rel} has no catalog row — add the "
                    "row or move the file out of the launchable set"
                )


def test_agents_contract_hermes_roster_is_catalogued(catalog, umbrella):
    roster = {
        a["key"] for a in _agents_json(umbrella)["harnesses"]["hermes"]["agents"]
    }
    missing = roster - set(catalog.keys())
    assert not missing, (
        f"hermes presets in contracts/agents.json without a catalog row: "
        f"{sorted(missing)}"
    )


# ---------------------------------------------------------------------------
# 7. Never catalogued (spec 042 §5, explicit)
# ---------------------------------------------------------------------------


def test_no_row_cites_retired_legacy_workflows(catalog):
    for row in catalog.rows():
        assert not row.source_path.startswith("workflows/legacy/"), (
            f"row {row.key!r} cites {row.source_path} — workflows/legacy/ "
            "is retired (re-registration trap, workflows/README.md:205-212)"
        )


def test_no_pi_js_rows_while_the_directory_does_not_exist(catalog, umbrella):
    if not (umbrella / "workflows" / "pi").is_dir():
        pi_rows = [r.key for r in catalog.rows() if r.dialect == "pi-js"]
        assert not pi_rows, (
            f"pi-js rows without a workflows/pi/ directory are mechanisms "
            f"without instances (spec 042 §5): {pi_rows}"
        )


def test_no_row_cites_a_cron_custodian(catalog):
    # Cron custodians run global sweeps — the wrong shape for per-card
    # assignment (spec 042 §5). The set grows in the PR that names new
    # custodians; vault-morning is cron-FIRED but per-run scoped, which
    # is why it is catalogued and nothing here names it.
    custodians = {"review-loop-sweeper.sh"}
    for row in catalog.rows():
        basename = Path(row.source_path).name
        assert basename not in custodians, (
            f"row {row.key!r} cites the cron custodian {basename} — "
            "global sweeps are never per-card workflows"
        )
