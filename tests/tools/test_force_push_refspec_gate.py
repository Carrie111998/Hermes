"""H-24 — `git push origin +main` must reach the force-push approval gate.

DANGEROUS_PATTERNS gated force push in exactly two spellings, `--forc[a-z]*`
and `-f\b`, and the surrounding comments show deliberate effort to catch
abbreviated long-flag forms. Git's third, fully equivalent spelling is a
leading `+` on the refspec: `git push origin +main` is `--force` for that ref.

No pattern matched it, so detect_dangerous_command returned False and the call
was auto-approved without ever reaching the approval gate. A model that has
learned `--force` prompts will reach for `+refspec` naturally, and the remote
branch is force-updated -- destroying any collaborator commits landed since the
last fetch, with no prompt and no remote-side recovery.
"""

from __future__ import annotations

import pytest

from tools.approval import detect_dangerous_command


def _flagged(cmd: str) -> bool:
    result = detect_dangerous_command(cmd)
    return bool(result[0]) if isinstance(result, tuple) else bool(result)


@pytest.mark.parametrize("cmd", [
    "git push origin +main",
    "git push origin +feature/auth",
    "git push  origin  +refs/heads/main",
    "git push upstream +HEAD:main",
    "cd /repo && git push origin +main",
])
def test_plus_refspec_force_push_is_gated(cmd):
    assert _flagged(cmd), f"force push via +refspec bypassed the gate: {cmd!r}"


@pytest.mark.parametrize("cmd", [
    "git push --force origin main",
    "git push -f origin main",
])
def test_existing_force_spellings_still_gated(cmd):
    assert _flagged(cmd)


@pytest.mark.parametrize("cmd", [
    "git push origin main",
    "git push",
    "git push origin HEAD:refs/heads/feature",
    "git push --set-upstream origin feature",
    "git commit -m 'a + b'",
    "git log --oneline",
])
def test_ordinary_pushes_are_not_gated(cmd):
    """Over-gating trains people to click through prompts; keep it precise."""
    assert not _flagged(cmd), f"false positive on a safe command: {cmd!r}"
