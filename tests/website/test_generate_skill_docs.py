"""Tests for website/scripts/generate-skill-docs.py.

The generator turns every `skills/**/SKILL.md` into a Docusaurus page before
the `docs-site-checks` CI workflow runs `ascii-guard lint` on the result. If
a SKILL.md contains ASCII diagrams (box-drawing chars in a fenced code block)
without its own `<!-- ascii-guard-ignore -->` markers, the generator must
add them defensively — otherwise every PR touching `website/**` fails lint
on unrelated skill content.

Regression for issue #15305.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = REPO_ROOT / "website" / "scripts" / "generate-skill-docs.py"


@pytest.fixture(scope="module")
def gen_module():
    """Load generate-skill-docs.py as a module (hyphenated filename, not importable via normal import)."""
    spec = importlib.util.spec_from_file_location("generate_skill_docs", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_code_block_without_box_chars_is_not_wrapped(gen_module):
    """Plain bash/python code blocks should stay uncluttered."""
    body = "Intro.\n\n```bash\npip install foo\nfoo --run\n```\n\nOutro."
    result = gen_module.mdx_escape_body(body)
    assert "ascii-guard-ignore" not in result
    assert "pip install foo" in result


def test_code_block_with_box_chars_gets_wrapped(gen_module):
    """A code fence containing Unicode box-drawing chars must be wrapped in
    ascii-guard-ignore comments so the docs-site-checks lint can't fail on
    a skill's own diagram (issue #15305)."""
    body = (
        "Some text.\n\n"
        "```\n"
        "┌─────────┐\n"
        "│ diagram │\n"
        "└─────────┘\n"
        "```\n\n"
        "More text."
    )
    result = gen_module.mdx_escape_body(body)
    assert "<!-- ascii-guard-ignore -->" in result
    assert "<!-- ascii-guard-ignore-end -->" in result
    # The wrapper must sit OUTSIDE the fence, not inside.
    wrap_open = result.index("<!-- ascii-guard-ignore -->")
    fence_open = result.index("```\n┌")
    assert wrap_open < fence_open


def test_multiple_code_blocks_only_box_ones_wrapped(gen_module):
    """Mixed body: plain code stays plain, box code gets wrapped."""
    body = (
        "```bash\necho hi\n```\n\n"
        "```\n┌──┐\n│  │\n└──┘\n```\n\n"
        "```python\nprint('ok')\n```"
    )
    result = gen_module.mdx_escape_body(body)
    # exactly one wrap pair
    assert result.count("<!-- ascii-guard-ignore -->") == 1
    assert result.count("<!-- ascii-guard-ignore-end -->") == 1
    # plain blocks untouched
    assert "echo hi" in result
    assert "print('ok')" in result


def test_tilde_fenced_box_is_wrapped(gen_module):
    """The generator supports both ``` and ~~~ fences — both must be covered."""
    body = "~~~\n│ box │\n~~~"
    result = gen_module.mdx_escape_body(body)
    assert "<!-- ascii-guard-ignore -->" in result


def test_already_wrapped_source_double_wraps_harmlessly(gen_module):
    """If the SKILL.md already has ascii-guard-ignore markers, the generator's
    extra wrap is harmless (ascii-guard tolerates adjacent duplicate markers).
    The test just verifies we don't crash and the content survives."""
    body = (
        "<!-- ascii-guard-ignore -->\n"
        "```\n┌─┐\n└─┘\n```\n"
        "<!-- ascii-guard-ignore-end -->"
    )
    result = gen_module.mdx_escape_body(body)
    assert "┌─┐" in result
    # At least one marker pair survives
    assert "<!-- ascii-guard-ignore -->" in result
    assert "<!-- ascii-guard-ignore-end -->" in result


def test_box_drawing_detection_covers_common_chars(gen_module):
    """Smoke-test that the char set covers box-drawing ranges actually used
    in skill diagrams."""
    # Sample from real SKILL.md diagrams (segment-anything, research-paper-writing, etc.)
    for ch in "┌┐└┘─│├┤┬┴┼═║╔╗╚╝╭╮╯╰▶◀▲▼":
        assert ch in gen_module._BOX_DRAWING_CHARS, f"missing: {ch!r}"


def test_bundled_catalog_explains_missing_local_skills(gen_module):
    """The bundled catalog should explain how to restore a listed skill that
    was removed from the local profile's skills tree."""
    result = gen_module.build_catalog_md_bundled([])
    assert "respects local deletions and user edits" in result
    assert "hermes skills reset <name> --restore" in result


# --- _wrap_markdown_tables tests ---


