---
name: 3-statement-model
description: Build fully-integrated 3-statement models (IS, BS, CF) in Excel with working capital schedules, D&A roll-forwards, debt schedule, and the plugs that make cash and retained earnings tie. Pairs with excel-author.
version: 1.0.0
author: Anthropic (adapted by Nous Research)
license: Apache-2.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [finance, three-statement, income-statement, balance-sheet, cash-flow, excel, openpyxl, modeling]
    related_skills: [excel-author, pptx-author, dcf-model, lbo-model]
    requires: [excel-author]
---

## Environment

This skill assumes **headless openpyxl** — you are producing an .xlsx file on disk.
Follow the `excel-author` skill's conventions for cell coloring, formulas, named ranges, and sensitivity tables.
Recalculate before delivery: `python /path/to/excel-author/scripts/recalc.py ./out/model.xlsx`.

> **Hard dependency — `excel-author` (required, not just related).** `recalc.py` ships in the sibling `excel-author` skill at `excel-author/scripts/recalc.py`; it is NOT bundled here. If you installed `3-statement-model` standalone without `excel-author`, every recalc step will fail with a missing-script error. Install it first: `hermes skills install excel-author`.

# 3-Statement Financial Model Template Completion

Complete and populate integrated financial model templates with proper linkages between Income Statement, Balance Sheet, and Cash Flow Statement.

## ⚠️ CRITICAL PRINCIPLES — Read Before Populating Any Template

**Formulas over hardcodes (non-negotiable):**
- Every projection cell, roll-forward, linkage, and subtotal MUST be an Excel formula — never a pre-computed value
- When using Python/openpyxl: write formula strings (`ws["D15"] = "=D14*(1+Assumptions!$B$5)"`), NOT computed results (`ws["D15"] = 12500`)
- The ONLY cells that should contain hardcoded numbers are: (1) historical actuals, (2) assumption drivers in the Assumptions tab
- If you find yourself computing a value in Python and writing the result to a cell — STOP. Write the formula instead.
- Why: the model must flex when scenarios toggle or assumptions change. Hardcodes break every downstream integrity check silently.

**Verify step-by-step with the user:**
1. **After mapping the template** → show the user which tabs/sections you've identified and confirm before touching any cells
2. **After populating historicals** → show the user the historical block and confirm values/periods match source data
3. **After building IS projections** → run the subtotal checks, show the user the projected IS, confirm before moving to BS
4. **After building BS** → show the user the balance check (Assets = L+E) for every period, confirm before moving to CF
5. **After building CF** → show the user the cash tie-out (CF ending cash = BS cash), confirm before finalizing
6. **Do NOT populate the entire model end-to-end and present it complete** — break at each statement, show the work, catch errors early

## Routing — where the detail lives

These files are NOT auto-loaded; open the one the task needs.

| If you need to... | Read |
|---|---|
| Map the template's tabs/rows/columns/named ranges, then populate it safely (Steps 1-6: analyze structure, enter data without breaking formulas, validate formulas, per-sheet quality checks, cross-statement integrity, final review) | `references/template-workflow.md` |
| Look up any formula: core linkages, gross profit, margins, credit metrics, % -of-revenue forecasts, WC / D&A / debt / RE / NOL schedules, IS-BS-CF structures, check formulas — plus Margin Analysis and Credit Metrics sections (only if the user or template asks for them) | `references/formulas.md` |
| Apply fonts/fills/borders, bold total rows, BS check-row conditional format, margin & credit-metric formats, threshold colors, reasonability flags, and the blue/grey palette | `references/formatting.md` |
| Set up scenarios (Base/Upside/Downside) or build the Checks tab: all 9+ check categories, master check formula, quick debug workflow | `references/validation.md` |
| Pull historicals out of 10-K / 10-Q filings (EDGAR lookup, currency, line-item mapping, notes) | `references/sec-filings.md` |

## Shortest End-to-End Skeleton

