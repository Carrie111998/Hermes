---
name: dcf-model
description: Build institutional-quality DCF valuation models in Excel — revenue projections, FCF build, WACC, terminal value, Bear/Base/Bull scenarios, 5x5 sensitivity tables. Pairs with excel-author. Use for intrinsic-value equity analysis.
version: 1.0.0
author: Anthropic (adapted by Nous Research)
license: Apache-2.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [finance, valuation, dcf, excel, openpyxl, modeling, investment-banking]
    related_skills: [excel-author, pptx-author, comps-analysis, lbo-model, 3-statement-model]
    requires: [excel-author]
---

# DCF Model Builder

## Overview

This skill creates institutional-quality DCF models for equity valuation following investment banking standards. Each analysis produces a detailed Excel model (with sensitivity analysis included at the bottom of the DCF sheet).

## When to use

Use this skill when the request is intrinsic-value equity analysis: "build a DCF for X", "what's X worth", "value X on a 5-year unlevered FCF basis", "run Bear/Base/Bull cases", "give me a WACC vs terminal growth sensitivity table".

Do NOT use it for relative valuation (use `comps-analysis`), leveraged buyout returns (use `lbo-model`), or full financial statement forecasting (use `3-statement-model`). For generic spreadsheet mechanics, use `excel-author` directly.

## Environment

This skill assumes **headless openpyxl** — you are producing an .xlsx file on disk.
Follow the `excel-author` skill's conventions for cell coloring, formulas, named ranges, and sensitivity tables.
Recalculate before delivery: `python /path/to/excel-author/scripts/recalc.py ./out/model.xlsx`.

> **Hard dependency — `excel-author` (required, not just related).** `recalc.py` ships in the sibling `excel-author` skill at `excel-author/scripts/recalc.py`; it is NOT bundled here. If you installed `dcf-model` standalone without `excel-author`, every recalc step below will fail with a missing-script error. Install it first: `hermes skills install excel-author`.

## Tools

- Default to using all of the information provided by the user and MCP servers available for data sourcing.
- `scripts/validate_dcf.py` — structural validator for a produced workbook.
- `requirements.txt` — Python dependencies for this skill.

## Reference routing

Read only the reference you need — these files are NOT loaded until you request them.

| Intent / task at hand | Read |
|---|---|
| Source data, analyze history, project revenue, model OpEx, build unlevered FCF (Steps 1-5); MCP-vs-web data sourcing policy | `references/workflow-data-and-projections.md` |
| Derive cost of equity/debt, capital structure weights, WACC ranges, discount factors and mid-year convention (Steps 6-7) | `references/wacc-and-discounting.md` |
| Terminal value (perpetuity or exit multiple), TV sanity checks, EV → equity value → per-share bridge (Steps 8-9) | `references/terminal-value-and-equity-bridge.md` |
| Build the three 5×5 sensitivity tables, axis construction, per-cell formula pattern, forbidden shortcuts, where they live on the sheet (Step 10) | `references/sensitivity-tables.md` |
| Lay out Bear/Base/Bull assumption blocks, the consolidation column with INDEX, case selector, per-case assumption philosophy | `references/scenarios-and-case-selector.md` |
| Correct FCF formula references, cell comment source format, row-planning process | `references/correct-patterns.md` |
| What NOT to do: wrong patterns, top-5 errors, WACC / growth / terminal value / cash flow error catalog | `references/common-mistakes.md` |
| Sheet architecture, formatting + color + border + number-format standards, DCF sheet row-by-row layout, WACC sheet layout, input requirements, deliverables | `references/excel-model-structure.md` |
| Run recalc.py and read its JSON, quality rubric, best practices, workflow integration, pre-delivery checklist | `references/recalc-and-delivery-qa.md` |
| High-growth tech, mature, cyclical or multi-segment company adjustments | `references/company-type-variations.md` |
| Formula errors, #REF!/#DIV/0!, or unreasonable valuation output | `TROUBLESHOOTING.md` |

## Critical Constraints - Read These First

These constraints apply throughout all DCF model building. Review before starting:

**Formulas Over Hardcodes (NON-NEGOTIABLE):**
- Every projection, margin, discount factor, PV, and sensitivity cell MUST be a live Excel formula — never a value computed in Python and written as a number
- When using openpyxl: `ws["D20"] = "=D19*(1+$B$8)"` is correct; `ws["D20"] = calculated_revenue` is WRONG
- The only hardcoded numbers permitted are: (1) raw historical inputs, (2) assumption drivers (growth rates, WACC inputs, terminal g), (3) current market data (share price, debt balance)
- If you catch yourself computing something in Python and writing the result — STOP. The model must flex when the user changes an assumption.

**Verify Step-by-Step With the User (DO NOT build end-to-end):**
- After data retrieval → show the user the raw inputs block (revenue, margins, shares, net debt) and confirm before projecting
- After revenue projections → show the projected top line and growth rates, confirm before building margin build
- After FCF build → show the full FCF schedule, confirm logic before computing WACC
- After WACC → show the calculation and inputs, confirm before discounting
- After terminal value + PV → show the equity bridge (EV → equity value → per share), confirm before sensitivity tables
- Catch errors at each stage — a wrong margin assumption discovered after sensitivity tables are built means rebuilding everything downstream

