"""Fixture tests for the npmrc-stale-exclude lint. No live network —
registry responses are injected via the ``fetch_times`` parameter."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from lints import npmrc_stale_exclude as mod  # noqa: E402

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
OLD = (NOW - timedelta(days=40)).isoformat()  # far past 14d + grace
FRESH = (NOW - timedelta(days=3)).isoformat()  # inside 14d
BOUNDARY = (NOW - timedelta(days=14, hours=12)).isoformat()  # in the grace band


def _write_lockfile(path: Path, versions: dict[str, str]) -> None:
    packages = {"": {"name": "fixture", "version": "0.0.0"}}
    for name, version in versions.items():
        packages[f"node_modules/{name}"] = {"version": version}
    path.write_text(json.dumps({"lockfileVersion": 3, "packages": packages}), encoding="utf-8")


def _registry(times: dict[str, dict[str, str]]):
    def fetch(name: str) -> dict[str, str]:
        if name not in times:
            raise RuntimeError(f"unexpected registry fetch for {name}")
        return times[name]

    return fetch


def _evaluate(tmp_path, npmrc_text, lock_versions, registry_times):
    npmrc = tmp_path / ".npmrc"
    npmrc.write_text(npmrc_text, encoding="utf-8")
    lockfile = tmp_path / "package-lock.json"
    _write_lockfile(lockfile, lock_versions)
    return npmrc, mod.evaluate_file(npmrc, lockfile, _registry(registry_times), now=NOW)


# ── parse_npmrc ──────────────────────────────────────────────────────────


def test_parse_groups_comment_blocks_and_entries():
    text = (
        "engine-strict=true\n"
        "\n"
        "min-release-age=14\n"
        "# vuln fix. remove when old\n"
        "min-release-age-exclude[]=tar\n"
        "\n"
        "# ink needs\n"
        "min-release-age-exclude[]=lightningcss\n"
        "min-release-age-exclude[]=postcss\n"
    )
    min_age, groups = mod.parse_npmrc(text)
    assert min_age == 14
    assert len(groups) == 2
    assert [p for _, p in groups[0].entries] == ["tar"]
    assert [p for _, p in groups[1].entries] == ["lightningcss", "postcss"]


def test_parse_real_repo_npmrc_files():
    """Both tracked .npmrc files must parse (invariant, not a snapshot)."""
    for rel in (".npmrc", "website/.npmrc"):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        min_age, groups = mod.parse_npmrc(text)
        assert min_age is not None, rel
        assert groups, rel
        for group in groups:
            assert group.entries, rel


# ── locked_versions ──────────────────────────────────────────────────────


def test_locked_versions_handles_nested_and_scoped(tmp_path):
    lockfile = tmp_path / "package-lock.json"
    lockfile.write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "root", "version": "1.0.0"},
                    "node_modules/@scope/pkg": {"version": "1.0.0"},
                    "node_modules/a": {"version": "2.0.0"},
                    "node_modules/a/node_modules/b": {"version": "3.0.0"},
                    "apps/x/node_modules/a": {"version": "2.5.0"},
                },
            }
        ),
        encoding="utf-8",
    )
    locked = mod.locked_versions(lockfile)
    assert locked["@scope/pkg"] == {"1.0.0"}
    assert locked["a"] == {"2.0.0", "2.5.0"}  # both install paths collected
    assert locked["b"] == {"3.0.0"}


# ── evaluate_file ────────────────────────────────────────────────────────

BASE = "min-release-age=14\n# fix. remove when > 2wks old\nmin-release-age-exclude[]=tar\n"


def test_stale_when_all_locked_versions_old(tmp_path):
    _, stale = _evaluate(tmp_path, BASE, {"tar": "7.5.21"}, {"tar": {"7.5.21": OLD}})
    assert len(stale) == 1
    assert stale[0][1] == "tar"
    assert "older than min-release-age" in stale[0][2]


def test_fresh_version_keeps_exclude(tmp_path):
    _, stale = _evaluate(tmp_path, BASE, {"tar": "7.5.21"}, {"tar": {"7.5.21": FRESH}})
    assert stale == []


def test_boundary_grace_keeps_exclude(tmp_path):
    """Just past min-release-age but inside the 1-day grace: keep, to
    avoid remove/re-add churn across runs."""
    _, stale = _evaluate(tmp_path, BASE, {"tar": "7.5.21"}, {"tar": {"7.5.21": BOUNDARY}})
    assert stale == []


def test_one_fresh_version_among_old_keeps_exclude(tmp_path):
    """A name locked at several versions (nested deps): any fresh one
    means the exclude is still doing work."""
    npmrc = "min-release-age=14\nmin-release-age-exclude[]=nanoid\n"
    lockfile = tmp_path / "package-lock.json"
    lockfile.write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "node_modules/nanoid": {"version": "5.0.0"},
                    "node_modules/x/node_modules/nanoid": {"version": "3.3.17"},
                },
            }
        ),
        encoding="utf-8",
    )
    path = tmp_path / ".npmrc"
    path.write_text(npmrc, encoding="utf-8")
    stale = mod.evaluate_file(
        path,
        lockfile,
        _registry({"nanoid": {"5.0.0": OLD, "3.3.17": FRESH}}),
        now=NOW,
    )
    assert stale == []


def test_wildcard_scope_expands_against_lockfile(tmp_path):
    npmrc = "min-release-age=14\nmin-release-age-exclude[]=@radix-ui/*\n"
    _, stale = _evaluate(
        tmp_path,
        npmrc,
        {"@radix-ui/react-menu": "2.0.0", "@radix-ui/number": "1.1.3"},
        {
            "@radix-ui/react-menu": {"2.0.0": OLD},
            "@radix-ui/number": {"1.1.3": OLD},
        },
    )
    assert len(stale) == 1 and stale[0][1] == "@radix-ui/*"


def test_wildcard_with_one_fresh_member_kept(tmp_path):
    npmrc = "min-release-age=14\nmin-release-age-exclude[]=@radix-ui/*\n"
    _, stale = _evaluate(
        tmp_path,
        npmrc,
        {"@radix-ui/react-menu": "2.0.0", "@radix-ui/number": "1.1.3"},
        {
            "@radix-ui/react-menu": {"2.0.0": OLD},
            "@radix-ui/number": {"1.1.3": FRESH},
        },
    )
    assert stale == []


def test_no_lockfile_match_is_stale(tmp_path):
    npmrc = "min-release-age=14\nmin-release-age-exclude[]=left-pad\n"
    _, stale = _evaluate(tmp_path, npmrc, {"tar": "7.5.21"}, {})
    assert len(stale) == 1
    assert "matches no package" in stale[0][2]


def test_a_comment_cannot_exempt_a_stale_entry(tmp_path):
    """There is no opt-out marker. An exclude expires when its releases
    grow older than the gate. The comment above it has no effect."""
    npmrc = (
        "min-release-age=14\n"
        "# lint: keep — they update a LOT\n"
        "min-release-age-exclude[]=@assistant-ui/*\n"
    )
    _, stale = _evaluate(
        tmp_path,
        npmrc,
        {"@assistant-ui/react": "1.0.0"},
        {"@assistant-ui/react": {"1.0.0": OLD}},
    )
    assert [pattern for _, pattern, _ in stale] == ["@assistant-ui/*"]


def test_registry_failure_fails_open(tmp_path):
    def exploding_fetch(name):
        raise OSError("registry down")

    npmrc_path = tmp_path / ".npmrc"
    npmrc_path.write_text(BASE, encoding="utf-8")
    lockfile = tmp_path / "package-lock.json"
    _write_lockfile(lockfile, {"tar": "7.5.21"})
    stale = mod.evaluate_file(npmrc_path, lockfile, exploding_fetch, now=NOW)
    assert stale == []


def test_unknown_publish_date_fails_open(tmp_path):
    """Version missing from the registry's time map counts as young."""
    _, stale = _evaluate(tmp_path, BASE, {"tar": "7.5.21"}, {"tar": {}})
    assert stale == []