Build order — one pass, stopping at each arrow to verify with the user per the step-by-step rule above:

```
Assumptions (drivers, scenario toggle)
  → IS (Net Revenue → COGS → Gross Profit → OpEx → EBITDA/EBIT → Interest → EBT → NOL → Taxes → Net Income)
  → Supporting schedules (WC, D&A/PP&E, Debt, NOL)
  → BS (Cash from CF, AR/Inv/AP from WC, PP&E from D&A, Debt from Debt schedule, RE roll-forward)
  → CF (NI from IS + non-cash add-backs + ΔWC → CFO; CapEx → CFI; debt/equity/dividends → CFF; Ending Cash → BS Cash)
  → Checks tab (every check below must read 0 / PASS in every scenario)
```

## Formatting

Default palette, fonts, number formats and threshold colors: `references/formatting.md`.

## SEC Filings Data Extraction

If the template specifically requires pulling data from SEC filings (10-K, 10-Q), see [references/sec-filings.md](references/sec-filings.md) for detailed extraction guidance. This reference is only needed when populating templates with public company data from regulatory filings.

## Red Lines — Linkages, Signs, Circularity

### Core Linkages (Must Always Hold)

See [references/formulas.md](references/formulas.md) for all formula details.

| Check | Formula | Expected Result |
|-------|---------|-----------------|
| Balance Sheet Balance | Assets - Liabilities - Equity | = 0 |
| Cash Tie-Out | CF Ending Cash - BS Cash | = 0 |
| Cash Monthly vs Annual | Closing Cash (Monthly) - Closing Cash (Annual) | = 0 |
| Net Income Link | IS Net Income - CF Starting Net Income | = 0 |
| Retained Earnings | Prior RE + NI + SBC - Dividends - BS Ending RE | = 0 |
| Equity Financing | ΔCommon Stock/APIC (BS) - Equity Issuance (CFF) | = 0 |
| Year 0 Equity | Equity Raised (Year 0) - Beginning Equity Capital (Year 1) | = 0 |

### Sign Convention Reference

| Statement | Item | Sign Convention |
|-----------|------|-----------------|
| CFO | D&A, SBC | Positive (add-back) |
| CFO | ΔAR (increase) | Negative (use of cash) |
| CFO | ΔAP (increase) | Positive (source of cash) |
| CFI | CapEx | Negative |
| CFF | Debt issuance | Positive |
| CFF | Debt repayments | Negative |
| CFF | Dividends | Negative |

### Circular Reference Handling

Interest expense creates circularity: Interest → Net Income → Cash → Debt Balance → Interest

Enable iterative calculation in Excel: File → Options → Formulas → Enable iterative calculation. Set maximum iterations to 100, maximum change to 0.001. Add a circuit breaker toggle in Assumptions tab.

## Data sources — MCP first, web fallback

Many passages below say "use the S&P Kensho MCP / Daloopa MCP / FactSet MCP". Those are commercial financial-data MCPs from the original Cowork plugin context. In Hermes:

- **If you have any structured financial-data MCP configured** (Hermes supports MCP — see `native-mcp` skill), prefer it for point-in-time comps, precedent transactions, and filings.
- **Otherwise**, fall back to:
  - `web_search` / `web_extract` against SEC EDGAR (`https://www.sec.gov/cgi-bin/browse-edgar`) for US filings
  - Company IR pages for press releases, earnings decks
  - `browser_navigate` for interactive data portals
  - User-provided data (explicitly ask when the context doesn't have it)
- **Never fabricate**. If a multiple, precedent, or filing number can't be sourced, flag the cell as `[UNSOURCED]` and surface it to the user.

## Attribution

This skill is adapted from Anthropic's Claude for Financial Services plugin suite (Apache-2.0). The Office-JS / Cowork live-Excel paths have been removed; this version targets headless openpyxl via the `excel-author` skill's conventions. Original: https://github.com/anthropics/financial-services
