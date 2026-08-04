"""Tests for AgentCard capability routing (fleet/agentcard-routing, task t_02d89dde).

Covers the T5 acceptance evidence:

* T5.1 — the router loads ``cards/*.json`` at dispatch time and validates
  against ``agentcard.schema.json``; invalid cards are excluded (``CARD_INVALID``).
* T5.2 — stage 1 domain guard: guard bonus, forbidden removal, guard-table
  fallback by name (``GUARD_FALLBACK_BY_NAME``). "design a module" → arquiteto,
  "audit the auth module" → security, with zero hardcoded names in the routing
  path (the winner comes from the domain guard, not from a name in the title).
* T5.3 — stages 2-4 scoring/selection/fallback with the section-6 audit line,
  appended to ``routing-audit.log``.
* T5.4 — ``kanban_decompose`` routes a decomposed triage task by capability
  (LLM classifies, machine routes) and writes the audit line + board comment.
* T5.5 — ``kanban.cards_dir`` config resolution; missing dir falls back with
  ``AGENTCARD_REGISTRY_EMPTY``.

Fixture cards live in the fleet workspace
(``<hermes-root>/workspace/fleet/tools/fixtures/cards``) — the ready test
registry the design task handed off. Tests skip when the fleet workspace is
absent (CI without the fleet checkout).
"""

from __future__ import annotations

import json as jsonlib
import logging
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import agentcard_router as router
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_decompose as decomp

FLEET = Path(r"C:\Users\tneemo\AppData\Local\hermes\workspace\fleet")
FIXTURE_CARDS = FLEET / "tools" / "fixtures" / "cards"
FIXTURE_SCHEMA = FLEET / "agentcard.schema.json"

FIXTURE_PROFILES = [
    "arquiteto", "caretaker", "coder", "gestor",
    "inventor", "qa-browser", "researcher", "security",
]

requires_fleet = pytest.mark.skipif(
    not (FIXTURE_CARDS.is_dir() and FIXTURE_SCHEMA.is_file()),
    reason="fleet workspace with fixture cards not available",
)


def _load_fixture_registry(cards_dir: Path = FIXTURE_CARDS, schema: Path = FIXTURE_SCHEMA):
    return router.load_registry(cards_dir, schema)


@pytest.fixture
def fixture_registry():
    return _load_fixture_registry()


@pytest.fixture
def isolated_cards(tmp_path):
    """Copy fixture cards + schema into a tmp dir (hermetic audit-log writes)."""
    cards_dir = tmp_path / "cards"
    shutil.copytree(FIXTURE_CARDS, cards_dir)
    shutil.copy2(FIXTURE_SCHEMA, tmp_path / "agentcard.schema.json")
    return cards_dir, tmp_path


# ---------------------------------------------------------------------------
# T5.1 — registry load + validation
# ---------------------------------------------------------------------------

@requires_fleet
def test_load_registry_all_fixture_cards_valid(fixture_registry):
    result = fixture_registry
    assert set(result.cards) == set(FIXTURE_PROFILES)
    assert result.warnings == []
    # Registry invariant: every capability id is declared by exactly one card.
    ids = [
        cap["id"]
        for card in result.cards.values()
        for cap in card["capabilities"]
    ]
    assert len(ids) == len(set(ids))


@requires_fleet
def test_load_registry_excludes_invalid_card_with_card_invalid(fixture_registry, tmp_path):
    cards_dir = tmp_path / "cards"
    shutil.copytree(FIXTURE_CARDS, cards_dir)
    shutil.copy2(FIXTURE_SCHEMA, tmp_path / "agentcard.schema.json")
    # Mutate one card: drop a required field (domain_boundaries).
    broken = jsonlib.loads((cards_dir / "coder.json").read_text(encoding="utf-8"))
    del broken["domain_boundaries"]
    (cards_dir / "coder.json").write_text(
        jsonlib.dumps(broken), encoding="utf-8"
    )

    result = router.load_registry(cards_dir, tmp_path / "agentcard.schema.json")

    assert "coder" not in result.cards
    assert len(result.cards) == len(FIXTURE_PROFILES) - 1
    assert any("CARD_INVALID" in w and "coder.json" in w for w in result.warnings)


