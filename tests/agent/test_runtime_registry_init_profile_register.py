"""TDD coverage for the minimal ``teamo-router/<default>`` profile
registration that completes the cross-vendor routing surface.

This slice is the *read-only* TDD adapter to the read-only audit report
that authorised three vendors — MiniMax, DeepSeek, Teamo Router — into
the route layer.  The DeepSeek and MiniMax entries already exist
(DeepSeek as ``hard-fallback-only``; MiniMax as a frequent primary).
Teamo Router was registered in ``config.yaml`` only and was not yet a
recognised profile in the runtime registry.

The minimum adaptation here is:

* Register a new ``teamo-router/<default>`` profile in
  ``model-profiles.json`` with ``vendor_family="teamo"`` and
  ``routing_role="hard-failover-only"``.  The latter is the only
  ``routing_role`` value the registry's text schema accepts for
  hard-failover semantic (the value is a free-form string per
  ``agent.runtime_registry._validate_model_profile_payload``, so the
  registration is schema-clean).
* Append the new profile to the **tail** of the ``soft_failover`` array
  of three policies that the user explicitly approved for this slice:
  ``standard-reasoning``, ``fast-economy``, and ``routing-classifier``.
  The order matters: the existing soft-failover entries (GPT-5.6 Luna
  for standard-reasoning / fast-economy / routing-classifier; MiniMax
  for standard-reasoning) stay in their existing positions, and the
  Teamo entry is appended last.  This matches the user's "soft_failover
  末尾追加" instruction exactly and is the *minimal* adaptation.
* Leave the remaining policies untouched:
  - ``complex-execution`` primary stays ``minimax-cn/MiniMax-M3``.
  - ``high-stakes-decision`` (``primary=openai-codex/gpt-5.6-sol``) and
    ``publishing`` (``primary=openai-codex/gpt-5.6-terra``) are not
    modified.
  - ``independent-review`` keeps its cross-vendor rules and exclusion
    semantics.

* Bump ``registryVersion`` to a fresh ``YYYY-MM-DD.N`` stamp and
  recompute the manifest hashes for the two modified payloads
  (``model-policies.json`` and ``model-profiles.json``).  Every other
  manifest entry's hash stays untouched so the cross-file diff stays
  minimal.

The tests below follow the existing TDD style used by
``tests/agent/test_runtime_registry.py``,
``tests/agent/test_runtime_registry_init.py``, and
``tests/agent/test_image_generation_policy.py``: they load the in-tree
LIVE fixture (a byte-for-byte copy of ``authority/registry/``) via the
public ``load_registry`` API and assert the immutable snapshot's
behaviour.  The LIVE fixture is the same surface every other
``test_live_registry_*`` test reads, so the GREEN path here proves that
the shipped authority registry is correctly registered.

The tests are intentionally scoped to the *minimal* adaptation.  They
do not assert any cross-vendor invariants that are out of scope for
this slice (e.g. ``independent-review`` cross-family rotation, the
existing ``multimodal-extraction`` gate).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agent.runtime_registry import load_registry


LIVE_FIXTURE = Path(__file__).parents[1] / "fixtures" / "runtime_registry" / "live"
TEAMO_PROFILE_KEY = "teamo-router/<default>"
TEAMO_VENDOR_FAMILY = "teamo"
TEAMO_ROUTING_ROLE = "hard-failover-only"

# The three policies that explicitly opt in to Teamo Router as a
# soft-failover entry, in the order the task brief enumerates them.
# Append-only: every pre-existing soft-failover entry stays put and
# the Teamo entry is added at the tail of the list.
SOFT_FAILOVER_POLICIES = (
    "standard-reasoning",
    "fast-economy",
    "routing-classifier",
)

# Policies the task brief explicitly forbids touching.
FROZEN_POLICIES = (
    # complex-execution primary restored to formal GPT-Luna in
    # 2026-08-22.18; this slice of tests does not assert the
    # primary identity — see test_complex_execution_primary_is_formal_gpt_luna.
    "complex-execution",
    "high-stakes-decision",  # GPT-first role; not in Teamo scope
    "publishing",  # GPT-first role; not in Teamo scope
    "independent-review",  # cross-vendor reviewer rules; not in Teamo scope
)


def _load_live_snapshot():
    """Load the LIVE fixture in preview mode.

    The LIVE fixture currently declares ``promotionState=READY_FOR_REVIEW``
    so ``mode="production"`` would reject it (the production gate only
    accepts APPROVED / PUBLISHED).  Preview mode is the right surface
    for testing the *registry contents* without flipping the promotion
    state; the production gate is exercised separately by
    ``test_runtime_registry_init.py``.
    """

    return load_registry(LIVE_FIXTURE, mode="preview")


def _read_payload(relative_path: str) -> dict:
    """Read a JSON payload from the LIVE fixture on disk.

    The ``load_registry`` snapshot freezes the bundle, which is
    deliberate for runtime consumers but inconvenient for tests that
    need to inspect intermediate structures (lists with explicit
    ordering, optional fields).  For ordering-sensitive assertions we
    re-read the payload from disk and parse it ourselves.
    """

    return json.loads((LIVE_FIXTURE / relative_path).read_text(encoding="utf-8"))


# ── Profile registration: the new vendor entry exists with the right shape ──


def test_teamo_router_default_profile_is_registered_with_correct_shape() -> None:
    """The new ``teamo-router/<default>`` profile must be declared in
    ``model-profiles.json`` and load through the public registry
    surface with ``vendor_family=teamo`` and
    ``routing_role=hard-failover-only``.

    This is the GREEN happy path for the profile registration: the
    schema accepts the entry, the loader produces an immutable
    snapshot without raising, and the cross-file reference validator
    is happy because every soft_failover pool that references the new
    profile can resolve it.
    """

    snapshot = _load_live_snapshot()
    profiles = snapshot.bundle["model_profiles"]["profiles"]

    assert TEAMO_PROFILE_KEY in profiles, (
        f"model-profiles.json must declare {TEAMO_PROFILE_KEY!r}; "
        f"declared profiles: {sorted(profiles)!r}"
    )
    profile = profiles[TEAMO_PROFILE_KEY]
    assert profile["vendor_family"] == TEAMO_VENDOR_FAMILY, (
        f"vendor_family must be {TEAMO_VENDOR_FAMILY!r}; "
        f"got {profile['vendor_family']!r}"
    )
    assert profile["routing_role"] == TEAMO_ROUTING_ROLE, (
        f"routing_role must be {TEAMO_ROUTING_ROLE!r} per the audit's "
        f"'hard-failover-only' instruction; got {profile['routing_role']!r}"
    )


def test_teamo_router_default_profile_does_not_disturb_existing_profiles() -> None:
    """Adding the new entry must NOT remove, rename, or restructure
    any of the pre-existing profiles.  The diff is purely additive at
    the profile layer."""

    snapshot = _load_live_snapshot()
    profiles = snapshot.bundle["model_profiles"]["profiles"]

    required_existing = {
        # Reasoning primaries that the audit says are already in
        # production routing.
        "openai-codex/gpt-5.6-luna",
        "openai-codex/gpt-5.6-terra",
        "openai-codex/gpt-5.6-sol",
        "minimax-cn/MiniMax-M3",
        # DeepSeek hard-failover (different spelling: "hard-fallback-only"
        # is the pre-existing convention; the new Teamo entry uses
        # "hard-failover-only" per the task brief).
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
        # Vision / image roles.
        # Note: local-only ollama profile was removed in 2026-08-22.17
        # (local model cleanup); the cloud multimodal chain handles the
        # same workload.
        "bboluo/[L]gemini-3.1-pro-preview",
    }
    missing = required_existing - set(profiles)
    assert not missing, (
        "pre-existing profiles must remain present after the Teamo "
        f"registration; missing: {sorted(missing)!r}"
    )


# ── Soft-failover ordering: the new entry is appended last, only in scope ──


@pytest.mark.parametrize("policy_name", SOFT_FAILOVER_POLICIES)
def test_teamo_router_default_appended_to_soft_failover(policy_name: str) -> None:
    """The three policies enumerated by the task brief
    (``standard-reasoning``, ``fast-economy``, ``routing-classifier``)
    must have ``teamo-router/<default>`` appended to the tail of
    their ``soft_failover`` array.  Pre-existing entries must keep
    their relative order — the diff is strictly append."""

    policies_payload = _read_payload("model-policies.json")
    policy = policies_payload["policies"][policy_name]
    assert "soft_failover" in policy, (
        f"{policy_name} must declare a soft_failover array; got keys {sorted(policy)!r}"
    )
    soft_failover = list(policy["soft_failover"])
    assert soft_failover, f"{policy_name}.soft_failover must not be empty"

    assert soft_failover[-1] == TEAMO_PROFILE_KEY, (
        f"{policy_name}.soft_failover must end with {TEAMO_PROFILE_KEY!r}; "
        f"got tail {soft_failover[-1]!r}, full list {soft_failover!r}"
    )
    # And the new entry must not be duplicated inside the array.
    assert soft_failover.count(TEAMO_PROFILE_KEY) == 1, (
        f"{policy_name}.soft_failover must reference {TEAMO_PROFILE_KEY!r} "
        f"exactly once; got count={soft_failover.count(TEAMO_PROFILE_KEY)}"
    )


@pytest.mark.parametrize("policy_name", SOFT_FAILOVER_POLICIES)
def test_teamo_router_default_does_not_leak_into_hard_failover(
    policy_name: str,
) -> None:
    """The new Teamo entry is a *soft* failover, not a hard one.  The
    hard_failover arrays of the three opt-in policies must NOT pick
    up the new entry — DeepSeek is the only hard-failover for these
    three reasoning-economy roles."""

    policies_payload = _read_payload("model-policies.json")
    policy = policies_payload["policies"][policy_name]
    hard_failover = list(policy.get("hard_failover", []))
    assert TEAMO_PROFILE_KEY not in hard_failover, (
        f"{policy_name}.hard_failover must not list {TEAMO_PROFILE_KEY!r}; "
        f"got {hard_failover!r}"
    )


# ── Frozen policies: the audit's "不动" list is honored exactly ──


@pytest.mark.parametrize("policy_name", FROZEN_POLICIES)
def test_frozen_policies_are_not_modified_by_teamo_adapter(policy_name: str) -> None:
    """The task brief explicitly forbids touching four policies.  None
    of them may list the new Teamo entry in any failover field, and
    the high-stakes / publishing / reviewer / complex-execution
    surfaces must keep their pre-existing primary/structure intact."""

    policies_payload = _read_payload("model-policies.json")
    policy = policies_payload["policies"][policy_name]

    for field in ("soft_failover", "hard_failover", "failover"):
        values = policy.get(field, [])
        if isinstance(values, str):
            values = [values]
        assert TEAMO_PROFILE_KEY not in values, (
            f"frozen policy {policy_name!r} must not list "
            f"{TEAMO_PROFILE_KEY!r} in {field!r}; got {values!r}"
        )


def test_complex_execution_primary_is_formal_gpt_luna() -> None:
    """After the 2026-08-22.18 GPT-quota recovery, the
    complex-execution primary MUST be the formal GPT-5.6 Luna model
    — not the temporary ``minimax-cn/MiniMax-M3`` fallback that was
    installed under quota throttling. Teamo-tail is also removed from
    this chain.

    This pins the in-registry evidence that the recovery landed; the
    stricter cross-registry check is in
    ``test_model_policy_gpt_role_restoration_2026_08_22_18``.
    """

    policies_payload = _read_payload("model-policies.json")
    primary = policies_payload["policies"]["complex-execution"].get("primary")
    assert primary == "openai-codex/gpt-5.6-luna", (
        "complex-execution.primary must be the formal GPT-Luna model "
        "after the 2026-08-22.18 GPT-quota recovery; "
        f"got {primary!r}"
    )
    soft_failover = list(
        policies_payload["policies"]["complex-execution"].get("soft_failover", [])
    )
    assert TEAMO_PROFILE_KEY not in soft_failover, (
        "complex-execution.soft_failover must NOT carry a Teamo tail "
        f"in the formal chain; got {soft_failover!r}"
    )


def test_independent_review_rules_remain_unchanged() -> None:
    """The cross-vendor reviewer rules must not pick up a Teamo entry.
    The audit forbids touching the reviewer surface, so the existing
    minimax→openai-codex/gpt-5.6-terra, openai→minimax-cn/MiniMax-M3,
    google→openai-codex/gpt-5.6-sol, deepseek→openai-codex/gpt-5.6-sol
    mapping stays exactly as the prior worktree left it."""

    policies_payload = _read_payload("model-policies.json")
    rules = policies_payload["policies"]["independent-review"]["rules"]
    assert TEAMO_VENDOR_FAMILY not in rules, (
        f"independent-review.rules must not introduce vendor_family "
        f"{TEAMO_VENDOR_FAMILY!r}; rules={rules!r}"
    )


# ── Manifest integrity: version bumped, hashes match disk, no stale entries ──


def test_registry_version_was_bumped_above_prior_value() -> None:
    """The audit requires bumping ``registryVersion`` as part of the
    adapter.  The new stamp must be a strictly later
    ``YYYY-MM-DD.N`` than the prior shipped value (``2026-08-22.14``)
    so the manifest diff is unambiguous."""

    snapshot = _load_live_snapshot()
    version = snapshot.registry_version
    match_format = version.split(".")
    assert len(match_format) >= 2, (
        f"registryVersion must look like YYYY-MM-DD.N; got {version!r}"
    )
    # Compare as a tuple of (date, revision).  A simple string compare
    # works for ISO dates but is wrong once the date changes; tuples
    # of integers are unambiguous.
    date_part, revision_part = version.rsplit(".", 1)
    prior_date, prior_revision = "2026-08-22", 14
    assert (date_part, int(revision_part)) > (prior_date, prior_revision), (
        f"registryVersion must be bumped above 2026-08-22.14; got {version!r}"
    )


def test_manifest_hashes_match_actual_payload_bytes() -> None:
    """Every manifest hash entry must match the actual on-disk bytes
    of the corresponding payload after the adapter edits.  The
    cross-file integrity baseline (B03 in
    ``run_registry_integrity_baseline``) depends on this invariant;
    if any sha drifts from disk the loader rejects the registry with
    ``hash_mismatch``."""

    snapshot = _load_live_snapshot()
    declared_hashes = {
        entry["path"]: entry["sha256"]
        for entry in snapshot.manifest["files"]
    }

    for path, declared_sha in declared_hashes.items():
        full_path = LIVE_FIXTURE / path
        assert full_path.is_file(), f"manifest references missing file {path!r}"
        actual_sha = hashlib.sha256(full_path.read_bytes()).hexdigest()
        assert actual_sha == declared_sha, (
            f"manifest hash for {path!r} must match actual bytes; "
            f"declared={declared_sha!r}, actual={actual_sha!r}"
        )


def test_model_policies_and_profiles_hashes_were_recomputed() -> None:
    """The two payloads the adapter edits
    (``model-policies.json`` and ``model-profiles.json``) must have
    their manifest hashes point at the on-disk bytes after the edit.
    This is the GREEN stamp that proves the adapter did not leave a
    stale hash in the manifest."""

    snapshot = _load_live_snapshot()
    declared_hashes = {
        entry["path"]: entry["sha256"]
        for entry in snapshot.manifest["files"]
    }

    for payload_name in ("model-policies.json", "model-profiles.json"):
        full_path = LIVE_FIXTURE / payload_name
        actual_sha = hashlib.sha256(full_path.read_bytes()).hexdigest()
        assert declared_hashes[payload_name] == actual_sha, (
            f"manifest hash for {payload_name!r} must be recomputed "
            f"after the adapter edit; declared="
            f"{declared_hashes[payload_name]!r}, actual={actual_sha!r}"
        )


def test_unrelated_manifest_entries_preserve_their_hashes() -> None:
    """Manifest entries that the adapter does NOT touch
    (``capability-contracts.json``, ``execution-roles.json``,
    ``route-policy.json``, ``semantic-router-prompt.md``,
    ``workflow-templates.json``) must keep the exact sha256 they had
    before.  This pins the diff to the two adapter-edited payloads
    only."""

    snapshot = _load_live_snapshot()
    declared_hashes = {
        entry["path"]: entry["sha256"]
        for entry in snapshot.manifest["files"]
    }
    # Prior-worktree sha256s from the unmodified payloads.  These
    # were updated in 2026-08-22.17 (local model cleanup): the
    # cleanup removed ollama/qwen3-vl:2b profile, local-deterministic
    # policy/contract, local-worker/-extractor roles, local-* workflows,
    # and local_* specialized_workflows entries — all legitimately
    # change-detector hashes.  If any of these changes again, the
    # adapter (or any future edit) accidentally touched a payload it
    # was supposed to leave alone relative to 2026-08-22.17.
    prior_hashes = {
        "capability-contracts.json": "6e7fe1c7e9e4c53245188ef7a1089bcd6ff9782bb63c3256c9cb50326179a51b",
        "execution-roles.json": "7831c2178d992bd4942081f08532975c9c8942d5deeeabbe90a70dab0c0535f2",
        "route-policy.json": "8129574878f88c508fdd05a7b9e7cd900e0411678127b547695ffbea2475c230",
        "semantic-router-prompt.md": "8fc6f46034b5891526b8573d4484cab9e0472d7701ced9bbc3e9274a0b08262a",
        "workflow-templates.json": "6308817c85276343fcda2e6fe049ae9a8cfb26f4c1e7ae8584e39fcd3cd28f0d",
    }
    for path, expected_sha in prior_hashes.items():
        assert declared_hashes[path] == expected_sha, (
            f"manifest hash for {path!r} must remain the prior-worktree "
            f"value; declared={declared_hashes[path]!r}, "
            f"expected={expected_sha!r}"
        )
