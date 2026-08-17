"""Tests for ruff lint config — guards against accidental rule removal.

PLW1514 (unspecified-encoding) was enabled after a debug session on
Windows turned up three separate UTF-8 regressions in execute_code.
The rule catches bare ``open()`` / ``read_text()`` / ``write_text()``
calls that default to locale encoding — cp1252 on Windows — which
silently corrupts non-ASCII content.

The Pyflakes "F" group was added alongside it in May 2026, then fell out of
``main`` during the v0.15.1 upstream cutover — silently, with no named revert.
Nobody noticed for two and a half months because nothing was checking: the CI
job kept passing, because it enforces whatever ``select`` says and ``select``
had quietly become ``["PLW1514"]``. That is the failure mode this file exists
to make loud.

These tests ensure:
  1. Both ``F`` and ``PLW1514`` stay in ``[lint] select``
  2. The CI workflow's blocking step still invokes ``ruff check .``
  3. ``preview = true`` is set (required — PLW1514 is a preview rule
     in ruff 0.15.x)
  4. The config lives in ruff.toml and pyproject.toml has no shadowing
     ``[tool.ruff]`` table
  5. The F-group sunset list stays per-rule and free of stale entries

If someone removes any of these, CI stops enforcing UTF-8-explicit
opens and the F group, and we're back to the original traps.

**Config moved to ruff.toml on 2026-08-16.** When ruff.toml exists, ruff reads
it in preference to pyproject.toml and ignores ``[tool.ruff]`` there
**silently** — no warning, no error. So these tests read ruff.toml, and one of
them asserts the pyproject table never comes back as dead-but-live-looking
config. Design:
docs/superpowers/specs/2026-08-16-ruff-f-group-reland-design.md
"""

from __future__ import annotations

import pathlib

import pytest

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover — 3.10 and earlier
    import tomli as tomllib  # type: ignore

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
RUFF_TOML = REPO_ROOT / "ruff.toml"

#: Rules that must stay armed. "F" is the Pyflakes group; PLW1514 is
#: unspecified-encoding. Removing either is a deliberate act that must delete
#: its entry here in the same commit.
REQUIRED_SELECT = ("F", "PLW1514")


def _load_ruff_config() -> dict:
    assert RUFF_TOML.is_file(), (
        f"{RUFF_TOML} is missing.  ruff configuration lives in ruff.toml at "
        "the repo root, not in pyproject.toml.  If you meant to move it back, "
        "read TestRuffConfig.test_pyproject_has_no_ruff_table first — ruff "
        "ignores pyproject's [tool.ruff] silently when ruff.toml exists."
    )
    with open(RUFF_TOML, "rb") as fh:
        return tomllib.load(fh)


def _load_pyproject() -> dict:
    with open(REPO_ROOT / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)


class TestRuffConfig:
    @pytest.mark.parametrize("rule", REQUIRED_SELECT)
    def test_required_rule_is_in_select_list(self, rule: str):
        """ruff.toml must keep both PLW1514 and the F group in [lint] select."""
        selected = _load_ruff_config().get("lint", {}).get("select", [])
        assert rule in selected, (
            f"{rule} was removed from ruff.toml [lint] select (currently "
            f"{sorted(selected)}).  PLW1514 blocks bare open() calls that "
            "default to locale encoding on Windows; F is the Pyflakes group, "
            "which catches undefined names, unused imports and duplicate dict "
            "keys.  Dropping either disarms the gate while leaving CI green — "
            "exactly how the F group was lost in the v0.15.1 cutover.  If you "
            "genuinely want to remove it, delete it from REQUIRED_SELECT in "
            "the same commit so the intent is deliberate."
        )

    def test_preview_mode_enabled(self):
        """PLW1514 is a preview rule in ruff 0.15.x — preview=true is
        required for it to actually run."""
        ruff_cfg = _load_ruff_config()
        assert ruff_cfg.get("preview") is True, (
            "ruff.toml preview=true is required — PLW1514 is a preview "
            "rule and silently becomes a no-op without it.  If this ever "
            "becomes a stable rule, you can drop preview=true but must "
            "verify PLW1514 still fires in a sample test run first."
        )

    def test_pyproject_has_no_ruff_table(self):
        """A [tool.ruff] table in pyproject.toml would be dead config.

        When ruff.toml exists, ruff reads it in preference to pyproject.toml
        and ignores ``[tool.ruff]`` there **silently** — no warning, no error.
        A table added back would still grep as live lint config while having
        no effect at all.  Verified empirically 2026-08-16, not assumed.
        """
        assert "ruff" not in _load_pyproject().get("tool", {}), (
            "pyproject.toml has a [tool.ruff] table, but ruff.toml exists and "
            "takes precedence — ruff ignores the pyproject table SILENTLY.  "
            "Move any settings into ruff.toml and delete the table, or you "
            "will have lint config that looks live and does nothing."
        )


