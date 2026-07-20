"""Host-independent invariants for the ``browser`` install stage.

The behavioral coverage lives in the Pester suite at
``scripts/tests/test-install-ps1-browser-stage.ps1`` -- that suite dot-sources
install.ps1 with ``-Manifest`` (which loads all functions then returns), overrides
the side-effect functions (Test-Node, Install-AgentBrowser) in the child pwsh
process, invokes Stage-Browser directly, and asserts the ``$script:_StageSkippedReason``
output for each soft-skip path (Node-missing, disabled_toolsets block/inline/complex,
browser-not-disabled proceeding, no-config fresh install, npm-failure-to-skip).

This Python file retains ONLY the invariants that are genuinely
host-independent and CI-valuable on Linux runners that cannot execute
PowerShell:

* ``install.ps1`` stays pure ASCII (issues #66994 / #67000).
* The ``browser`` stage name appears in ``$InstallStages`` at all.
* Stage-Browser wraps ``Install-AgentBrowser`` in a try/catch that converts
  npm failures to a soft-skip (the behavioral Pester suite cannot easily
  trigger npm failure in isolation because ``Sync-EnvPath`` overwrites PATH
  from the registry at each stage, preventing npm stub injection; this
  targeted invariant pins the catch block exists and sets the skip channel).

The source-text regex suite that previously lived here was replaced per
review on #67835.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"


def _install_ps1() -> str:
    return INSTALL_PS1.read_text(encoding="utf-8")


def test_install_ps1_browser_stage_exists() -> None:
    """The ``browser`` stage name must appear in $InstallStages.

    Structural existence check only -- does NOT pin the field layout,
    ordering, or worker name.  The behavioral Pester suite verifies the
    actual stage-execution contract (JSON frame, soft-skip channels).
    """
    assert re.search(
        r'@\{\s*Name\s*=\s*"browser"',
        _install_ps1(),
        re.MULTILINE,
    ), (
        "$InstallStages must include a stage named 'browser'."
    )


def test_install_ps1_stage_browser_converts_npm_failure_to_skip() -> None:
    """Stage-Browser must catch Install-AgentBrowser failures as a soft-skip.

    Install-AgentBrowser throws on npm non-zero exit (L374-379).
    bootstrap-runner.ts:973-976 aborts on any stage that re-throws.
    So Stage-Browser MUST wrap the call in try/catch and convert the failure
    to ``$script:_StageSkippedReason`` instead of letting it propagate.

    The behavioral Pester suite cannot easily trigger the npm-failure path
    in isolation (Sync-EnvPath restores PATH from the registry at each
    stage, preventing npm-stub injection). This targeted invariant verifies
    the try/catch structure exists so the skip conversion can't be removed
    without breaking CI.
    """
    text = _install_ps1()
    m = re.search(r"function\s+Stage-Browser\s*\{(?P<body>.*?)\n\}", text, re.DOTALL)
    assert m, "Stage-Browser not defined"
    body = m.group("body")
    assert re.search(r"try\s*\{", body), (
        "Stage-Browser must wrap Install-AgentBrowser in a try block so "
        "npm failures don't abort the desktop bootstrap pipeline."
    )
    assert re.search(r"catch\s*\{", body), (
        "Stage-Browser must catch Install-AgentBrowser failures and convert "
        "them to a soft-skip via $script:_StageSkippedReason."
    )
    assert re.search(r"_StageSkippedReason", body), (
        "Stage-Browser's catch block must populate $script:_StageSkippedReason "
        "so the JSON frame emits skipped=true / ok=true."
    )


def test_install_ps1_keeps_pure_ascii() -> None:
    """install.ps1 must be pure ASCII (regression guard for #66994 / #67000).

    Windows PowerShell 5.1 reads a BOM-less .ps1 in CP1252 if any byte is
    non-ASCII, and misdecodes multibyte characters inside double-quoted
    strings.
    """
    raw = INSTALL_PS1.read_bytes()
    offenders: list[int] = []
    line_no = 1
    for byte in raw:
        if byte == 0x0A:
            line_no += 1
        elif byte >= 0x80:
            offenders.append(line_no)
    assert not offenders, (
        "scripts/install.ps1 must be pure ASCII. Non-ASCII byte(s) on line(s): "
        f"{sorted(set(offenders))}."
    )
