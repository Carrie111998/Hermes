"""`hermes chat -Q` must not leak presentation output into stdout (#93220).

The quiet single-query branch lives inline in ``cli.py``'s ~21k-line main
flow (no standalone function to call), so these pin the branch's required
statements at the source level — the established convention for behavior
with no runtime mirror (see the install.ps1 source-text tests). Deleting
either live-agent neutralization reintroduces the leak this fixed.
"""

from __future__ import annotations

from pathlib import Path

import cli as cli_mod


def _quiet_branch(source: str) -> str:
    """Return the source slice of the quiet single-query branch."""
    anchor = 'if quiet:'
    start = source.index(
        '# Quiet mode: suppress banner, spinner, tool previews.'
    )
    # The branch runs from its anchor a couple of lines above the comment.
    anchor_idx = source.rindex(anchor, 0, start)
    # ...to the next statement at the branch's indent (the credentials
    # check's body ends before `effective_query` moves on); a generous
    # 2000-char window covers the whole branch body.
    return source[anchor_idx:anchor_idx + 2000]


def test_quiet_branch_clears_live_agent_reasoning_callback():
    source = Path(cli_mod.__file__).read_text(encoding="utf-8")
    branch = _quiet_branch(source)
    assert "cli.agent.reasoning_callback = None" in branch, (
        "-Q must clear the reasoning callback on the live agent: the "
        "callback was bound at agent construction from display.show_reasoning, "
        "so the Reasoning box keeps rendering into captured stdout without "
        "this (#93220)."
    )


def test_quiet_branch_syncs_tool_progress_off_to_agent():
    source = Path(cli_mod.__file__).read_text(encoding="utf-8")
    branch = _quiet_branch(source)
    assert "cli.agent.tool_progress_mode = \"off\"" in branch, (
        "-Q must sync tool_progress_mode to the live agent: the tool "
        "renderer reads agent.tool_progress_mode (initialized from "
        "display.tool_progress at construction), so tool diffs keep "
        "printing into captured stdout without this (#93220)."
    )


def test_quiet_branch_still_sets_cli_side_mode():
    source = Path(cli_mod.__file__).read_text(encoding="utf-8")
    branch = _quiet_branch(source)
    assert 'cli.tool_progress_mode = "off"' in branch
    # The live-agent neutralizations must come after the cli-side switch,
    # inside the same quiet branch.
    assert branch.index('cli.tool_progress_mode = "off"') < branch.index(
        "cli.agent.reasoning_callback = None"
    )
