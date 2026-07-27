import ast
import re
import tomllib
from pathlib import Path

import pytest
from packaging.version import Version


REPO_ROOT = Path(__file__).resolve().parents[1]


def _distribution_name(requirement: str) -> str:
    """Extract the PEP 508 distribution name from a requirement string.

    Robust to markers (``; python_version < '3.12'``), direct references
    (``name @ https://...``), extras (``name[extra]``) and every version
    operator (``==``, ``>=``, ``<=``, ``~=``, ``!=``, ``<``, ``>``), so a
    future dep declared with any valid specifier shape doesn't silently
    mis-parse here.
    """
    spec = requirement.split(";", 1)[0]  # drop environment markers
    spec = spec.split("@", 1)[0]  # drop direct-reference URLs
    spec = spec.split("[", 1)[0]  # drop extras
    spec = re.split(r"[=<>!~]", spec, maxsplit=1)[0]  # drop any version operator
    return spec.strip().lower()


def test_packaging_declared_as_core_dependency():
    """Regression for #40503.

    ``packaging`` is imported directly on three production paths
    (plugins/memory/hindsight/__init__.py, tools/lazy_deps.py,
    hermes_cli/main.py) yet was undeclared, so it only reached users
    transitively. The slim Docker image shipped without it, silently
    disabling Hindsight append-mode and version-constraint checks. It must
    be a declared core dependency so it installs everywhere and the
    update-repair step (``_verify_core_dependencies_installed``) guards it.
    """
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    core = data["project"]["dependencies"]
    names = {_distribution_name(dep) for dep in core}
    assert "packaging" in names, (
        "packaging is imported on production paths (hindsight version compare, "
        "lazy_deps version constraints, requirement parsing) and must be a "
        "declared core dependency, not a transitive — see #40503"
    )


def test_faster_whisper_is_not_a_base_dependency():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]

    assert not any(dep.startswith("faster-whisper") for dep in deps)

    voice_extra = data["project"]["optional-dependencies"]["voice"]
    assert any(dep.startswith("faster-whisper") for dep in voice_extra)


# Minimum non-vulnerable Starlette: CVE-2026-48710 ("BadHost") was fixed in
# 1.0.1. Anything below that lets a malformed Host header desync
# ``request.url.path`` from the dispatched ASGI path, bypassing path-based
# authz in middleware/endpoints that gate on ``request.url``. Starlette is a
# transitive dep (fastapi in [web]; sse-starlette/mcp in [mcp]/[computer-use]/
# [dev]) so we pin it directly in every extra that exposes a server surface and
# enforce the current reviewed floor in both pyproject and the committed
# lockfile.
_STARLETTE_CVE_FLOOR = (1, 3, 1)
_UPDATE_DOWNGRADE_GUARD_FLOORS = {
    # `hermes update` reinstalls exact pins from pyproject/lazy_deps. These
    # reviewed CVE pins must not slide back to stale versions that downgrade
    # already-patched user environments.
    "cryptography": (48, 0, 1),
    "starlette": (1, 3, 1),
    "python-multipart": (0, 0, 32),
}


def _version_below_floor(version: str, floor: tuple[int, ...]) -> bool:
    """Compare dependency versions with PEP 440 prerelease semantics."""
    return Version(version) < Version(".".join(map(str, floor)))


def test_security_floor_comparison_rejects_prereleases() -> None:
    assert _version_below_floor("1.3.1rc1", (1, 3, 1))


