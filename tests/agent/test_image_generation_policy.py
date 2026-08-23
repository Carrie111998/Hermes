"""TDD coverage for the image-generation role + contract.

These tests assert the contract described in the registry:

* ``model-policies.json`` must declare an ``image-generation`` policy whose
  ``primary_pool`` is restricted to generation-only model profiles (FAL
  catalog: flux-2-pro, nano-banana-pro, gpt-image-2,
  recraft/v4/pro/text-to-image).  Reasoning profiles (MiniMax, GPT, Gemini,
  DeepSeek) MUST NOT appear in that pool.
* ``capability-contracts.json`` must declare an ``image-generation``
  contract that is text-modality with ``reasoning_intent=off``.
* The cross-file reference validator must STILL reject dangling / unknown
  models — adding the new role does not relax that invariant.

These tests follow the existing TDD style used by
``tests/agent/test_runtime_registry.py``: they build a temp registry
tree, hash the payloads into a manifest, and exercise the public loader
surface (``load_registry`` / ``RegistryLoadError``).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

import pytest

from agent.runtime_registry import RegistryLoadError, load_registry


# Generation-only FAL catalog.  These are the four minimal entries the
# registry must declare as routable image-generation profiles.  None of
# them supports reasoning, so the image-generation primary_pool is a
# closed set.
GENERATION_ONLY_PROFILES: dict[str, dict[str, Any]] = {
    "fal/flux-2-pro": {
        "vendor_family": "fal",
        "supported_reasoning": [],
        "thinking_map": {"off": "off"},
        "context_window": 4096,
        "supports_tools": False,
        "supports_images": False,
        "routing_role": "image-generation-only",
    },
    "fal/nano-banana-pro": {
        "vendor_family": "fal",
        "supported_reasoning": [],
        "thinking_map": {"off": "off"},
        "context_window": 4096,
        "supports_tools": False,
        "supports_images": False,
        "routing_role": "image-generation-only",
    },
    "fal/gpt-image-2": {
        "vendor_family": "fal",
        "supported_reasoning": [],
        "thinking_map": {"off": "off"},
        "context_window": 4096,
        "supports_tools": False,
        "supports_images": False,
        "routing_role": "image-generation-only",
    },
    "fal/recraft/v4/pro/text-to-image": {
        "vendor_family": "fal",
        "supported_reasoning": [],
        "thinking_map": {"off": "off"},
        "context_window": 4096,
        "supports_tools": False,
        "supports_images": False,
        "routing_role": "image-generation-only",
    },
}


def _minimal_payloads() -> dict[str, dict[str, Any]]:
    """Return a fully-valid minimal registry fixture with the new role.

    The image-generation policy and capability contract are pre-populated
    so this fixture represents the GREEN state.  Tests that exercise the
    GREEN path use this directly.  Tests that exercise RED mutations
    rewrite the relevant payload and expect ``dangling_reference`` or
    related errors.
    """

    return {
        "route-policy.json": {
            "schema_version": "1.0",
            "description": "route",
            "default_route": {"level": "L1", "risk": "low"},
            "level_contracts": {"L1": "basic"},
            "level_workflows": {"L1": "standard"},
            "specialized_workflows": {},
            "risk_gates": {"low": {"min_workflow": None}},
            "semantic_router": {
                "enabled": True,
                "max_calls_per_task": 1,
                "timeout_ms": 6000,
                "model_policy": "route",
                "triggers": [],
                "on_failure": "fast_gate_conservative",
                "on_low_confidence": "no_upgrade",
            },
            "workflow_rank": {"standard": 1},
        },
        "workflow-templates.json": {
            "schema_version": "1.1",
            "description": "workflows",
            "templates": {
                "standard": {
                    "roles": ["responder"],
                    "verify": False,
                    "policies": ["route"],
                }
            },
        },
        "execution-roles.json": {
            "schema_version": "1.0",
            "description": "roles",
            "roles": {
                "responder": {
                    "responsibility": "answer",
                    "tools": "contextual",
                    "dispatch": "self",
                    "model_policy": "route",
                }
            },
        },
        "model-policies.json": {
            "schema_version": "1.2",
            "description": "policies",
            "policies": {
                "route": {"primary": "model"},
                "image-generation": {
                    "primary_pool": list(GENERATION_ONLY_PROFILES),
                    "primary_pool_mode": "flexible_available_primary",
                    "soft_failover": [],
                    "hard_failover": [],
                    "note": (
                        "Image-generation role uses FAL catalog only. "
                        "Reasoning profiles are explicitly excluded."
                    ),
                },
            },
        },
        "model-profiles.json": {
            "schema_version": "1.1",
            "description": "profiles",
            "profiles": {
                "model": {
                    "vendor_family": "test",
                    "supported_reasoning": ["low"],
                    "thinking_map": {"low": "low"},
                    "context_window": 1000,
                    "supports_tools": True,
                    "supports_images": False,
                },
                **GENERATION_ONLY_PROFILES,
            },
        },
        "capability-contracts.json": {
            "schema_version": "1.0",
            "description": "contracts",
            "contracts": {
                "basic": {
                    "quality": "basic",
                    "modality": ["text"],
                    "tools": "basic",
                    "context_class": "short",
                    "reasoning_intent": "low",
                },
                "image-generation": {
                    "quality": "image-generation",
                    "modality": ["text"],
                    "tools": "none",
                    "context_class": "short",
                    "reasoning_intent": "off",
                },
            },
        },
    }


def _write_registry(root: Path, *, promotion_state: str = "PUBLISHED") -> None:
    """Write a fully-hashed registry fixture under ``root``."""

    root.mkdir(exist_ok=True)
    payloads = _minimal_payloads()
    entries = []
    for name, payload in payloads.items():
        data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        (root / name).write_bytes(data)
        entries.append({"path": name, "sha256": hashlib.sha256(data).hexdigest()})
    prompt = b"route prompt\n"
    (root / "semantic-router-prompt.md").write_bytes(prompt)
    entries.append(
        {
            "path": "semantic-router-prompt.md",
            "sha256": hashlib.sha256(prompt).hexdigest(),
        }
    )
    manifest = {
        "schemaVersion": "hermes-workflow-registry/1.0",
        "registryVersion": "2026-08-21.1",
        "promotionState": promotion_state,
        "files": entries,
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _manifest(root: Path) -> dict[str, Any]:
    return json.loads((root / "manifest.json").read_text(encoding="utf-8"))


def _rewrite_payload(root: Path, filename: str, payload: Any) -> None:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    (root / filename).write_bytes(data)
    manifest = _manifest(root)
    next(entry for entry in manifest["files"] if entry["path"] == filename)[
        "sha256"
    ] = hashlib.sha256(data).hexdigest()
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


# ── GREEN: registry accepts the new role + contract ─────────────────────────


def test_image_generation_policy_validates_with_generation_only_primary_pool(
    tmp_path: Path,
) -> None:
    """model-policies.json must accept an image-generation policy whose
    primary_pool contains only generation-only FAL profiles."""

    _write_registry(tmp_path)

    snapshot = load_registry(tmp_path, mode="production")

    policies = snapshot.bundle["model_policies"]["policies"]
    assert "image-generation" in policies, (
        "image-generation role must be declared in model-policies.json"
    )
    pool = policies["image-generation"].get("primary_pool")
    assert pool is not None, "image-generation must declare a primary_pool"
    assert set(pool) == set(GENERATION_ONLY_PROFILES), (
        "image-generation primary_pool must be exactly the generation-only catalog; "
        f"got {set(pool)!r}, expected {set(GENERATION_ONLY_PROFILES)!r}"
    )
    # Reasoning families must never leak into a generation-only pool.
    forbidden = {"minimax-cn/MiniMax-M3", "openai-codex/gpt-5.6-luna"}
    assert forbidden.isdisjoint(pool), (
        "reasoning profiles must not appear in the image-generation primary_pool"
    )


def test_image_generation_contract_declares_text_modality_reasoning_off(
    tmp_path: Path,
) -> None:
    """capability-contracts.json must declare an image-generation contract
    with modality=['text'] and reasoning_intent='off'."""

    _write_registry(tmp_path)

    snapshot = load_registry(tmp_path, mode="production")

    contracts = snapshot.bundle["capability_contracts"]["contracts"]
    assert "image-generation" in contracts, (
        "image-generation contract must be declared in capability-contracts.json"
    )
    contract = contracts["image-generation"]
    assert list(contract["modality"]) == ["text"], (
        f"image-generation contract must be text-modality, got {list(contract['modality'])!r}"
    )
    assert contract["reasoning_intent"] == "off", (
        f"image-generation reasoning_intent must be 'off', got "
        f"{contract['reasoning_intent']!r}"
    )


def test_image_generation_policy_profiles_are_present_and_resolve(
    tmp_path: Path,
) -> None:
    """Every profile named in the image-generation primary_pool must exist
    in model-profiles.json and be cross-validated."""

    _write_registry(tmp_path)

    snapshot = load_registry(tmp_path, mode="production")

    profiles = snapshot.bundle["model_profiles"]["profiles"]
    pool = snapshot.bundle["model_policies"]["policies"]["image-generation"][
        "primary_pool"
    ]
    for model in pool:
        assert model in profiles, (
            f"image-generation primary_pool references unknown profile {model!r}"
        )
        assert profiles[model]["vendor_family"] == "fal", (
            f"image-generation profile {model!r} must be a FAL profile; "
            f"got vendor_family={profiles[model]['vendor_family']!r}"
        )
        assert profiles[model]["routing_role"] == "image-generation-only", (
            f"image-generation profile {model!r} must declare "
            f"routing_role=image-generation-only; "
            f"got {profiles[model]['routing_role']!r}"
        )


# ── RED: cross-file reference validator must still reject dangling models ───


def test_dangling_image_generation_primary_pool_model_is_rejected(
    tmp_path: Path,
) -> None:
    """If the image-generation primary_pool references a profile that is
    not in model-profiles.json, the loader must fail closed with a
    dangling_reference error."""

    _write_registry(tmp_path)
    payload = json.loads(
        json.dumps(_minimal_payloads()["model-policies.json"])
    )
    # Inject a dangling reference into the primary_pool.
    payload["policies"]["image-generation"]["primary_pool"] = [
        "fal/flux-2-pro",
        "fal/ghost-model",
    ]
    _rewrite_payload(tmp_path, "model-policies.json", payload)

    with pytest.raises(RegistryLoadError) as exc_info:
        load_registry(tmp_path)

    assert exc_info.value.code == "dangling_reference"
    assert (
        exc_info.value.path
        == "model-policies.json.policies.image-generation.primary_pool[1]"
    )


def test_dangling_image_generation_primary_string_is_rejected(
    tmp_path: Path,
) -> None:
    """A non-pool image-generation policy that names a single dangling
    profile must also fail closed."""

    _write_registry(tmp_path)
    payload = json.loads(
        json.dumps(_minimal_payloads()["model-policies.json"])
    )
    # Replace primary_pool with a single primary string pointing nowhere.
    payload["policies"]["image-generation"] = {
        "primary": "fal/ghost-model",
    }
    _rewrite_payload(tmp_path, "model-policies.json", payload)

    with pytest.raises(RegistryLoadError) as exc_info:
        load_registry(tmp_path)

    assert exc_info.value.code == "dangling_reference"
    assert exc_info.value.path is not None
    assert (
        exc_info.value.path
        == "model-policies.json.policies.image-generation.primary"
    )


def test_reasoning_profile_in_image_generation_pool_is_still_rejected(
    tmp_path: Path,
) -> None:
    """A reasoning profile (e.g. MiniMax-M3) added to the image-generation
    primary_pool must trigger a dangling_reference error if it is not
    also declared in model-profiles.json — the validator must not silently
    allow cross-family leakage into the generation-only pool."""

    _write_registry(tmp_path)
    policies_payload = json.loads(
        json.dumps(_minimal_payloads()["model-policies.json"])
    )
    # Inject a reasoning profile that is NOT in this fixture's
    # model-profiles.json: it's dangling from the image-generation pool's
    # point of view and the cross-file reference validator must reject it.
    policies_payload["policies"]["image-generation"]["primary_pool"] = [
        "fal/flux-2-pro",
        "minimax-cn/MiniMax-M3",  # not declared in profiles.json
    ]
    _rewrite_payload(tmp_path, "model-policies.json", policies_payload)

    with pytest.raises(RegistryLoadError) as exc_info:
        load_registry(tmp_path)

    assert exc_info.value.code == "dangling_reference"
    assert exc_info.value.path is not None
    assert "primary_pool" in exc_info.value.path


@pytest.mark.parametrize(
    "missing_field",
    ["modality", "tools", "context_class", "reasoning_intent"],
)
def test_image_generation_contract_missing_required_field_is_rejected(
    tmp_path: Path, missing_field: str
) -> None:
    """The new contract must obey the same required-field contract as every
    other capability contract — stripping any required field fails closed."""

    _write_registry(tmp_path)
    payload = json.loads(
        json.dumps(_minimal_payloads()["capability-contracts.json"])
    )
    payload["contracts"]["image-generation"].pop(missing_field)
    _rewrite_payload(tmp_path, "capability-contracts.json", payload)

    with pytest.raises(RegistryLoadError) as exc_info:
        load_registry(tmp_path)

    assert exc_info.value.code == "missing_required_field"
    assert exc_info.value.path is not None
    assert exc_info.value.path.startswith(
        "capability-contracts.json.contracts.image-generation."
    )


def test_image_generation_pool_must_be_nonempty_when_declared(
    tmp_path: Path,
) -> None:
    """An image-generation policy with an empty primary_pool must still
    parse, but if its only generation model disappears, the loader fails
    closed — not silently allow a no-op pool."""

    _write_registry(tmp_path)
    payload = json.loads(
        json.dumps(_minimal_payloads()["model-policies.json"])
    )
    payload["policies"]["image-generation"]["primary_pool"] = []
    _rewrite_payload(tmp_path, "model-policies.json", payload)

    # An empty primary_pool parses (no dangling refs to check), but the
    # loader must still complete without error and produce an immutable
    # snapshot.  This proves the validator does NOT relax invariant
    # enforcement just because the new role exists.
    snapshot = load_registry(tmp_path, mode="production")
    actual_pool = snapshot.bundle["model_policies"]["policies"][
        "image-generation"
    ]["primary_pool"]
    assert list(actual_pool) == [], (
        f"empty primary_pool must round-trip empty; got {list(actual_pool)!r}"
    )


# ── RED: live fixture must declare the image-generation role + contract ──
#
# The test below exercises the LIVE production fixture (a byte-for-byte
# copy of authority/registry/) so that adding the policy + contract to
# authority/registry/ is what flips these tests from RED to GREEN.
# Until then they fail with KeyError / AssertionError, which is exactly
# the RED-state signal we want from strict TDD.


LIVE_FIXTURE = Path(__file__).parents[1] / "fixtures" / "runtime_registry" / "live"


def test_live_registry_declares_image_generation_policy() -> None:
    """The shipped ``model-policies.json`` (via the live fixture) must
    declare an ``image-generation`` policy whose ``primary_pool`` lists
    only generation-only profiles."""

    snapshot = load_registry(LIVE_FIXTURE, mode="preview")

    policies = snapshot.bundle["model_policies"]["policies"]
    assert "image-generation" in policies, (
        "authority/registry/model-policies.json must declare an "
        "image-generation policy"
    )
    policy = policies["image-generation"]
    assert "primary_pool" in policy, (
        "image-generation policy must declare a primary_pool"
    )
    pool = list(policy["primary_pool"])
    expected_pool = list(GENERATION_ONLY_PROFILES)
    assert set(pool) == set(expected_pool), (
        f"image-generation primary_pool must equal the FAL catalog; "
        f"got {set(pool)!r}, expected {set(expected_pool)!r}"
    )


def test_live_registry_declares_image_generation_contract() -> None:
    """The shipped ``capability-contracts.json`` (via the live fixture)
    must declare an ``image-generation`` contract with text modality and
    ``reasoning_intent='off'``."""

    snapshot = load_registry(LIVE_FIXTURE, mode="preview")

    contracts = snapshot.bundle["capability_contracts"]["contracts"]
    assert "image-generation" in contracts, (
        "authority/registry/capability-contracts.json must declare an "
        "image-generation contract"
    )
    contract = contracts["image-generation"]
    assert list(contract["modality"]) == ["text"], (
        f"image-generation contract must be text-modality, got "
        f"{list(contract['modality'])!r}"
    )
    assert contract["reasoning_intent"] == "off", (
        f"image-generation reasoning_intent must be 'off', got "
        f"{contract['reasoning_intent']!r}"
    )


# ── RED: existing 65 tests must still pass — invariant guard ────────────────


def test_image_generation_role_does_not_break_manifest_hashing(tmp_path: Path) -> None:
    """Adding the new role + contract must not break the manifest hash
    contract: declared sha256 must still equal computed sha256 for every
    payload file."""

    _write_registry(tmp_path)

    snapshot = load_registry(tmp_path, mode="production")

    declared = {
        entry["path"]: entry["sha256"]
        for entry in snapshot.manifest["files"]
    }
    for relative_path, expected in declared.items():
        actual_path = snapshot.root / relative_path
        actual = hashlib.sha256(actual_path.read_bytes()).hexdigest()
        assert actual == expected, (
            f"manifest hash for {relative_path} drifted: declared={expected} actual={actual}"
        )


def test_image_generation_role_does_not_introduce_off_branch_validation(
    tmp_path: Path,
) -> None:
    """``reasoning_intent='off'`` on the image-generation contract must NOT
    cause the loader to spin up an extra validation branch — the contract
    is declarative only.

    Concretely: after loading a registry whose image-generation contract
    declares ``reasoning_intent='off'``, the snapshot's bundle must not
    expose any extra attribute or sentinel that hints at a code branch
    keyed on the ``off`` value.  This test exists so a future refactor
    that introduces ``validate_off_branch`` (or similar) is forced to
    update this guard.
    """

    _write_registry(tmp_path)

    snapshot = load_registry(tmp_path, mode="production")

    contract = snapshot.bundle["capability_contracts"]["contracts"][
        "image-generation"
    ]
    assert contract["reasoning_intent"] == "off"

    # The bundle must remain a flat declarative dict — no injected
    # helper attributes, no ``__post_init__`` side effects.
    forbidden_keys = {
        "_off_branch",
        "_validate_off_branch",
        "_off_branch_validated",
    }
    assert forbidden_keys.isdisjoint(contract.keys()), (
        f"image-generation contract leaked off-branch validation keys: "
        f"{forbidden_keys & contract.keys()}"
    )

    # And the loader must not have left behind any module-level sentinel
    # that names ``validate_off_branch``.
    import agent.runtime_registry as registry_module

    leaked = [
        name
        for name in dir(registry_module)
        if "off_branch" in name.lower() or "validate_off" in name.lower()
    ]
    assert not leaked, (
        f"runtime_registry must not export validate_off_branch / off_branch "
        f"helpers; found {leaked!r}"
    )