def test_wrap_markdown_tables_no_tables_unchanged(gen_module):
    """Input with no markdown tables must pass through byte-for-byte unchanged.

    Regression for the _flush trailing-newline bug: the function was appending
    an extra '\\n' to every segment unconditionally, so plain text got an
    extra newline even when no tables were present.
    """
    for text in [
        "Hello world\n",
        "No tables here.\n\nJust paragraphs.\n",
        "",
        "Single line without newline",
        "```\ncode block\nno pipe lines\n```\n",
    ]:
        assert gen_module._wrap_markdown_tables(text) == text, (
            f"_wrap_markdown_tables changed non-table input: {text!r}"
        )


def test_wrap_markdown_tables_wraps_plain_table(gen_module):
    """A markdown table outside a code fence gets wrapped."""
    text = (
        "Some text.\n\n"
        "| Col A | Col B |\n"
        "|-------|-------|\n"
        "| 1     | 2     |\n\n"
        "More text."
    )
    result = gen_module._wrap_markdown_tables(text)
    assert "<!-- ascii-guard-ignore -->" in result
    assert "<!-- ascii-guard-ignore-end -->" in result
    assert "Some text." in result
    assert "More text." in result


def test_wrap_markdown_tables_skips_fenced_code_blocks(gen_module):
    """Pipe-delimited lines inside a ``` code block must not be wrapped."""
    text = (
        "```\n"
        "| header | row |\n"
        "|--------|-----|\n"
        "| a      | b   |\n"
        "```\n"
    )
    result = gen_module._wrap_markdown_tables(text)
    assert "ascii-guard-ignore" not in result


def test_wrap_markdown_tables_skips_tilde_fenced_code_blocks(gen_module):
    """Pipe-delimited lines inside a ~~~ code block must not be wrapped."""
    text = (
        "~~~\n"
        "| header | row |\n"
        "|--------|-----|\n"
        "| a      | b   |\n"
        "~~~\n"
    )
    result = gen_module._wrap_markdown_tables(text)
    assert "ascii-guard-ignore" not in result


def test_wrap_markdown_tables_mixed_fence_types(gen_module):
    """A ~~~ block containing ``` should not cause early termination."""
    text = (
        "~~~markdown\n"
        "```\n"
        "| header | row |\n"
        "|--------|-----|\n"
        "```\n"
        "~~~\n"
    )
    result = gen_module._wrap_markdown_tables(text)
    assert "ascii-guard-ignore" not in result


def test_wrap_markdown_tables_skips_existing_ignore_regions(gen_module):
    """Tables already inside ascii-guard-ignore markers are not double-wrapped."""
    text = (
        "<!-- ascii-guard-ignore -->\n"
        "| Col A | Col B |\n"
        "|-------|-------|\n"
        "| 1     | 2     |\n"
        "<!-- ascii-guard-ignore-end -->\n"
    )
    result = gen_module._wrap_markdown_tables(text)
    assert result.count("<!-- ascii-guard-ignore -->") == 1
    assert result.count("<!-- ascii-guard-ignore-end -->") == 1


def test_wrap_markdown_tables_wraps_table_outside_fence_but_not_inside(gen_module):
    """Only the table outside a code fence is wrapped; the one inside is untouched."""
    text = (
        "| real | table |\n"
        "|------|-------|\n"
        "| a    | b     |\n\n"
        "```\n"
        "| example | table |\n"
        "|---------|-------|\n"
        "| x       | y     |\n"
        "```\n"
    )
    result = gen_module._wrap_markdown_tables(text)
    assert result.count("<!-- ascii-guard-ignore -->") == 1
    assert result.count("<!-- ascii-guard-ignore-end -->") == 1
    # The real table is wrapped
    wrap_pos = result.index("<!-- ascii-guard-ignore -->")
    real_table_pos = result.index("| real | table |")
    assert wrap_pos < real_table_pos


def test_wrap_markdown_tables_longer_closing_fence(gen_module):
    """CommonMark allows a closing fence longer than the opener; table inside
    must not be wrapped."""
    text = (
        "```\n"
        "| header | row |\n"
        "|--------|-----|\n"
        "| a      | b   |\n"
        "````\n"
    )
    result = gen_module._wrap_markdown_tables(text)
    assert "ascii-guard-ignore" not in result


def test_render_skill_page_wraps_body_tables(gen_module):
    """render_skill_page must apply _wrap_markdown_tables to the body so that
    markdown tables in generated docs don't fail ascii-guard lint."""
    meta = {
        "slug": "test-skill",
        "category": "testing",
        "source_kind": "bundled",
        "rel_path": "testing/test-skill",
        "skill_md": "/dev/null",
    }
    fm = {
        "name": "test-skill",
        "description": "A test skill.",
    }
    body = (
        "Some text.\n\n"
        "| Col A | Col B |\n"
        "|-------|-------|\n"
        "| 1     | 2     |\n"
    )
    result = gen_module.render_skill_page(meta, fm, body)
    # The body table should be wrapped (in addition to the info_table which
    # render_skill_page wraps explicitly).
    # Count: 2 ignore regions — one for info_table, one for body table.
    assert result.count("<!-- ascii-guard-ignore -->") >= 2