def test_starlette_pinned_above_current_security_floor_in_pyproject():
    """Core and every server extra must exact-pin patched Starlette.

    Regression guard for #35067 and #72108. A future edit that drops the
    pin (re-exposing the unbounded transitive ``starlette>=0.27`` from mcp /
    ``>=0.40.0`` from fastapi) or pins a pre-1.3.1 version fails here instead of
    shipping a known-vulnerable server dependency to dashboard / MCP users.
    """
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    core = data["project"]["dependencies"]
    extras = data["project"]["optional-dependencies"]

    expected = f"starlette=={'.'.join(map(str, _STARLETTE_CVE_FLOOR))}"
    assert expected in core, (
        "core dependencies must exact-pin the Starlette security floor because "
        "core FastAPI installs otherwise admit a stale vulnerable transitive"
    )

    found = {}
    for extra, specs in extras.items():
        for spec in specs:
            name = (
                spec
                .split("==", 1)[0]
                .split(">", 1)[0]
                .split("<", 1)[0]
                .split("[", 1)[0]
                .strip()
            )
            if name.lower() == "starlette":
                assert "==" in spec, f"[{extra}] must exact-pin starlette, got {spec!r}"
                ver = spec.split("==", 1)[1].split(";", 1)[0].strip()
                found[extra] = ver

    # The four server-surface extras must each carry the direct pin.
    for extra in ("web", "mcp", "computer-use", "dev"):
        assert extra in found, (
            f"[{extra}] no longer pins starlette directly — security regression "
            f"risk (mcp/fastapi pull it transitively with no upper bound)"
        )

    for extra, ver in found.items():
        assert not _version_below_floor(ver, _STARLETTE_CVE_FLOOR), (
            f"[{extra}] pins starlette=={ver}, below the current security floor "
            f"{'.'.join(map(str, _STARLETTE_CVE_FLOOR))}"
        )


def test_locked_dependencies_clear_issue_72108_high_advisories():
    """The hash-verified install must retain every #72108 security floor."""
    minimums = {
        "cryptography": (48, 0, 1),
        "httplib2": (0, 32, 0),
        "mcp": (1, 28, 1),
        "pillow": (12, 3, 0),
        "pyasn1": (0, 6, 4),
        "python-multipart": (0, 0, 32),
        "starlette": _STARLETTE_CVE_FLOOR,
    }
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked: dict[str, list[str]] = {}
    for package in lock["package"]:
        locked.setdefault(package["name"].lower(), []).append(package["version"])

    for package, floor in minimums.items():
        assert package in locked, f"{package} not found in uv.lock"
        for version in locked[package]:
            assert not _version_below_floor(version, floor), (
                f"uv.lock resolves {package}=={version}, "
                f"below the #72108 security floor {'.'.join(map(str, floor))}"
            )


def test_update_cve_pins_do_not_downgrade_reviewed_current_versions():
    """`hermes update` must not reinstall stale reviewed CVE pins.

    The project intentionally exact-pins reviewed dependency versions. When
    security pins get stale, update reinstalls can downgrade environments that
    already contain newer fixed versions. Guard the reviewed CVE packages
    across pyproject, lazy_deps, and the committed lockfile.
    """
    pins = _pins_from_specs(_pyproject_pinned_specs() + _lazy_deps_pinned_specs())
    for package, floor in _UPDATE_DOWNGRADE_GUARD_FLOORS.items():
        versions = pins.get(package)
        assert versions, f"{package} is no longer exact-pinned; update this guard"
        below_floor = sorted(
            version for version in versions
            if _version_below_floor(version, floor)
        )
        assert not below_floor, (
            f"{package} exact pin(s) {below_floor} are below the reviewed "
            f"anti-downgrade floor {'.'.join(map(str, floor))}; bump the pin "
            "and regenerate uv.lock"
        )
        locked_versions = _locked_versions(package)
        assert locked_versions, f"{package} is missing from uv.lock"
        locked_below_floor = sorted(
            version for version in locked_versions
            if _version_below_floor(version, floor)
        )
        assert not locked_below_floor, (
            f"uv.lock resolves {package} version(s) {locked_below_floor} below "
            f"the reviewed anti-downgrade floor {'.'.join(map(str, floor))}; "
            "regenerate uv.lock after bumping the pin"
        )


# ---------------------------------------------------------------------------
# Dependency-pin consistency: pyproject extras <-> tools/lazy_deps.py
#
# The same package is exact-pinned in two hand-maintained places: the
# [project.optional-dependencies] extras in pyproject.toml and the LAZY_DEPS
# allowlist in tools/lazy_deps.py (the lazy-install path deliberately mirrors
# the extras — see the comments on LAZY_DEPS: "match the corresponding extra
# in pyproject.toml ... update both this map AND the corresponding extra").
#
# They have silently drifted more than once: the aiohttp Slack pin (3.13.3 in
# the extras vs 3.13.4 in lazy_deps) and the anthropic pin (0.86.0 vs 0.87.0).
# The version a user ends up with then depends on whether the backend was
# installed eagerly (extra) or lazily (lazy_deps) — and for a CVE bump applied
# to only one side, that divergence is a latent security regression. These two
# tests assert the documented contract: the two sources agree, in lockstep.
# ---------------------------------------------------------------------------

