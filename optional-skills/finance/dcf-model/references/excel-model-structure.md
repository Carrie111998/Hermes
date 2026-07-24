# Excel Model Structure, Formatting Standards and Sheet Layouts

## Excel File Creation

**This skill uses the `xlsx` skill for all spreadsheet operations.** The xlsx skill provides:
- Standardized formula construction rules
- Number formatting conventions
- Automated formula recalculation via `recalc.py` script
- Comprehensive error checking and validation

All Excel files created by this skill must follow xlsx skill requirements, including zero formula errors and proper recalculation.

## Input Requirements

### Minimum Required Inputs
1. **Company identifier**: Ticker symbol or company name
2. **Growth assumptions**: Revenue growth rates for projection period (or "use consensus")
3. **Optional parameters**:
   - Projection period (default: 5 years)
   - Scenario cases (Bear/Base/Bull growth and margin assumptions)
   - Terminal growth rate (default: 2.5-3.0%)
   - Specific WACC inputs if not using CAPM

## Excel Model Structure

### Sheet Architecture

Create **two sheets**:

1. **DCF** - Main valuation model with sensitivity analysis at bottom
2. **WACC** - Cost of capital calculation

**CRITICAL**: Sensitivity tables go at the BOTTOM of the DCF sheet (not on a separate sheet). This keeps all valuation outputs together.

### Formatting Standards

**IMPORTANT**: Follow the xlsx skill for formula construction rules and number formatting conventions. The DCF skill adds specific visual presentation standards.

**Color Scheme - Two Layers**:

**Layer 1: Font Colors (MANDATORY from xlsx skill)**
- **Blue text (RGB: 0,0,255)**: ALL hardcoded inputs (stock price, shares, historical data, assumptions)
- **Black text (RGB: 0,0,0)**: ALL formulas and calculations
- **Green text (RGB: 0,128,0)**: Links to other sheets (WACC sheet references)

**Layer 2: Fill Colors — Professional Blue/Grey Palette (Default unless user specifies otherwise)**
- **Keep it minimal** — use only blues and greys for fills. Do NOT introduce greens, yellows, oranges, or multiple accent colors. A model with too many colors looks amateurish.
- **Default fill palette:**
  - **Section headers**: Dark blue (RGB: 31,78,121 / `#1F4E79`) background with white bold text
  - **Sub-headers/column headers**: Light blue (RGB: 217,225,242 / `#D9E1F2`) background with black bold text
  - **Input cells**: Light grey (RGB: 242,242,242 / `#F2F2F2`) background with blue font — or just white with blue font if you want maximum minimalism
  - **Calculated cells**: White background with black font
  - **Output/summary rows** (per-share value, EV, etc.): Medium blue (RGB: 189,215,238 / `#BDD7EE`) background with black bold font
- **That's it — 3 blues + 1 grey + white.** Resist the urge to add more.
- User-provided templates or explicit color preferences ALWAYS override these defaults.

**How the layers work together:**
- Input cell: Blue font + light grey fill = "Hardcoded input"
- Formula cell: Black font + white background = "Calculated value"
- Sheet link: Green font + white background = "Reference from another sheet"
- Key output: Black bold font + medium blue fill = "This is the answer"

**Font color tells you WHAT it is (input/formula/link). Fill color tells you WHERE you are (header/data/output).**

### Border Standards (REQUIRED for Professional Appearance)

**Thick borders** (1.5pt) around major sections:
- KEY INPUTS section
- PROJECTION ASSUMPTIONS section
- 5-YEAR CASH FLOW PROJECTION section
- TERMINAL VALUE section
- VALUATION SUMMARY section
- Each SENSITIVITY ANALYSIS table

**Medium borders** (1pt) between sub-sections:
- Company Details vs Historical Performance
- Growth Assumptions vs EBIT Margin vs FCF Parameters

**Thin borders** (0.5pt) around data tables:
- Scenario assumption tables (Bear | Base | Bull | Selected)
- Historical vs projected financials matrix

**No borders:** Individual cells within tables (keep clean, scannable)

**Borders are mandatory** - models without professional borders are not client-ready.

**Number Formats** (follows xlsx skill standards):
- **Years**: Format as text strings (e.g., "2024" not "2,024")
- **Percentages**: `0.0%` (one decimal place)
- **Currency**: `$#,##0` for millions; `$#,##0.00` for per-share - ALWAYS specify units in headers ("Revenue ($mm)")
- **Zeros**: Use number formatting to make all zeros "-" (e.g., `$#,##0;($#,##0);-`)
- **Large numbers**: `#,##0` with thousands separator
- **Negative numbers**: `(#,##0)` in parentheses (NOT minus sign)