@requires_fleet
def test_load_registry_duplicate_capability_id_excludes_both(tmp_path):
    base = jsonlib.loads((FIXTURE_CARDS / "coder.json").read_text(encoding="utf-8"))
    twin = jsonlib.loads((FIXTURE_CARDS / "arquiteto.json").read_text(encoding="utf-8"))
    twin["profile"] = "twin"
    twin["capabilities"][0]["id"] = "implementation"  # duplicate coder's id

    cards_dir = tmp_path / "cards"
    cards_dir.mkdir()
    shutil.copy2(FIXTURE_SCHEMA, tmp_path / "agentcard.schema.json")
    (cards_dir / "coder.json").write_text(jsonlib.dumps(base), encoding="utf-8")
    (cards_dir / "twin.json").write_text(jsonlib.dumps(twin), encoding="utf-8")

    result = router.load_registry(cards_dir, tmp_path / "agentcard.schema.json")

    assert result.cards == {}
    assert any(
        "CARD_INVALID" in w and "duplicate id" in w and "implementation" in w
        for w in result.warnings
    )


# ---------------------------------------------------------------------------
# T5.2 — stage 1 domain guard
# ---------------------------------------------------------------------------

@requires_fleet
def test_domain_guard_routes_design_to_arquiteto(fixture_registry):
    routed = router.route_task(
        {
            "task_id": "t_guard_a",
            "title": "Design a module",
            "primary_domain": "design",
            "requires_capabilities": [],
        },
        fixture_registry.cards,
        default_assignee="gestor",
    )
    winner, audit = routed.winner, routed.audit
    assert winner == "arquiteto"
    arquiteto = next(c for c in audit["candidates"] if c["profile"] == "arquiteto")
    assert arquiteto["guard"] is True
    assert arquiteto["score"] == "inf"
    assert audit["fallback_used"] is None


@requires_fleet
def test_domain_guard_beats_keyword_no_hardcoded_names(fixture_registry):
    # The task's body is full of 'design' keywords that would tempt arquiteto,
    # but the primary domain is security: the guard wins and coder is REMOVED
    # (forbidden) even though 'auth'/'module' keywords would score for it.
    routed = router.route_task(
        {
            "task_id": "t_guard_b",
            "title": "Audit the auth module",
            "body": "review the design of the module boundaries first",
            "primary_domain": "security",
            "requires_capabilities": [],
        },
        fixture_registry.cards,
        default_assignee="gestor",
    )
    winner, audit = routed.winner, routed.audit
    assert winner == "security"
    assert audit["winner"] == "security"
    security = next(c for c in audit["candidates"] if c["profile"] == "security")
    assert security["guard"] is True
    # coder is forbidden from security — never a candidate, never the winner.
    assert all(c["profile"] != "coder" for c in audit["candidates"])
    # No hardcoded name in the routing path: the title says 'audit', and the
    # winner was chosen by the domain guard, not by matching the task to a
    # profile's name/description.
    assert winner == router.GUARD_TABLE["security"]


@requires_fleet
def test_guard_fallback_by_name_when_owner_card_missing(fixture_registry, tmp_path):
    cards_dir = tmp_path / "cards"
    shutil.copytree(FIXTURE_CARDS, cards_dir)
    shutil.copy2(FIXTURE_SCHEMA, tmp_path / "agentcard.schema.json")
    (cards_dir / "arquiteto.json").unlink()  # design owner card disappears

    result = router.load_registry(cards_dir, tmp_path / "agentcard.schema.json")
    routed = router.route_task(
        {
            "task_id": "t_guard_c",
            "title": "Design a module",
            "primary_domain": "design",
            "requires_capabilities": [],
        },
        result.cards,
        default_assignee="gestor",
    )
    winner, audit = routed.winner, routed.audit
    # The guard table still routes by name — domain boundaries are not
    # sacrificed for registry convenience (the Bolsotron of routing).
    assert winner == "arquiteto"
    assert audit["fallback_used"] == "GUARD_FALLBACK_BY_NAME"


