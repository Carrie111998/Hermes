"""Regression: install.sh --commit must fail hard, never silently unpinned.

``install.sh --commit <short-sha>`` used to swallow the pin fetch with
``|| true`` and then run ``git checkout --detach`` unconditionally. With an
abbreviated sha the server refuses the fetch (``couldn't find remote ref``),
the checkout misparses the unknown name as a pathspec, and the installer still
printed success and exited 0 -- leaving the user on unpinned ``$BRANCH`` while
believing they were pinned (#87268).

The pin block must now: reject a non-40-char sha up front, fail hard when the
fetch is refused, verify the object resolves locally before checking out, and
abort if the checkout itself fails.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def _extract_commit_pin_block() -> str:
    text = INSTALL_SH.read_text(encoding="utf-8")
    match = re.search(
        r'(?P<block>if \[ -n "\$INSTALL_COMMIT" \]; then.*?\n    fi\n\n    log_success "Repository ready")',
        text,
        re.DOTALL,
    )
    assert match is not None, "commit-pin block not found in install.sh"
    return match["block"]


def test_pin_fetch_failure_is_not_swallowed() -> None:
    block = _extract_commit_pin_block()
    # The old `git fetch origin "$INSTALL_COMMIT" || true` masked the refusal.
    assert 'git fetch origin "$INSTALL_COMMIT" || true' not in block
    assert 'git fetch origin "$INSTALL_COMMIT"' in block


def test_pin_rejects_abbreviated_sha() -> None:
    block = _extract_commit_pin_block()
    # A full 40-char sha is required; anything shorter/non-hex is rejected early.
    assert '"${#INSTALL_COMMIT}" -eq 40' in block
    assert "must be a full 40-character commit sha" in block


def test_pin_verifies_object_before_checkout() -> None:
    block = _extract_commit_pin_block()
    # After the fetch, the object must resolve locally before any checkout, so
    # an unfetchable target aborts instead of being misparsed as a pathspec.
    cat_file_idx = block.rfind("git cat-file -e")
    checkout_idx = block.find("if ! git checkout --detach")
    assert cat_file_idx != -1 and checkout_idx != -1
    assert cat_file_idx < checkout_idx, "resolvability guard must precede checkout"


def test_pin_aborts_on_any_failure() -> None:
    block = _extract_commit_pin_block()
    # Every failure path in the pin must exit 1 rather than continue to a
    # successful-looking, unpinned install.
    assert block.count("exit 1") >= 4, "pin failures must all exit 1"
    # And the checkout itself is guarded (both the forced and normal paths).
    assert block.count("if ! git checkout --detach") == 2