def test_missing_lockfile_yields_nothing(tmp_path):
    npmrc = tmp_path / ".npmrc"
    npmrc.write_text(BASE, encoding="utf-8")
    stale = mod.evaluate_file(
        npmrc, tmp_path / "package-lock.json", _registry({}), now=NOW
    )
    assert stale == []


# ── remove_stale_lines ───────────────────────────────────────────────────


def test_removal_deletes_line_and_orphaned_comment_block():
    text = (
        "min-release-age=14\n"
        "\n"
        "# tar fix. remove when > 2wks old\n"
        "min-release-age-exclude[]=tar\n"
        "\n"
        "# fresh fix, still needed\n"
        "min-release-age-exclude[]=nanoid\n"
    )
    _, groups = mod.parse_npmrc(text)
    tar_idx = next(i for g in groups for i, p in g.entries if p == "tar")
    result = mod.remove_stale_lines(text, {tar_idx})
    assert "tar" not in result
    assert "# tar fix" not in result  # orphaned comment went with it
    assert "min-release-age-exclude[]=nanoid" in result
    assert "# fresh fix, still needed" in result
    assert "\n\n\n" not in result  # no double-blank residue


def test_removal_keeps_comment_when_group_partially_survives():
    text = (
        "min-release-age=14\n"
        "# vite chain — remove once vite is old\n"
        "min-release-age-exclude[]=vite\n"
        "min-release-age-exclude[]=rolldown\n"
    )
    _, groups = mod.parse_npmrc(text)
    vite_idx = next(i for g in groups for i, p in g.entries if p == "vite")
    result = mod.remove_stale_lines(text, {vite_idx})
    assert "min-release-age-exclude[]=vite\n" not in result
    assert "min-release-age-exclude[]=rolldown" in result
    assert "# vite chain" in result  # block still owns a live entry


