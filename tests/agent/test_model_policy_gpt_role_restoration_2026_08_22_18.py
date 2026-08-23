"""Strict-TDD assertions for restoring the formal GPT role hierarchy in
runtime-registry model-policies.

Background
----------
The production registry was bumped to ``2026-08-22.16`` (PUBLISHED) and the
active preview authority worktree to ``2026-08-22.17`` (READY_FOR_REVIEW).
While GPT quota was throttled, ``complex-execution.primary`` was temporarily
set to ``minimax-cn/MiniMax-M3`` and a Tail ``teamo-router/<default>`` was
left in place. GPT quota has now recovered and the formal hierarchy must be
restored at version ``2026-08-22.18``:

* ``standard-reasoning``  primary  = GPT Luna  (soft: MiniMax-M3 + Teamo)
* ``complex-execution``   primary  = GPT Luna  (soft: GPT Terra)
* ``publishing``          primary  = GPT Terra (soft: GPT Luna)
* ``high-stakes-decision`` primary = GPT Sol   (soft: GPT Terra)
* ``routing-classifier`` / ``fast-economy``  unchanged
    (primary MiniMax-M3; soft Teamo tail; hard DeepSeek flash)
* ``independent-review``  unchanged  (fail_closed / no same family;
    MiniMax output reviewer = GPT Terra)

Multimodal extraction, local-deterministic, and image-generation must NOT
change.

These tests pin both the on-disk manifest files and the in-memory policy
ordering that ``route._candidate_ids`` derives from them. They are written
to FAIL on the current .17 / .16 snapshots and to PASS once the .18
restoration is applied to all three locations:

* ``~/.hermes/workflows/production-registry``
* ``authority/registry/``  (active preview worktree)
* ``tests/fixtures/runtime_registry/live``

The version bump from .17 -> .18 is also pinned, as is the production
registry staying in PUBLISHED while the authority + fixtures cycle back to
READY_FOR_REVIEW.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable

import pytest

from agent.route import _candidate_ids


WORKTREE = Path(__file__).resolve().parents[2]
AUTHORITY_REGISTRY = WORKTREE / "authority" / "registry"
LIVE_FIXTURE = WORKTREE / "tests" / "fixtures" / "runtime_registry" / "live"
PRODUCTION_REGISTRY = Path.home() / ".hermes" / "workflows" / "production-registry"

EXPECTED_VERSION = "2026-08-22.18"
PREVIOUS_AUTHORITY_VERSION = "2026-08-22.17"

# --- Formal hierarchy (post-recovery .18) ---------------------------------

FORMAL_POLICIES: dict[str, dict] = {
    "standard-reasoning": {
        "primary": "openai-codex/gpt-5.6-luna",
        "soft_failover": [
            "minimax-cn/MiniMax-M3",
            "teamo-router/<default>",
        ],
        "hard_failover": ["deepseek/deepseek-v4-flash"],
        # Exact ordered candidate list that route._candidate_ids produces.
        "candidates": [
            "openai-codex/gpt-5.6-luna",
            "minimax-cn/MiniMax-M3",
            "teamo-router/<default>",
            "deepseek/deepseek-v4-flash",
        ],
    },
    "complex-execution": {
        "primary": "openai-codex/gpt-5.6-luna",
        "soft_failover": ["openai-codex/gpt-5.6-terra"],
        "hard_failover": ["deepseek/deepseek-v4-pro"],
        # Teamo has been removed from the soft_failover tail; MiniMax must
        # not appear in this formal chain at all.
        "candidates": [
            "openai-codex/gpt-5.6-luna",
            "openai-codex/gpt-5.6-terra",
            "deepseek/deepseek-v4-pro",
        ],
        "excluded": ["minimax-cn/MiniMax-M3", "teamo-router/<default>"],
    },
    "publishing": {
        "primary": "openai-codex/gpt-5.6-terra",
        "soft_failover": ["openai-codex/gpt-5.6-luna"],
        "hard_failover": ["deepseek/deepseek-v4-pro"],
        "candidates": [
            "openai-codex/gpt-5.6-terra",
            "openai-codex/gpt-5.6-luna",
            "deepseek/deepseek-v4-pro",
        ],
    },
    "high-stakes-decision": {
        "primary": "openai-codex/gpt-5.6-sol",
        "soft_failover": ["openai-codex/gpt-5.6-terra"],
        "hard_failover": ["deepseek/deepseek-v4-pro"],
        "candidates": [
            "openai-codex/gpt-5.6-sol",
            "openai-codex/gpt-5.6-terra",
            "deepseek/deepseek-v4-pro",
        ],
    },
    "routing-classifier": {
        "primary_pool": ["minimax-cn/MiniMax-M3"],
        "soft_failover": [
            "openai-codex/gpt-5.6-luna",
            "teamo-router/<default>",
        ],
        "hard_failover": ["deepseek/deepseek-v4-flash"],
        "candidates": [
            "minimax-cn/MiniMax-M3",
            "openai-codex/gpt-5.6-luna",
            "teamo-router/<default>",
            "deepseek/deepseek-v4-flash",
        ],
    },
    "fast-economy": {
        "primary_pool": ["minimax-cn/MiniMax-M3"],
        "soft_failover": [
            "openai-codex/gpt-5.6-luna",
            "teamo-router/<default>",
        ],
        "hard_failover": ["deepseek/deepseek-v4-flash"],
        "candidates": [
            "minimax-cn/MiniMax-M3",
            "openai-codex/gpt-5.6-luna",
            "teamo-router/<default>",
            "deepseek/deepseek-v4-flash",
        ],
    },
}

# --- Independent-review invariants (unchanged across .17 -> .18) ------------

INDEPENDENT_REVIEW_RULES = {
    "minimax": ["openai-codex/gpt-5.6-terra"],
    "openai": ["minimax-cn/MiniMax-M3"],
    "google": ["openai-codex/gpt-5.6-sol"],
    "deepseek": ["openai-codex/gpt-5.6-sol"],
}


def _load_manifest(registry_dir: Path) -> dict:
    manifest_path = registry_dir / "manifest.json"
    if not manifest_path.exists():
        pytest.skip(f"registry not present: {registry_dir}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _load_policies(registry_dir: Path) -> dict:
    return json.loads(
        (registry_dir / "model-policies.json").read_text(encoding="utf-8")
    )


def _all_registries() -> Iterable[Path]:
    yield AUTHORITY_REGISTRY
    yield LIVE_FIXTURE
    yield PRODUCTION_REGISTRY


# ===========================================================================
# 1) Manifest version + promotion-state pinning
# ===========================================================================

@pytest.mark.parametrize(
    "registry_dir",
    [AUTHORITY_REGISTRY, LIVE_FIXTURE],
    ids=["authority", "live_fixture"],
)
def test_authority_and_fixture_versions_bumped_to_18(registry_dir: Path) -> None:
    manifest = _load_manifest(registry_dir)
    assert manifest["registryVersion"] == EXPECTED_VERSION, (
        f"{registry_dir}: expected registryVersion={EXPECTED_VERSION}; "
        f"got {manifest['registryVersion']!r}"
    )
    assert manifest["promotionState"] == "READY_FOR_REVIEW", (
        f"{registry_dir}: authority/fixtures must re-enter READY_FOR_REVIEW "
        f"after the GPT role restoration; got {manifest['promotionState']!r}"
    )


def test_production_registry_bumps_to_18_and_stays_published() -> None:
    manifest = _load_manifest(PRODUCTION_REGISTRY)
    assert manifest["registryVersion"] == EXPECTED_VERSION, (
        f"production registry must also bump to {EXPECTED_VERSION}; "
        f"got {manifest['registryVersion']!r}"
    )
    assert manifest["promotionState"] == "PUBLISHED", (
        "production registry must remain PUBLISHED — the .18 rollover is the "
        "new live hierarchy, not a candidate preview"
    )


# ===========================================================================
# 2) Every registry must declare the EXACT formal GPT role hierarchy
# ===========================================================================

@pytest.mark.parametrize(
    "registry_dir",
    list(_all_registries()),
    ids=["authority", "live_fixture", "production"],
)
def test_each_registry_pins_formal_gpt_role_hierarchy(registry_dir: Path) -> None:
    policies = _load_policies(registry_dir)["policies"]

    for role, expected in FORMAL_POLICIES.items():
        policy = policies.get(role)
        assert policy is not None, (
            f"{registry_dir}: role {role!r} is missing from model-policies.json"
        )

        # 1) Primary / pool identity — the strict-TDD anchor for "GPT is back"
        if "primary" in expected:
            assert policy["primary"] == expected["primary"], (
                f"{registry_dir}: {role}.primary must be "
                f"{expected['primary']!r}, got {policy['primary']!r} "
                "(the MiniMax-temp override has not been unwound)"
            )
        else:
            assert policy["primary_pool"] == expected["primary_pool"], (
                f"{registry_dir}: {role}.primary_pool must be "
                f"{expected['primary_pool']!r}, got {policy['primary_pool']!r}"
            )

        # 2) Soft / hard failover lists (order independent unless pinned)
        assert policy["soft_failover"] == expected["soft_failover"], (
            f"{registry_dir}: {role}.soft_failover must be "
            f"{expected['soft_failover']!r}, got {policy['soft_failover']!r}"
        )
        assert policy["hard_failover"] == expected["hard_failover"], (
            f"{registry_dir}: {role}.hard_failover must be "
            f"{expected['hard_failover']!r}, got {policy['hard_failover']!r}"
        )

        # 3) Ordered candidate chain that route._candidate_ids produces from
        #    the on-disk JSON — this is what runtime dispatch actually walks.
        actual = _candidate_ids(policy)
        assert actual == expected["candidates"], (
            f"{registry_dir}: {role} ordered candidates must be "
            f"{expected['candidates']!r}, got {actual!r}"
        )

        # 4) Forbidden-in-formal-chain members must be absent
        for forbidden in expected.get("excluded", ()):  # type: ignore[arg-type]
            assert forbidden not in actual, (
                f"{registry_dir}: {forbidden!r} must not appear in the "
                f"{role} chain after restoration; got {actual!r}"
            )


# ===========================================================================
# 3) Independent-review invariants (unchanged, but pinned across .17 -> .18)
# ===========================================================================

@pytest.mark.parametrize(
    "registry_dir",
    list(_all_registries()),
    ids=["authority", "live_fixture", "production"],
)
def test_independent_review_invariants_preserved(registry_dir: Path) -> None:
    review = _load_policies(registry_dir)["policies"]["independent-review"]

    assert review["degradation"]["strategy"] == "fail_closed_no_same_family"
    assert review["exclusion"] == "vendor_family"
    assert review["rules"]["minimax"] == INDEPENDENT_REVIEW_RULES["minimax"], (
        "MiniMax output reviewer must remain GPT Terra"
    )
    assert review["rules"]["openai"] == INDEPENDENT_REVIEW_RULES["openai"]
    assert review["rules"]["google"] == INDEPENDENT_REVIEW_RULES["google"]
    assert review["rules"]["deepseek"] == INDEPENDENT_REVIEW_RULES["deepseek"]


# ===========================================================================
# 4) Roles explicitly OUT OF SCOPE must not have been touched
# ===========================================================================

@pytest.mark.parametrize(
    "registry_dir",
    list(_all_registries()),
    ids=["authority", "live_fixture", "production"],
)
def test_multimodal_extraction_unchanged(registry_dir: Path) -> None:
    """The Gemini -> MiniMax -> GPT exception must be preserved verbatim."""
    policy = _load_policies(registry_dir)["policies"]["multimodal-extraction"]
    assert policy["primary"] == "bboluo/[L]gemini-3.1-pro-preview"
    assert policy["soft_failover"] == [
        "minimax-cn/MiniMax-M3",
        "openai-codex/gpt-5.6-terra",
        "openai-codex/gpt-5.6-sol",
    ]
    assert policy["hard_failover"] == []


@pytest.mark.parametrize(
    "registry_dir",
    list(_all_registries()),
    ids=["authority", "live_fixture", "production"],
)
def test_image_generation_unchanged(registry_dir: Path) -> None:
    """FAL catalog-only — image generation must not route through the
    reasoning chain."""
    policy = _load_policies(registry_dir)["policies"]["image-generation"]
    assert policy["primary_pool"] == [
        "fal/flux-2-pro",
        "fal/nano-banana-pro",
        "fal/gpt-image-2",
        "fal/recraft/v4/pro/text-to-image",
    ]
    assert policy["soft_failover"] == []
    assert policy["hard_failover"] == []


@pytest.mark.parametrize(
    "registry_dir",
    [PRODUCTION_REGISTRY],
    ids=["production"],
)
def test_local_deterministic_set_aside_decision_preserved(registry_dir: Path) -> None:
    """The ``2026-08-22.16`` set-aside of the ollama primary is a
    pre-existing policy decision that lives in the production registry only.
    It must NOT be reverted by the .18 GPT-role restoration."""
    policy = _load_policies(registry_dir)["policies"].get("local-deterministic")
    if policy is None:
        # Authority / fixtures strip it; only production carries the set-aside
        # policy. Nothing to pin here.
        return
    assert policy["primary"] == "ollama/qwen3-vl:2b"
    # The cloud-escalation chain added in .16 must remain in place.
    assert "minimax-cn/MiniMax-M3" in policy["soft_failover"]
    assert "openai-codex/gpt-5.6-luna" in policy["soft_failover"]


# ===========================================================================
# 5) Manifest sha256 integrity — every listed file's hash must match the
#    actual on-disk bytes (this is the structural pin for "registry is
#    coherent").
# ===========================================================================

@pytest.mark.parametrize(
    "registry_dir",
    [AUTHORITY_REGISTRY, LIVE_FIXTURE, PRODUCTION_REGISTRY],
    ids=["authority", "live_fixture", "production"],
)
def test_all_listed_manifest_hashes_match_disk(registry_dir: Path) -> None:
    manifest = _load_manifest(registry_dir)
    root = registry_dir
    for entry in manifest["files"]:
        path = root / entry["path"]
        assert path.exists(), (
            f"{registry_dir}: manifest lists {entry['path']} but it is missing"
        )
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == entry["sha256"], (
            f"{registry_dir}: sha256 mismatch for {entry['path']}; "
            f"manifest says {entry['sha256']}, disk says {actual}. "
            "Recompute the hash after editing the file."
        )


# ===========================================================================
# 6) live-fixture manifest must declare registryVersion .18 explicitly so the
#    downstream test_current_live_registry_fixture_loads_in_explicit_preview
#    test stays in sync.
# ===========================================================================

def test_live_fixture_declares_expected_version_to_test_runtime_registry() -> None:
    """The pre-existing fixture-loading test pins ``2026-08-22.17``. After
    the .18 bump, that test must be re-pointed to .18 by the restoration
    patch — this assertion enforces that re-pointing did not get forgotten.
    """
    from agent.runtime_registry import RegistryLoader

    snapshot = RegistryLoader(LIVE_FIXTURE).load(mode="preview")
    assert snapshot.registry_version == EXPECTED_VERSION
    assert snapshot.promotion_state == "READY_FOR_REVIEW"
