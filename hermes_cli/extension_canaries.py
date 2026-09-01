"""Fail-closed canary controls for optional skill-retrieval and RTK plugins.

The helpers in this module are deliberately plugin-agnostic. They evaluate
receipts produced by canary runs and report profile-local capability state;
they never install, update, import, or promote a third-party plugin.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any


REQUIRED_RTK_CASES = (
    "git_status",
    "git_diff",
    "focused_tests",
    "large_logs",
    "failing_output",
)

_FLOATING_REVISIONS = frozenset({"", "head", "latest", "main", "master", "trunk"})
_RELEASE_TAG = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


def is_pinned_revision(revision: object) -> bool:
    """Return whether a reviewed source revision is immutable enough to canary.

    Full commit SHAs and semantic-version release tags are accepted. Branches,
    ``latest``, ``HEAD``, and blank revisions fail closed so this control plane
    can never turn an unreviewed global-latest install into a capability.
    """

    if not isinstance(revision, str):
        return False
    value = revision.strip()
    if value.casefold() in _FLOATING_REVISIONS:
        return False
    return bool(_COMMIT_SHA.fullmatch(value) or _RELEASE_TAG.fullmatch(value))


def _normalise_skill_name(value: object) -> str:
    return str(value or "").strip().casefold()


def evaluate_skill_retrieval(
    corpus: Sequence[Mapping[str, Any]],
    retrieve: Callable[[str, int], Iterable[str]],
    *,
    top_k: int = 8,
    minimum_recall: float = 0.95,
) -> dict[str, Any]:
    """Evaluate expected-skill-in-top-K over a prompt regression corpus."""

    if not corpus:
        raise ValueError("skill retrieval corpus must not be empty")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
        raise ValueError("top_k must be a positive integer")
    if not isinstance(minimum_recall, (int, float)) or not 0 <= minimum_recall <= 1:
        raise ValueError("minimum_recall must be between 0 and 1")

    misses: list[dict[str, str]] = []
    hits = 0
    for index, item in enumerate(corpus):
        prompt = str(item.get("prompt") or "").strip()
        expected = str(item.get("expected_skill") or "").strip()
        if not prompt or not expected:
            raise ValueError(f"corpus item {index} requires prompt and expected_skill")
        ranked = list(retrieve(prompt, top_k))[:top_k]
        expected_key = _normalise_skill_name(expected)
        if expected_key in {_normalise_skill_name(name) for name in ranked}:
            hits += 1
        else:
            misses.append(
                {
                    "id": str(item.get("id") or index),
                    "expected_skill": expected,
                }
            )

    total = len(corpus)
    recall = hits / total
    return {
        "status": "passed" if recall >= minimum_recall else "failed",
        "top_k": top_k,
        "minimum_recall": minimum_recall,
        "total": total,
        "hits": hits,
        "recall": recall,
        "misses": misses,
    }


def evaluate_rtk_comparisons(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Validate raw-versus-compressed RTK canary receipts.

    Compressed text is never compared as proof. Passing means every required
    routine case was exercised, RTK preserved the command exit code, and the
    explicit reviewer bypass returned the exact same bounded raw string.
    """

    by_name = {
        str(case.get("case") or "").strip(): case
        for case in cases
        if str(case.get("case") or "").strip()
    }
    missing = [name for name in REQUIRED_RTK_CASES if name not in by_name]
    compared = [by_name[name] for name in REQUIRED_RTK_CASES if name in by_name]
    exit_codes_preserved = all(
        case.get("raw_exit_code") == case.get("compressed_exit_code")
        for case in compared
    )
    raw_bypass_verified = all(
        isinstance(case.get("raw_output"), str)
        and case.get("raw_bypass_output") == case.get("raw_output")
        for case in compared
    )
    passed = bool(compared) and not missing and exit_codes_preserved and raw_bypass_verified

    return {
        "status": "passed" if passed else "failed",
        "required_cases": list(REQUIRED_RTK_CASES),
        "missing_cases": missing,
        "exit_codes_preserved": exit_codes_preserved,
        "raw_bypass_verified": raw_bypass_verified,
        "compressed_authoritative": False,
        "comparisons": [
            {
                "case": str(case["case"]),
                "raw_chars": len(str(case.get("raw_output") or "")),
                "compressed_chars": len(str(case.get("compressed_output") or "")),
                "exit_code_preserved": case.get("raw_exit_code")
                == case.get("compressed_exit_code"),
                "raw_bypass_verified": case.get("raw_bypass_output")
                == case.get("raw_output"),
            }
            for case in compared
        ],
    }


def installed_extension_plugins() -> set[str]:
    """Inspect plugin manifests without importing third-party plugin code."""

    try:
        from hermes_cli.plugins import (
            PluginManager,
            discover_entrypoint_manifests,
        )

        manifests = PluginManager()._collect_directory_manifests()
        manifests.extend(discover_entrypoint_manifests())
        names: set[str] = set()
        for manifest in manifests:
            names.add(str(manifest.name))
            names.add(str(manifest.key or manifest.name))
        return names
    except Exception:
        return set()


def _extension_state(section: object, installed_plugins: set[str]) -> str:
    if not isinstance(section, Mapping) or not section.get("enabled", False):
        return "disabled"
    plugin = str(section.get("plugin") or "").strip()
    if not plugin or plugin not in installed_plugins:
        return "missing"
    source = section.get("source")
    if not isinstance(source, Mapping) or not is_pinned_revision(
        source.get("revision")
    ):
        return "installed"
    regression = section.get("regression")
    regression_status = (
        str(regression.get("status") or "not_run").strip().casefold()
        if isinstance(regression, Mapping)
        else "not_run"
    )
    if regression_status != "passed":
        return "installed"
    if str(section.get("promotion") or "canary").strip().casefold() == "promoted":
        return "promoted"
    return "canary"


def extension_health(
    config: Mapping[str, Any], *, installed_plugins: set[str] | None = None
) -> dict[str, str]:
    """Return profile-local lifecycle state for optional canary extensions."""

    installed = installed_extension_plugins() if installed_plugins is None else installed_plugins
    return {
        "skill_retrieval": _extension_state(config.get("skill_retrieval"), installed),
        "rtk": _extension_state(config.get("rtk"), installed),
    }


def extension_capabilities(
    config: Mapping[str, Any], *, installed_plugins: set[str] | None = None
) -> set[str]:
    """Advertise only installed, regression-passing extension capabilities."""

    health = extension_health(config, installed_plugins=installed_plugins)
    ready = {"canary", "promoted"}
    capabilities: set[str] = set()
    retrieval = config.get("skill_retrieval")
    if health["skill_retrieval"] in ready:
        capabilities.add("skill_retrieval.retrieve")
    rtk = config.get("rtk")
    if health["rtk"] in ready and isinstance(rtk, Mapping):
        if rtk.get("routine_filter") is True:
            capabilities.add("rtk.routine_filter")
        if rtk.get("raw_bypass") is True:
            capabilities.add("terminal.raw_evidence")
    return capabilities
