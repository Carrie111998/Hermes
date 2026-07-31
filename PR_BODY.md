fix(discord): render markdown tables as fenced box-drawing tables

## Problem

Discord does not render GitHub-flavored markdown pipe tables. The Discord adapter currently converts outbound tables to bold-heading + bullet groups (`convert_table_to_bullets`), which loses the tabular shape entirely: column relationships disappear, and multi-column comparisons (scores, benchmarks, config matrices) become long vertical lists that are hard to scan. Discord *does* render fenced code blocks in a monospace font, so a properly aligned ASCII table survives intact — including in narrow mobile views.

## Change

- `gateway/platforms/helpers.py`
  - Extracted the fence-aware table scan loop (previously the body of `convert_table_to_bullets`) into `_convert_tables(text, render_block)` so both renderers share one detector. Detection semantics are unchanged: a header row containing `|` immediately followed by a delimiter row matching `TABLE_SEPARATOR_RE`, with tables inside existing fenced code blocks left untouched.
  - Added `convert_table_to_codeblock()`: renders each detected table as a box-drawing (`┌─┬─┐ │ … └─┴─┘`) table inside a code fence. Column widths are computed with an East-Asian-width-aware `_display_width()` so CJK full/wide characters (width 2 in monospace fonts) keep columns aligned. Ragged rows are padded or clipped to the header's column count.
  - `convert_table_to_bullets()` keeps its exact behavior (Telegram still uses it) and now delegates to the shared scanner.
- `plugins/platforms/discord/adapter.py`: `format_message()` now calls `convert_table_to_codeblock()` instead of `convert_table_to_bullets()`.

## Correctness notes

- Tables already inside fenced code blocks are preserved verbatim (`_convert_tables` tracks fence state), so preformatted examples are never re-rendered.
- `TABLE_SEPARATOR_RE` requires at least one internal `|` in the delimiter row, so `---` horizontal rules and single-column pseudo-tables are not converted; prose containing a lone `|` short-circuits untouched.
- Long converted tables can exceed Discord's 2000-character message limit; `BasePlatformAdapter.truncate_message()` already splits on code-block boundaries and re-opens fences per chunk, so every chunk stays balanced (covered by a new test).
- Telegram behavior is unchanged: it imports `convert_table_to_bullets` and the shared primitives, all of which keep their signatures and output.

## Tests

- `tests/gateway/test_table_helpers.py`: new `TestConvertTableToCodeblock` — basic conversion, fenced-block preservation (both a whole-message fence and a fence preceding a real table), CJK alignment (all table lines have identical display width), ragged-row padding/clipping, pipe-in-prose and horizontal-rule non-matches, single-column non-conversion.
- `tests/gateway/test_discord_send.py`: `format_message()` end-to-end conversion, and a >2000-char generated table chunked by `truncate_message()` with every chunk under the limit and containing balanced fences.
- `tests/gateway/test_discord_format.py`: `test_table_converted_to_bullets` updated to `test_table_converted_to_codeblock`, asserting the new fenced-table output; the plain-text, fenced-block, and empty-string cases are unchanged.
- Suites run: `tests/gateway/` plus the discord/telegram plugin and tool tests. The gateway suite shows the same 36 environment-dependent failures (host-filesystem path completion, Telegram/Feishu SDK version drift, host session state) on this branch and on an unmodified `main` checkout; every test this PR adds or touches passes.