def test_removal_preserves_unrelated_directives():
    text = (
        "engine-strict=true\n"
        "\n"
        "min-release-age=14\n"
        "min-release-age-exclude[]=tar\n"
    )
    _, groups = mod.parse_npmrc(text)
    tar_idx = groups[0].entries[0][0]
    result = mod.remove_stale_lines(text, {tar_idx})
    assert "engine-strict=true" in result
    assert "min-release-age=14" in result
    assert "exclude" not in result


# ── _verify_only_exclude_deletions ───────────────────────────────────────


def test_verify_accepts_pure_exclude_deletion():
    old = "min-release-age=14\n# c\nmin-release-age-exclude[]=tar\nengine-strict=true\n"
    new = "min-release-age=14\nengine-strict=true\n"
    mod._verify_only_exclude_deletions(old, new)  # no raise


def test_verify_rejects_deleting_other_directives():
    old = "min-release-age=14\nengine-strict=true\n"
    new = "min-release-age=14\n"
    try:
        mod._verify_only_exclude_deletions(old, new)
    except RuntimeError as e:
        assert "which is not an exclude" in str(e)
    else:
        raise AssertionError("the verifier must refuse to delete engine-strict")


def test_verify_rejects_edited_or_inserted_lines():
    old = "min-release-age=14\n"
    for new in ("min-release-age=7\n", "min-release-age=14\nregistry=https://evil\n"):
        try:
            mod._verify_only_exclude_deletions(old, new)
        except RuntimeError as e:
            assert "not present in the original" in str(e)
        else:
            raise AssertionError(f"should have refused: {new!r}")


# ── end-to-end: check()/fix() against a fixture repo ─────────────────────


def test_fix_roundtrip(tmp_path, monkeypatch):
    npmrc = tmp_path / ".npmrc"
    npmrc.write_text(
        "min-release-age=14\n"
        "# stale one\n"
        "min-release-age-exclude[]=tar\n"
        "# fresh one\n"
        "min-release-age-exclude[]=nanoid\n",
        encoding="utf-8",
    )
    _write_lockfile(tmp_path / "package-lock.json", {"tar": "7.5.21", "nanoid": "3.3.17"})

    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "_tracked_npmrc_files", lambda: [npmrc])
    monkeypatch.setattr(
        mod,
        "_fetch_times",
        _registry({"tar": {"7.5.21": OLD}, "nanoid": {"3.3.17": FRESH}}),
    )
    # evaluate_file defaults now to real utcnow; OLD/FRESH are relative to
    # NOW which is in the past — regenerate stamps relative to real now.
    real_now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        mod,
        "_fetch_times",
        _registry(
            {
                "tar": {"7.5.21": (real_now - timedelta(days=40)).isoformat()},
                "nanoid": {"3.3.17": (real_now - timedelta(days=3)).isoformat()},
            }
        ),
    )

    findings = mod.check()
    assert [f.path for f in findings] == [".npmrc"]
    assert all(f.fixable for f in findings)
    assert "tar" in findings[0].message

    changed = mod.fix()
    assert changed == [".npmrc"]
    text = npmrc.read_text(encoding="utf-8")
    assert "tar" not in text
    assert "min-release-age-exclude[]=nanoid" in text

    # Idempotent: second pass finds nothing, changes nothing.
    assert mod.check() == []
    assert mod.fix() == []


def test_lint_metadata_contract():
    """The lint must be network-gated and autofix-bounded to .npmrc."""
    lint = mod.LINT
    assert lint.network is True  # advisory on PRs by policy
    assert lint.autofix is True
    from fnmatch import fnmatch

    for path in (".npmrc", "website/.npmrc"):
        assert any(fnmatch(path, g) for g in lint.fix_touches), path