**Cell Comments (MANDATORY for all hardcoded inputs)**:

Per the xlsx skill, ALL hardcoded values must have cell comments documenting the source. Format: "Source: [System/Document], [Date], [Reference], [URL if applicable]"

**CRITICAL**: Add comments AS CELLS ARE CREATED. Do not defer to the end.

### DCF Sheet Detailed Structure

**Section 1: Header**
```csv
Row,Content
1,[Company Name] DCF Model
2,Ticker: [XXX] | Date: [Date] | Year End: [FYE]
3,Blank
4,Case Selector Cell (1=Bear 2=Base 3=Bull)
5,Case Name Display (formula: =IF([Selector]=1"Bear"IF([Selector]=2"Base""Bull")))
```

**Section 2: Market Data (NOT case dependent)**
```csv
Item,Value
Current Stock Price,$XX.XX
Shares Outstanding (M),XX.X
Market Cap ($M),[Formula]
Net Debt ($M),XXX [or Net Cash if negative]
```

**Section 3: DCF Scenario Assumptions**

Create separate assumption blocks for each scenario (Bear, Base, Bull) with DCF-specific assumptions (Revenue Growth %, EBIT Margin %, Tax Rate %, D&A % of Revenue, CapEx % of Revenue, NWC Change % of ΔRev, Terminal Growth Rate, WACC) laid out horizontally across projection years. Each block must include section header, column header row showing the projection years (FY1, FY2, etc.), and data rows. See `<correct_patterns>` section "Correct Assumption Table Structure" for the exact layout.

**Section 4: Historical & Projected Financials**

**Reference a consolidation column (e.g., "Selected Case") that pulls from scenario blocks**, not scattered IF formulas in every projection row.

```csv
Income Statement ($M),2020A,2021A,2022A,2023A,2024E,2025E,2026E
Revenue,XXX,XXX,XXX,XXX,[=E29*(1+$E$10)],[=F29*(1+$E$11)],[=G29*(1+$E$12)]
  % growth,XX%,XX%,XX%,XX%,[=E29/D29-1],[=F29/E29-1],[=G29/F29-1]
,,,,,,
Gross Profit,XXX,XXX,XXX,XXX,[=E29*E33],[=F29*F33],[=G29*G33]
  % margin,XX%,XX%,XX%,XX%,[=E33/E29],[=F33/F29],[=G33/G29]
,,,,,,
Operating Expenses:,,,,,,,
  S&M,XXX,XXX,XXX,XXX,[=E29*0.15],[=F29*0.14],[=G29*0.13]
  R&D,XXX,XXX,XXX,XXX,[=E29*0.12],[=F29*0.11],[=G29*0.10]
  G&A,XXX,XXX,XXX,XXX,[=E29*0.08],[=F29*0.07],[=G29*0.07]
  Total OpEx,XXX,XXX,XXX,XXX,[=E36+E37+E38],[=F36+F37+F38],[=G36+G37+G38]
,,,,,,
EBIT,XXX,XXX,XXX,XXX,[=E33-E39],[=F33-F39],[=G33-G39]
  % margin,XX%,XX%,XX%,XX%,[=E41/E29],[=F41/F29],[=G41/G29]
,,,,,,
Taxes,(XX),(XX),(XX),(XX),[=E41*$E$24],[=F41*$E$24],[=G41*$E$24]
  Tax rate,XX%,XX%,XX%,XX%,[=E43/E41],[=F43/F41],[=G43/G41]
,,,,,,
NOPAT,XXX,XXX,XXX,XXX,[=E41-E43],[=F41-F43],[=G41-G43]
```

**Key Formula Pattern**:
- Revenue growth: `=E29*(1+$E$10)` where $E$10 is consolidation column for Year 1 growth
- NOT: `=E29*(1+IF($B$6=1,$B$10,IF($B$6=2,$C$10,$D$10)))`

This approach is cleaner, easier to audit, and prevents formula errors by centralizing the scenario logic.

**Section 5: Free Cash Flow Build**

**CRITICAL**: Verify row references point to the CORRECT assumption rows. Test formulas immediately after creation.