# ---------------------------------------------------------------------------
# T5.3 — stages 2-4 scoring/selection/fallback + audit line
# ---------------------------------------------------------------------------

@requires_fleet
def test_explicit_capability_match_routes_and_audits(fixture_registry):
    routed = router.route_task(
        {
            "task_id": "t_score_a",
            "title": "Design AgentCard schema and routing rules",
            "body": "",
            "requires_capabilities": ["architecture-design"],
        },
        fixture_registry.cards,
        default_assignee="gestor",
    )
    winner, audit = routed.winner, routed.audit
    assert winner == "arquiteto"
    assert audit["matched_capability_ids"] == ["architecture-design"]
    # Section-6 normative fields are all present.
    for key in (
        "ts", "task_id", "title", "primary_domain",
        "requires_capabilities_declared", "requires_capabilities_inferred",
        "candidates", "winner", "matched_capability_ids",
        "fallback_used", "stage",
    ):
        assert key in audit, f"audit missing normative field {key}"
    assert audit["primary_domain"] == "design"  # resolved from capability
    assert audit["fallback_used"] is None
    # Guard bonus present: arquiteto owns design, so it also carries +inf.
    arquiteto = next(c for c in audit["candidates"] if c["profile"] == "arquiteto")
    assert arquiteto["guard"] is True


@requires_fleet
def test_capability_unknown_routes_to_default(fixture_registry):
    routed = router.route_task(
        {
            "task_id": "t_fb_a",
            "title": "Rewrite the orchestrator in COBOL",
            "body": "",
            "requires_capabilities": ["cobol-migration"],
        },
        fixture_registry.cards,
        default_assignee="gestor",
    )
    winner, audit = routed.winner, routed.audit
    assert winner == "gestor"
    assert audit["fallback_used"] == "CAPABILITY_UNKNOWN"
    assert audit["unknown_ids"] == ["cobol-migration"]


@requires_fleet
def test_no_candidate_routes_to_default(fixture_registry):
    routed = router.route_task(
        {
            "task_id": "t_fb_b",
            "title": "ZZZ utterly unrelated topic",
            "body": "qwerty asdfgh",
            "primary_domain": "docs",  # open domain, no guard
            "requires_capabilities": [],
        },
        fixture_registry.cards,
        default_assignee="gestor",
    )
    winner, audit = routed.winner, routed.audit
    assert winner == "gestor"
    assert audit["fallback_used"] == "NO_CANDIDATE"


@requires_fleet
def test_audit_line_appends_to_routing_audit_log(fixture_registry, tmp_path):
    log_path = tmp_path / "routing-audit.log"
    routed = router.route_task(
        {
            "task_id": "t_log_a",
            "title": "Design a module",
            "primary_domain": "design",
            "requires_capabilities": [],
        },
        fixture_registry.cards,
        default_assignee="gestor",
    )
    audit = routed.audit
    router.append_audit_line(audit, log_path)
    router.append_audit_line(audit, log_path)  # append-only, never truncate

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    parsed = jsonlib.loads(lines[0])
    assert parsed["winner"] == "arquiteto"
    assert parsed["task_id"] == "t_log_a"
    assert parsed["candidates"][0]["guard"] is True


# ---------------------------------------------------------------------------
# T5.4 — decompose routes by capability end-to-end
# ---------------------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_aux_response(content: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def _patch_aux_client(content: str):
    return patch(
        "agent.auxiliary_client.call_llm",
        return_value=_fake_aux_response(content),
    )


