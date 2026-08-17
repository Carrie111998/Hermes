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
  5. ``per-file-ignores`` exempts NO F code anywhere — the sunset list that
     carried the re-land is burned down and deleted, so any F exemption is a
     regression rather than remaining backlog

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
import re

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


def _is_pyflakes_code(code: str) -> bool:
    """True for a Pyflakes "F" selector: bare ``F`` or ``F`` + digits.

    Deliberately NOT ``code.startswith("F")`` — ruff has several unrelated
    groups whose prefixes begin with F (FA, FBT, FIX, FLY, FURB), and treating
    ``FURB101`` as a Pyflakes code would make this guard reject a legitimate,
    unrelated exemption.
    """
    return code == "F" or (code[:1] == "F" and code[1:].isdigit())


class TestPerFileIgnores:
    """per-file-ignores must contain NO F-group exemption, anywhere.

    From 2026-08-16 to 2026-08-17 this table also carried a *sunset list*: 544
    per-rule entries naming every file that offended when F enforcement was
    switched back on, append-never and shrink-only.  Stages 2-5 burned it to
    zero and stage 6 deleted the block, so the only entries left are the four
    permanent PLW1514 exemptions.

    That makes a much stronger assertion available than the two tests this
    class replaces (no blanket ``"F"``, no stale entry): not "the exemptions
    are well-formed" but "there are no F exemptions at all".  Anything else is
    a regression — the burn-down is finished, and a file that cannot pass the
    F group is a bug to fix, not a line to add here.
    """

    @staticmethod
    def _ignores() -> dict[str, list[str]]:
        return _load_ruff_config().get("lint", {}).get("per-file-ignores", {})

    def test_no_f_code_is_exempted_anywhere(self):
        """No entry may exempt any F code — specific, or the bare group."""
        offenders = sorted(
            (path, sorted(c for c in codes if _is_pyflakes_code(c)))
            for path, codes in self._ignores().items()
            if any(_is_pyflakes_code(c) for c in codes)
        )
        assert not offenders, (
            f"ruff.toml per-file-ignores exempts F-group rules for "
            f"{len(offenders)} path(s): {offenders}.\n\n"
            "The F-group sunset list was burned down to zero and deleted in "
            "stage 6 (2026-08-17).  F is now clean tree-wide and gated in "
            "EVERY file, so re-adding an exemption here silently reopens the "
            "hole the whole re-land existed to close — and it reopens it for "
            "every rule you list, not just the one that failed.\n\n"
            "Fix the finding instead.  If a suppression is genuinely correct "
            "(a deliberate re-export, a PEP 562 __getattr__ __all__), use a "
            "line-level `# noqa: <code>` with a comment at the site, which "
            "stays visible in the file and cannot mask a future real bug "
            "elsewhere in it — that is how gateway/platforms/__init__.py "
            "handles its F822."
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


class TestRuffVersionPins:
    """The ruff BINARIES must all name one version.

    ``ruff.toml`` keeps the rule SET in sync for free -- adding a rule there
    arms the hook, CI and the local CLI at once.  The binaries have no such
    link: pre-commit builds an isolated env from ``rev:``, CI runs ``uv tool
    install``, and the local CLI is whatever is on PATH.  On 2026-08-17 those
    three sat on 0.15.12, latest-at-run-time (0.16.3) and 0.15.10.

    That drift was inert when measured -- all three resolved an identical
    44-rule set and all three passed the tree -- but that bounds rule
    MEMBERSHIP only.  A rule can change what it flags without changing whether
    it is enabled, and ``preview = true`` is exactly where ruff declines to
    promise stability across versions (PLW1514 is a preview rule).  With the F
    group gated tree-wide at zero suppressions, such a delta is a GATE delta:
    the same tree passes in one place and fails in another.

    A comment cannot hold this invariant -- one claiming the three were kept in
    step had already gone stale, and 94960380ea had to correct it.  So these
    tests hold it instead.

    **The environment surface is no longer unreachable (2026-08-17.)** This
    docstring used to end by conceding that the local CLI "is an environment no
    test can reach", and that concession was load-bearing: while it stood, the
    repo ``.venv`` sat on 0.15.10 -- installed from a ``pyproject.toml`` dev
    extra that had never been bumped -- while the hook and CI ran 0.15.12.  The
    15-minute whole-tree alarm in ``events/producers/ruff_gate_probe.py``
    resolves ruff off PATH, so which binary IT linted with depended on whether
    a session happened to have the venv activated.

    ``ruff.toml`` now carries ``required-version``, which ruff enforces itself:
    a mismatched binary refuses to load the config and exits 2, whoever invoked
    it.  That converts the environment from untestable to self-policing, and it
    is why a FIFTH declaration site (the dev extra) is now asserted below --
    the four this class knew about never included it.
    """

    PRE_COMMIT_PATH = REPO_ROOT / ".pre-commit-config.yaml"
    WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "lint.yml"

    #: ``uv tool install ruff`` with the ``==`` pin OPTIONAL.  Optional is the
    #: whole point: it makes an UNPINNED install match-and-then-fail rather
    #: than simply not match, and an install that fails to match is the one
    #: regression this guard exists to catch.  The trailing lookahead stops
    #: ``ruff-lsp`` and friends from reading as a bare ``ruff``.
    _UV_INSTALL_RUFF = re.compile(
        r"uv tool install ruff(?:==(?P<version>[\w.]+))?(?![\w.-])"
    )

    def _hook_version(self) -> str:
        """The ruff version the pre-commit hook builds, read off its ``rev:``."""
        import yaml

        parsed = yaml.safe_load(self.PRE_COMMIT_PATH.read_text(encoding="utf-8"))
        revs = [
            str(repo.get("rev", ""))
            for repo in parsed.get("repos", [])
            if "astral-sh/ruff-pre-commit" in str(repo.get("repo", ""))
        ]
        assert len(revs) == 1, (
            f"expected exactly one astral-sh/ruff-pre-commit entry in "
            f"{self.PRE_COMMIT_PATH.name}, found {len(revs)}: {revs}.  The CI "
            "pins cannot be compared until there is exactly one hook rev to "
            "compare them against."
        )
        # The hook rev is a git TAG (``v0.15.12``); a uv pin is a PyPI VERSION
        # (``0.15.12``).  Compare the versions, not the spellings.
        return revs[0].lstrip("v")

    def test_hook_rev_is_a_concrete_release(self):
        """``rev:`` must name a release tag, not a moving ref.

        pre-commit also accepts a branch or a SHA there.  A branch would put
        the floating-version problem back on the one surface that actually
        blocks commits, while still looking pinned.
        """
        version = self._hook_version()
        assert re.fullmatch(r"\d+\.\d+\.\d+", version), (
            f"the astral-sh/ruff-pre-commit rev resolves to {version!r}, which "
            "is not a concrete x.y.z release.  A branch or SHA makes the hook's "
            "ruff version unknowable from the file, so neither this test nor a "
            "human reader can tell whether CI and the local CLI agree with it."
        )

    def test_every_ci_ruff_install_pins_the_hook_version(self):
        """Every ``uv tool install ruff`` in lint.yml must pin the hook's ruff."""
        content = self.WORKFLOW_PATH.read_text(encoding="utf-8")
        matches = list(self._UV_INSTALL_RUFF.finditer(content))
        assert matches, (
            f"{self.WORKFLOW_PATH.name} no longer installs ruff via `uv tool "
            "install ruff`.  If the install moved to another mechanism, teach "
            "this test to read it -- otherwise CI's ruff version is unpinned "
            "and unverified again, which is the state this test was added to "
            "end."
        )

        expected = self._hook_version()
        mismatched = [
            m.group("version") for m in matches if m.group("version") != expected
        ]
        assert not mismatched, (
            f"{self.WORKFLOW_PATH.name} installs ruff "
            f"{[v or 'UNPINNED (latest at run time)' for v in mismatched]}, but "
            f"the pre-commit hook builds {expected}.  CI and the hook then "
            "enforce the same ruff.toml with different binaries, so one tree "
            "can pass one gate and fail the other -- and with the F group "
            "enforced tree-wide at zero suppressions, that is a gate delta, "
            "not a cosmetic one.\n\n"
            f"Fix by pinning every `uv tool install ruff` to =={expected}.  If "
            "you meant to move the whole toolchain instead, bump the hook rev, "
            "both lint.yml pins and the local CLI "
            f'(`python -m pip install "ruff=={expected}"`) in ONE commit, then '
            "re-run `ruff check --no-cache .`."
        )

    def test_ruff_toml_required_version_pins_the_hook_version(self):
        """``ruff.toml`` must pin the hook's ruff via ``required-version``.

        This is the only pin that reaches the ENVIRONMENT rather than a
        declaration.  The other assertions in this class compare files to files,
        so they stay green while the binary a developer or a Scheduled Task
        actually runs drifts underneath them -- which is exactly what happened
        to the repo .venv.  ruff enforcing its own config version is what makes
        that undetectable case detectable.
        """
        required = _load_ruff_config().get("required-version")
        expected = self._hook_version()
        assert required is not None, (
            "ruff.toml has no `required-version`.  Without it, nothing stops a "
            "binary of any version from linting this tree: the pins in "
            "pyproject.toml, .pre-commit-config.yaml and lint.yml are all "
            "DECLARATIONS, and the tests over them compare files to each other, "
            "never to the ruff that actually runs.  Add "
            f'`required-version = "=={expected}"`.'
        )
        assert required == f"=={expected}", (
            f"ruff.toml pins `required-version = {required!r}` but the "
            f"pre-commit hook builds {expected}.  ruff would then refuse to run "
            "for the hook itself -- exit 2, config load failure -- so this is a "
            "hard break rather than a silent drift.  Set them equal.\n\n"
            "Use an exact `==x.y.z` pin, not a range: a range re-admits the "
            "very version spread this class exists to forbid, while still "
            "looking pinned."
        )

    def test_dev_extra_pins_the_hook_version(self):
        """``pyproject.toml``'s dev extra is the FIFTH declaration site.

        It is the one that installs the repo ``.venv``, so a stale entry here
        does not merely misdocument the version -- it MANUFACTURES a mismatched
        binary on the next install.  It sat at 0.15.10 while every other site
        said 0.15.12, and the venv it produced was really running 0.15.10.
        """
        dev = _load_pyproject().get("project", {}).get("optional-dependencies", {}).get("dev", [])
        pins = [d for d in dev if re.fullmatch(r"ruff==[\w.]+", str(d).strip())]
        assert len(pins) == 1, (
            f"expected exactly one `ruff==x.y.z` entry in pyproject.toml's dev "
            f"extra, found {len(pins)}: {pins}.  An unpinned or missing ruff "
            "there installs whatever is latest into the repo .venv, which is "
            "the binary most local tooling resolves first."
        )
        expected = self._hook_version()
        actual = pins[0].split("==", 1)[1]
        assert actual == expected, (
            f"pyproject.toml's dev extra pins ruff {actual}, but the pre-commit "
            f"hook builds {expected}.  `uv sync` / `pip install -e .[dev]` then "
            "puts a mismatched ruff in the repo .venv, and ruff.toml's "
            "required-version makes every invocation from that venv fail to "
            f"load the config (exit 2).  Set it to =={expected}."
        )