**Sensitivity Tables:**
- **Use an ODD number of rows and columns** (standard: 5×5, sometimes 7×7) — this guarantees a true center cell
- **Center cell = base case.** Build the axis values so the middle row header and middle column header exactly equal the model's actual assumptions (e.g., if base WACC = 9.0%, the middle row is 9.0%; if terminal g = 3.0%, the middle column is 3.0%). The center cell's output must therefore equal the model's actual implied share price — this is the sanity check that the table is built correctly.
- **Highlight the center cell** with the medium-blue fill (`#BDD7EE`) + bold font so it's immediately visible which cell is the base case.
- Populate ALL cells (typically 3 tables × 25 cells = 75) with full DCF recalculation formulas
- Use openpyxl loops to write formulas programmatically
- NO placeholder text, NO linear approximations, NO manual steps required
- Each cell must recalculate full DCF for that assumption combination

**Cell Comments:**
- Add cell comments AS each hardcoded value is created
- Format: "Source: [System/Document], [Date], [Reference], [URL if applicable]"
- Every blue input must have a comment before moving to next section
- Do not defer to end or write "TODO: add source"

**Model Layout Planning:**
- Define ALL section row positions BEFORE writing any formulas
- Write ALL headers and labels first
- Write ALL section dividers and blank rows second
- THEN write formulas using the locked row positions
- Test formulas immediately after creation

**Formula Recalculation:**
- Run `python recalc.py model.xlsx 30` before delivery
- Fix ALL errors until status is "success"
- Zero formula errors required (#REF!, #DIV/0!, #VALUE!, etc.)

**Scenario Blocks:**
- Create separate blocks for Bear/Base/Bull cases
- Show assumptions horizontally across projection years within each block
- Use IF formulas: `=IF($B$6=1,[Bear cell],IF($B$6=2,[Base cell],[Bull cell]))`
- Verify formulas reference correct scenario block cells

## End-to-end skeleton walkthrough

The shortest complete path. Each numbered step names the reference to open when you reach it; pause for user confirmation at the checkpoints listed in Critical Constraints.

1. **Plan the sheet layout first.** Two sheets (DCF, WACC); sensitivity tables at the BOTTOM of the DCF sheet. Lock every section's row positions, write all headers/labels/dividers, and only then write formulas. → `references/excel-model-structure.md`
2. **Retrieve and validate data** (MCP first, then web/user data): historicals, share price, diluted shares, total debt, cash, beta, risk-free rate. Add a source cell comment to each hardcoded input as you write it. → `references/workflow-data-and-projections.md`
3. **Write the Bear/Base/Bull assumption blocks** (growth %, EBIT margin, tax, D&A %, CapEx %, ΔNWC %, terminal g, WACC) laid out horizontally across FY1-FY5, each with a section header row AND a year column header row. Add the case selector cell and a consolidation column of `=INDEX(...,1,$B$6)` formulas. → `references/scenarios-and-case-selector.md`
4. **Project revenue → gross profit → OpEx (as % of revenue) → EBIT → taxes → NOPAT**, every cell referencing the consolidation column: `=E29*(1+$E$10)`. → `references/workflow-data-and-projections.md`
5. **Build unlevered FCF:** NOPAT + D&A − CapEx − ΔNWC, with the % drivers pointed at the consolidation column. → `references/correct-patterns.md`
6. **Build the WACC sheet:** CAPM cost of equity, after-tax cost of debt, market-value weights, WACC output; link it back into the DCF sheet in green font. → `references/wacc-and-discounting.md`
7. **Discount:** periods 0.5, 1.5, 2.5 … (mid-year convention), discount factor `=1/(1+WACC)^period`, PV of each FCF. → `references/wacc-and-discounting.md`
8. **Terminal value and equity bridge:** terminal FCF / (WACC − g), discount it, sum PVs → EV → less net debt → equity value → ÷ diluted shares → implied price and upside. Sanity check TV = 50-70% of EV. → `references/terminal-value-and-equity-bridge.md`
9. **Write the three 5×5 sensitivity tables** (WACC vs terminal g; revenue growth vs EBIT margin; beta vs risk-free rate) with an openpyxl loop — 75 full-recalc formulas, base case in the highlighted center cell. → `references/sensitivity-tables.md`
10. **Format, recalc, verify, deliver:** blue inputs / black formulas / green links, blue-grey fills, section borders, number formats; run `python recalc.py model.xlsx 30` until `status: "success"`; walk the pre-delivery checklist; save as `[Ticker]_DCF_Model_[Date].xlsx`. → `references/recalc-and-delivery-qa.md`

## Troubleshooting

**If you encounter errors or unreasonable results, read [TROUBLESHOOTING.md](./TROUBLESHOOTING.md) for detailed debugging guidance.**

## Attribution

This skill is adapted from Anthropic's Claude for Financial Services plugin suite (Apache-2.0). The Office-JS / Cowork live-Excel paths have been removed; this version targets headless openpyxl via the `excel-author` skill's conventions. Original: https://github.com/anthropics/financial-services