# Matches "name==version" and "name[extra]==version", ignoring any trailing
# environment marker / comment. Only exact pins are collected; ranged specs
# (">=", "<") can't be compared for equality and are skipped.
_PIN_RE = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*==\s*([^\s;,#]+)"
)


def _canonical(name: str) -> str:
    # PEP 503 normalization so e.g. discord.py / discord-py compare equal.
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins_from_specs(specs):
    """Map canonical package name -> set of exact-pinned versions seen."""
    pins: dict[str, set[str]] = {}
    for spec in specs:
        m = _PIN_RE.match(spec)
        if not m:
            continue
        pins.setdefault(_canonical(m.group(1)), set()).add(m.group(2))
    return pins


def _locked_versions(package: str) -> set[str]:
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    return {
        pkg["version"]
        for pkg in lock.get("package", [])
        if _canonical(pkg["name"]) == _canonical(package)
    }


def _pyproject_pinned_specs():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    specs = list(data["project"].get("dependencies", []))
    for extra in data["project"].get("optional-dependencies", {}).values():
        specs.extend(extra)
    return specs


def _lazy_deps_pinned_specs():
    """Extract every string literal inside the LAZY_DEPS dict via AST.

    Parsing rather than importing keeps this test free of
    tools/lazy_deps.py's runtime imports and side effects.
    """
    src = (REPO_ROOT / "tools" / "lazy_deps.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    specs: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "LAZY_DEPS" for t in targets):
            continue
        for sub in ast.walk(node.value):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                specs.append(sub.value)
    assert specs, "could not extract specs from LAZY_DEPS — the AST parser drifted"
    return specs


def test_pyproject_pins_are_internally_consistent():
    """No package may be exact-pinned to two different versions in pyproject.

    A package legitimately appearing in several extras (e.g. aiohttp in
    messaging/slack/homeassistant/sms) must use the SAME version everywhere.
    """
    pins = _pins_from_specs(_pyproject_pinned_specs())
    conflicts = {name: sorted(v) for name, v in pins.items() if len(v) > 1}
    assert not conflicts, (
        "pyproject.toml exact-pins the same package to different versions "
        "across [project.dependencies] / extras: " + str(conflicts)
    )


def test_pyproject_and_lazy_deps_pins_agree():
    """Every package pinned in BOTH places must use the same version.

    Regression guard for the aiohttp / anthropic extras-vs-lazy drift:
    tools/lazy_deps.py mirrors the pyproject extras, so a CVE bump applied to
    one and not the other leaves users on a vulnerable version depending on
    the install path. Bump both in lockstep.
    """
    py = _pins_from_specs(_pyproject_pinned_specs())
    lazy = _pins_from_specs(_lazy_deps_pinned_specs())

    mismatches = [
        f"{name}: pyproject={sorted(py[name])} lazy_deps={sorted(lazy[name])}"
        for name in sorted(set(py) & set(lazy))
        if py[name] != lazy[name]
    ]
    assert not mismatches, (
        "pyproject.toml extras and tools/lazy_deps.py disagree on the pinned "
        "version of the same package — bump both in lockstep:\n  "
        + "\n  ".join(mismatches)
    )


def _lazy_deps_by_feature():
    """Parse LAZY_DEPS into {feature_name: [spec, ...]} via AST.

    Same parse-don't-import rationale as _lazy_deps_pinned_specs, but keeps the
    feature -> specs grouping so per-feature coverage can be asserted.
    """
    src = (REPO_ROOT / "tools" / "lazy_deps.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        if not any(isinstance(t, ast.Name) and t.id == "LAZY_DEPS" for t in targets):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        by_feature: dict[str, list[str]] = {}
        for key, value in zip(node.value.keys, node.value.values):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            by_feature[key.value] = [
                sub.value
                for sub in ast.walk(value)
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
            ]
        assert by_feature, (
            "could not extract features from LAZY_DEPS — AST parser drifted"
        )
        return by_feature
    raise AssertionError("LAZY_DEPS dict literal not found in tools/lazy_deps.py")


# Security-critical packages whose patched floor must be enforced on EVERY
# install path, eager and lazy. test_pyproject_and_lazy_deps_pins_agree only
# fires when a package is pinned in BOTH sources, so it cannot catch a lazy
# feature that omits the pin entirely — the exact gap that left platform.slack
# carrying aiohttp==3.14.0 while platform.discord (whose discord.py dep pulls
# aiohttp transitively as its HTTP backbone) shipped without it, so the lazy
# Discord path could keep an already-installed vulnerable aiohttp. A fully
# general "no mirrored feature drops a pin" check is impossible statically
# (it can't see transitive deps), so this is the explicit coverage contract:
# each security package -> the lazy features that bundle an SDK pulling it and
# must therefore carry the same pin as the pyproject extra.
_REQUIRED_SECURITY_PINS = {
    # Every lazy messaging feature whose SDK pulls aiohttp transitively must
    # carry the patched floor directly: discord.py (aiohttp<4), slack-bolt,
    # mautrix/aiohttp-socks (aiohttp<4 / >=3.10), and microsoft-teams-apps —
    # none of those upper/lower bounds excludes a vulnerable already-installed
    # aiohttp, so the lazy path would not upgrade it without an explicit pin.
    "aiohttp": {
        "platform.discord",
        "platform.slack",
        "platform.matrix",
        "platform.teams",
    },
    # google-api-python-client admits old httplib2 releases. Both the unlocked
    # google extra and the lazy Workspace installer must carry the patched
    # decompression-bound floor directly.
    "httplib2": {
        "skill.google_workspace",
    },
    # google-auth's pyasn1-modules dependency admits old pyasn1 releases.
    # Workspace and Vertex must repair the stale transitive on unlocked and
    # lazy install paths instead of relying on uv.lock alone.
    "pyasn1": {
        "skill.google_workspace",
        "provider.vertex",
    },
}

_REQUIRED_EXTRA_SECURITY_PINS = {
    "httplib2": {
        "google",
    },
    "pyasn1": {
        "google",
        "vertex",
    },
}


def test_security_pins_present_in_google_extras():
    """Unlocked Google extras must directly carry their transitive floors."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = data["project"]["optional-dependencies"]
    all_pins = _pins_from_specs(_pyproject_pinned_specs())

    problems = []
    for pkg, required_extras in _REQUIRED_EXTRA_SECURITY_PINS.items():
        canon = _canonical(pkg)
        expected = all_pins.get(canon)
        assert expected, f"{pkg} has no exact pin in pyproject.toml"
        for extra in sorted(required_extras):
            got = _pins_from_specs(extras[extra]).get(canon)
            if got != expected:
                problems.append(
                    f"{extra}: {pkg}="
                    f"{sorted(got) if got else 'MISSING'}, expected {sorted(expected)}"
                )
    assert not problems, (
        "a Google extra is missing a transitive security floor:\n  "
        + "\n  ".join(problems)
    )


def test_security_pins_present_in_mirrored_lazy_features():
    """Curated security pins must be present (not just version-consistent) in
    every lazy feature that bundles an SDK pulling that package transitively.
    """
    py = _pins_from_specs(_pyproject_pinned_specs())
    by_feature = _lazy_deps_by_feature()

    problems = []
    for pkg, features in _REQUIRED_SECURITY_PINS.items():
        canon = _canonical(pkg)
        expected = py.get(canon)
        assert expected, (
            f"{pkg} is listed in _REQUIRED_SECURITY_PINS but is not exact-pinned "
            f"in pyproject.toml — update the map or the pin."
        )
        for feature in sorted(features):
            specs = by_feature.get(feature)
            assert specs is not None, (
                f"lazy feature {feature!r} named in _REQUIRED_SECURITY_PINS no "
                f"longer exists in LAZY_DEPS — update the map."
            )
            got = _pins_from_specs(specs).get(canon)
            if got != expected:
                problems.append(
                    f"{feature}: {pkg}="
                    f"{sorted(got) if got else 'MISSING'}, expected {sorted(expected)}"
                )
    assert not problems, (
        "a lazy feature is missing a security pin it must mirror from the "
        "pyproject extras — the lazy install path would not enforce the "
        "CVE-patched floor:\n  " + "\n  ".join(problems)
    )
