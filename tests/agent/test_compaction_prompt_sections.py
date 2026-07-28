"""The compaction instruction must name sections the template actually has.

The iterative-compaction prompt told the summarizer to 'Move items from
"In Progress" to "Completed Actions"' and 'CRITICAL: Update "## Active Task"'.
Neither section exists in the template — they were removed when it was
restructured, and the instruction was not. A summarizer told to update a
section that isn't there either wastes attention or invents one, and the
invented heading is then carried forward into every later compaction. H-14.

This asserts the two are consistent with each other, so the next template edit
that drops a section fails here rather than silently rotting the instruction.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "agent" / "context_compressor.py"
TEXT = SOURCE.read_text(encoding="utf-8")

# Headings the templates actually define.
TEMPLATE_SECTIONS = set(re.findall(r"^## (.+)$", TEXT, re.M))

# Section names the prose instructions tell the summarizer to update, written
# either as "## Name" or as a bare quoted "Name".
_INSTRUCTION_LINES = [
    line for line in TEXT.splitlines()
    if "Update the summary using this exact structure" in line
]


def test_the_instruction_line_is_still_findable():
    """If this fails the rest proves nothing — the prompt was reworded."""
    assert _INSTRUCTION_LINES, "compaction instruction line not found"


@pytest.mark.parametrize("missing", ["In Progress", "Active Task"])
def test_removed_sections_are_not_referenced(missing):
    """These two were deleted from the template but kept in the instruction."""
    assert missing not in TEMPLATE_SECTIONS, (
        f"'{missing}' is back in the template — re-check the instruction"
    )
    for line in _INSTRUCTION_LINES:
        assert f'"{missing}"' not in line and f'"## {missing}"' not in line, (
            f"the compaction instruction still orders an update to "
            f"'{missing}', which the template does not define"
        )


def test_every_section_the_instruction_names_exists():
    """The general rule, so a future edit cannot reintroduce the same rot."""
    for line in _INSTRUCTION_LINES:
        for name in re.findall(r'"(?:## )?([A-Z][A-Za-z &]+)"', line):
            if name in {"None"}:
                continue
            assert name in TEMPLATE_SECTIONS, (
                f"instruction references section '{name}', which is not one of "
                f"the template sections: {sorted(TEMPLATE_SECTIONS)}"
            )


def test_the_unfulfilled_input_requirement_survived():
    """The instruction's INTENT — keep the outstanding user request visible
    across compaction — must not be lost while fixing the section names."""
    joined = " ".join(_INSTRUCTION_LINES)
    assert "unfulfilled" in joined
    assert "Goal" in joined or "Active State" in joined