```csv
Cash Flow ($M),2020A,2021A,2022A,2023A,2024E,2025E,2026E
NOPAT,XXX,XXX,XXX,XXX,[=E45],[=F45],[=G45]
(+) D&A,XXX,XXX,XXX,XXX,[=E29*$E$21],[=F29*$E$21],[=G29*$E$21]
    % of Rev,XX%,XX%,XX%,XX%,[=E58/E29],[=F58/F29],[=G58/G29]
(-) CapEx,(XX),(XX),(XX),(XX),[=E29*$E$22],[=F29*$E$22],[=G29*$E$22]
    % of Rev,XX%,XX%,XX%,XX%,[=E60/E29],[=F60/F29],[=G60/G29]
(-) Δ NWC,(XX),(XX),(XX),(XX),[=(E29-D29)*$E$23],[=(F29-E29)*$E$23],[=(G29-F29)*$E$23]
    % of Δ Rev,XX%,XX%,XX%,XX%,[=E62/(E29-D29)],[=F62/(F29-E29)],[=G62/(G29-F29)]
,,,,,,
Unlevered FCF,XXX,XXX,XXX,XXX,[=E57+E58-E60-E62],[=F57+F58-F60-F62],[=G57+G58-G60-G62]
```

**Row reference examples** (based on layout planning):
- $E$21 = D&A % assumption (consolidation column, row 21)
- $E$22 = CapEx % assumption (consolidation column, row 22)
- $E$23 = NWC % assumption (consolidation column, row 23)
- E29 = Revenue for year (row 29)
- E45 = NOPAT for year (row 45)

**Before writing formulas**: Confirm these row numbers match the actual layout. Test one column, then copy across.

**Section 6: Discounting & Valuation**
```csv
DCF Valuation,2024E,2025E,2026E,2027E,2028E,Terminal
Unlevered FCF ($M),XXX,XXX,XXX,XXX,XXX,
Period,0.5,1.5,2.5,3.5,4.5,
Discount Factor,0.XX,0.XX,0.XX,0.XX,0.XX,
PV of FCF ($M),XXX,XXX,XXX,XXX,XXX,
,,,,,,
Terminal FCF ($M),,,,,,,XXX
Terminal Value ($M),,,,,,,XXX
PV Terminal Value ($M),,,,,,,XXX
,,,,,,
Valuation Summary ($M),,,,,,
Sum of PV FCFs,XXX,,,,,
PV Terminal Value,XXX,,,,,
Enterprise Value,XXX,,,,,
(-) Net Debt,(XX),,,,,
Equity Value,XXX,,,,,
,,,,,,
Shares Outstanding (M),XX.X,,,,,
IMPLIED PRICE PER SHARE,$XX.XX,,,,,
Current Stock Price,$XX.XX,,,,,
Implied Upside/(Downside),XX%,,,,,
```

### WACC Sheet Structure

```csv
COST OF EQUITY CALCULATION,,
Risk-Free Rate (10Y Treasury),X.XX%,[Yellow input]
Beta (5Y monthly),X.XX,[Yellow input]
Equity Risk Premium,X.XX%,[Yellow input]
Cost of Equity,X.XX%,[Calculated blue]
,,
COST OF DEBT CALCULATION,,
Credit Rating,AA-,[Yellow input]
Pre-Tax Cost of Debt,X.XX%,[Yellow input]
Tax Rate,XX.X%,[Link to DCF sheet]
After-Tax Cost of Debt,X.XX%,[Calculated blue]
,,
CAPITAL STRUCTURE,,
Current Stock Price,$XX.XX,[Link to DCF]
Shares Outstanding (M),XX.X,[Link to DCF]
Market Capitalization ($M),"X,XXX",[Calculated]
,,
Total Debt ($M),XXX,[Yellow input]
Cash & Equivalents ($M),XXX,[Yellow input]
Net Debt ($M),XXX,[Calculated]
,,
Enterprise Value ($M),"X,XXX",[Calculated]
,,
WACC CALCULATION,Weight,Cost,Contribution
Equity,XX.X%,X.X%,X.XX%
Debt,XX.X%,X.X%,X.XX%
,,
WEIGHTED AVERAGE COST OF CAPITAL,X.XX%,[Green output]
```

**Key WACC Formulas:**
```
Market Cap = Price × Shares
Net Debt = Total Debt - Cash
Enterprise Value = Market Cap + Net Debt
Equity Weight = Market Cap / EV
Debt Weight = Net Debt / EV
WACC = (Cost of Equity × Equity Weight) + (After-tax Cost of Debt × Debt Weight)
```

## Deliverables Structure

**File naming**: `[Ticker]_DCF_Model_[Date].xlsx`

**Two sheets**:
1. **DCF** - Complete model with Bear/Base/Bull cases + three sensitivity tables at bottom (WACC vs Terminal Growth, Revenue Growth vs EBIT Margin, Beta vs Risk-Free Rate)
2. **WACC** - Cost of capital calculation

**Key features**: Case selector (1/2/3), consolidation column with INDEX/OFFSET formulas, color-coded cells, cell comments on all inputs, professional borders