class TestFGroupSunsetList:
    """The per-file-ignores list is temporary scaffolding for the F re-land.

    It names the files that offended when enforcement was switched back on
    (2026-08-16) and is append-never, shrink-only.  These tests keep it honest
    while it burns down.  When it is empty, delete the block — and these two
    tests with it.
    """

    @staticmethod
    def _ignores() -> dict[str, list[str]]:
        return _load_ruff_config().get("lint", {}).get("per-file-ignores", {})

    def test_never_blanket_exempts_the_f_group(self):
        """Entries must name specific rules, never the whole ``F`` group.

        The list is per-rule on purpose: a file exempted for F401 stays gated
        on F821.  A bare ``"F"`` entry would turn the whole group off for that
        file and hide new bugs behind old ones.
        """
        blanket = sorted(p for p, codes in self._ignores().items() if "F" in codes)
        assert not blanket, (
            f"per-file-ignores entries blanket-exempt the whole F group: "
            f"{blanket}.  List the specific rules instead (e.g. "
            '["F401", "F841"]), so the other F rules keep checking these files.'
        )

    def test_has_no_stale_entries(self):
        """Every non-glob entry must name a file that still exists.

        Entries outlive the files they were written for — a rename or delete
        elsewhere leaves a line that exempts nothing and quietly pads the
        count of remaining work.
        """
        stale = sorted(
            p
            for p in self._ignores()
            if not any(ch in p for ch in "*?[") and not (REPO_ROOT / p).exists()
        )
        assert not stale, (
            f"ruff.toml per-file-ignores names {len(stale)} file(s) that no "
            f"longer exist: {stale}.  Delete the entries — they exempt nothing "
            "and inflate the apparent size of the remaining burn-down."
        )


class TestLintWorkflow:
    WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "lint.yml"

    def test_workflow_exists(self):
        assert self.WORKFLOW_PATH.exists(), (
            f"CI workflow missing: {self.WORKFLOW_PATH}"
        )

    def test_workflow_has_blocking_ruff_step(self):
        """The workflow must run a blocking ``ruff check .`` step
        (one without --exit-zero) so violations fail the job."""
        content = self.WORKFLOW_PATH.read_text(encoding="utf-8")
        # Look for the blocking step's named line + its command.  We want
        # at least one ``ruff check .`` that does NOT have ``--exit-zero``
        # nearby.
        # Split into lines and find ruff check invocations
        lines = content.splitlines()
        found_blocking = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("ruff check") and "--exit-zero" not in stripped:
                # Also check it's not piped to `|| true` which would mask
                # the exit code.
                window = " ".join(lines[i:i + 3])
                if "|| true" not in window:
                    found_blocking = True
                    break
        assert found_blocking, (
            "lint.yml no longer contains a blocking ``ruff check .`` step "
            "(one without --exit-zero and not masked by || true).  "
            "Restore it — the PLW1514 and F rules are only useful if CI "
            "actually fails on violation."
        )

    def test_workflow_yaml_is_valid(self):
        """Workflow file must parse as valid YAML (can't ship a broken
        CI config to main)."""
        import yaml
        content = self.WORKFLOW_PATH.read_text(encoding="utf-8")
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            pytest.fail(f"lint.yml is not valid YAML: {exc}")
        assert isinstance(parsed, dict)
        assert "jobs" in parsed


class TestPreCommitHook:
    """The pre-commit hook is the third enforcement surface after CI and the
    config itself.  It was missing entirely between the v0.15.1 cutover and
    the 2026-08-16 re-land."""

    CONFIG_PATH = REPO_ROOT / ".pre-commit-config.yaml"

    def test_ruff_hook_is_registered(self):
        import yaml

        parsed = yaml.safe_load(self.CONFIG_PATH.read_text(encoding="utf-8"))
        ids = {
            hook.get("id")
            for repo in parsed.get("repos", [])
            for hook in repo.get("hooks", [])
        }
        # Upstream renamed the lint hook from `ruff` to `ruff-check`; accept
        # either so a rev bump that flips the name is not a false failure.
        assert ids & {"ruff", "ruff-check"}, (
            "No ruff hook in .pre-commit-config.yaml (found "
            f"{sorted(i for i in ids if i)}).  Local commits then bypass the "
            "lint gate entirely and violations are only caught later, in CI."
        )