def _patch_list_profiles(names: list[str]):
    fake_profiles = [
        SimpleNamespace(
            name=n, is_default=(i == 0), description=f"desc for {n}",
            description_auto=False, model="m", provider="p", skill_count=1,
        )
        for i, n in enumerate(names)
    ]
    return [
        patch("hermes_cli.profiles.list_profiles", return_value=fake_profiles),
        patch("hermes_cli.profiles.profile_exists", side_effect=lambda x: x in names),
        patch("hermes_cli.profiles.get_active_profile_name", return_value=names[0] if names else "default"),
    ]


@requires_fleet
def test_decompose_routes_children_by_capability(kanban_home, isolated_cards):
    cards_dir, tmp_path = isolated_cards
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ship the capability routing feature", triage=True)

    # The LLM CLASSIFIES (primary_domain + requires_capabilities) and emits no
    # assignee — the machine routes from the cards.
    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "tasks": [
            {
                "title": "Design the module boundaries",
                "body": "write the spec",
                "primary_domain": "design",
                "requires_capabilities": ["architecture-design"],
                "parents": [],
            },
            {
                "title": "Implement the feature",
                "body": "code it",
                "primary_domain": "implementation",
                "requires_capabilities": ["implementation"],
                "parents": [0],
            },
        ],
    })

    patches = _patch_list_profiles(FIXTURE_PROFILES + ["gestor"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"cards_dir": str(cards_dir)}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is True
    with kb.connect() as conn:
        c0 = kb.get_task(conn, outcome.child_ids[0])
        c1 = kb.get_task(conn, outcome.child_ids[1])
    # Routed by capability match, not by a hardcoded name.
    assert c0.assignee == "arquiteto"
    assert c1.assignee == "coder"

    # Section 6: routing-audit.log carries one JSON line per routed child.
    log_path = tmp_path / "routing-audit.log"
    assert log_path.is_file(), "routing-audit.log was not written"
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = jsonlib.loads(lines[0])
    assert first["winner"] == "arquiteto"
    assert first["matched_capability_ids"] == ["architecture-design"]
    assert first["primary_domain"] == "design"
    second = jsonlib.loads(lines[1])
    assert second["winner"] == "coder"

    # The board task carries the one-line decision comment.
    with kb.connect() as conn:
        comments = kb.list_comments(conn, tid)
    assert comments, "no routing comment on the board task"
    assert "capability routing" in comments[-1].body
    assert "arquiteto" in comments[-1].body
    assert "architecture-design" in comments[-1].body


# ---------------------------------------------------------------------------
# T5.5 — cards_dir config resolution + AGENTCARD_REGISTRY_EMPTY fallback
# ---------------------------------------------------------------------------

def test_resolve_cards_dir_config_override(tmp_path):
    cfg = {"kanban": {"cards_dir": str(tmp_path / "custom-cards")}}
    assert decomp._resolve_cards_dir(cfg) == tmp_path / "custom-cards"


def test_resolve_cards_dir_defaults_to_fleet(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    assert decomp._resolve_cards_dir({}) == (
        Path(tmp_path / ".hermes") / "workspace" / "fleet" / "cards"
    )


@requires_fleet
def test_decompose_falls_back_when_cards_dir_missing(
    kanban_home, caplog, tmp_path,
):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="route me the legacy way", triage=True)

    missing = tmp_path / "no-such-cards"
    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "legacy",
        "tasks": [
            {
                "title": "legacy child",
                "body": "done",
                "assignee": "engineer",  # legacy override still honored
                "parents": [],
            },
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "engineer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"cards_dir": str(missing)}},
        ), caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_decompose"):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        child = kb.get_task(conn, outcome.child_ids[0])
    assert child.assignee == "engineer"
    assert any("AGENTCARD_REGISTRY_EMPTY" in r.message for r in caplog.records)
