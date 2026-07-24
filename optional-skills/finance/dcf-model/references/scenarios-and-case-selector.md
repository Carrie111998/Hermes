# Scenario Blocks (Bear/Base/Bull) and Case Selector

### Scenario Block Selection Pattern - Follow This Approach

**Assumptions are organized in separate blocks for each scenario:**

**CRITICAL STRUCTURE - Three rows per section header:**

```csv
BEAR CASE ASSUMPTIONS (section header, merge cells across)
Assumption,FY1,FY2,FY3,FY4,FY5
Revenue Growth (%),12%,10%,9%,8%,7%
EBIT Margin (%),45%,44%,43%,42%,41%

BASE CASE ASSUMPTIONS (section header, merge cells across)
Assumption,FY1,FY2,FY3,FY4,FY5
Revenue Growth (%),16%,14%,12%,10%,9%
EBIT Margin (%),48%,49%,50%,51%,52%

BULL CASE ASSUMPTIONS (section header, merge cells across)
Assumption,FY1,FY2,FY3,FY4,FY5
Revenue Growth (%),20%,18%,15%,13%,11%
EBIT Margin (%),50%,51%,52%,53%,54%
```

**Each scenario block MUST have a column header row** showing the projection years (FY2025E, FY2026E, etc.) immediately below the section title. Without this, users cannot tell which assumption value corresponds to which year.

**How to reference assumptions - Create a consolidation column:**
1. Case selector cell (e.g., B6) contains 1=Bear, 2=Base, or 3=Bull
2. Create a consolidation column with INDEX or OFFSET formulas to pull from the correct scenario block
3. Projection formulas reference the consolidation column (clean cell references)
4. Each scenario block contains full set of DCF assumptions across projection years

**Recommended consolidation column pattern (using INDEX):**
`=INDEX(B10:D10, 1, $B$6)`

**NOT this - scattered IF statements throughout:**
`=IF($B$6=1,[Bear block cell],IF($B$6=2,[Base block cell],[Bull block cell]))`

The consolidation column approach centralizes logic and makes the model easier to audit.

### Correct Revenue Projection Pattern

**Create a consolidation column with INDEX formulas, then reference it in projections:**

**Step 1 - Consolidation column for FY1 growth:**
`=INDEX([Bear FY1 growth]:[Bull FY1 growth], 1, $B$6)`

**Step 2 - Revenue projection references the consolidation column:**
`Revenue Year 1: =D29*(1+$E$10)`

Where:
- D29 = Prior year revenue
- $E$10 = Consolidation column cell for FY1 growth (contains INDEX formula)
- $B$6 = Case selector (1=Bear, 2=Base, 3=Bull)

**This approach is cleaner than embedding IF statements in every projection formula** and makes it much easier to audit which scenario assumptions are being used.

### Correct Assumption Table Structure

**CRITICAL: Each scenario block requires THREE structural elements:**

1. **Section header row** (merged cells): e.g., "BEAR CASE ASSUMPTIONS"
2. **Column header row** showing years - THIS IS REQUIRED, DO NOT SKIP
3. **Data rows** with assumption values

**Structure:**
```csv
BEAR CASE ASSUMPTIONS (section header - merge across columns A:G)
Assumption,FY1,FY2,FY3,FY4,FY5
Revenue Growth (%),X%,X%,X%,X%,X%
EBIT Margin (%),X%,X%,X%,X%,X%
Terminal Growth,X%,,,,
WACC,X%,,,,

BASE CASE ASSUMPTIONS (section header - merge across columns A:G)
Assumption,FY1,FY2,FY3,FY4,FY5
Revenue Growth (%),X%,X%,X%,X%,X%
EBIT Margin (%),X%,X%,X%,X%,X%
Terminal Growth,X%,,,,
WACC,X%,,,,

BULL CASE ASSUMPTIONS (section header - merge across columns A:G)
Assumption,FY1,FY2,FY3,FY4,FY5
Revenue Growth (%),X%,X%,X%,X%,X%
EBIT Margin (%),X%,X%,X%,X%,X%
Terminal Growth,X%,,,,
WACC,X%,,,,
```

**WITHOUT the column header row showing projection years (FY2025E, FY2026E, etc.), users cannot tell which assumption value corresponds to which year. This row is MANDATORY.**

**Then create a consolidation column** (typically the next column to the right) that uses INDEX formulas to pull from the selected scenario block based on the case selector. This consolidation column is what your projection formulas reference.

### WRONG: Single Row for Each Assumption Across Scenarios

**Don't structure assumptions like this:**
```csv
Assumption,Bear,Base,Bull
Revenue Growth FY1,10%,13%,16%
Revenue Growth FY2,9%,12%,15%
```
This vertical layout makes it hard to see the progression across years within each scenario.

**Why it's wrong:**
- Makes it difficult to see assumptions evolving across years within each scenario
- Harder to compare scenario assumptions across full projection period
- Less intuitive for reviewing scenario logic

**Instead:**
- Create separate blocks for each scenario (Bear, Base, Bull)
- Within each block, show assumptions horizontally across projection years
- This makes each scenario's assumptions easier to review as a cohesive set

## Case Selector Implementation

**Three-Case Framework:**

### Bear Case
- Conservative revenue growth (low end of historical range)
- Margin compression or no expansion
- Higher WACC (risk premium increase)
- Lower terminal growth rate
- Higher CapEx assumptions

### Base Case
- Consensus or management guidance revenue growth
- Moderate margin expansion based on operating leverage
- Current market-implied WACC
- GDP-aligned terminal growth (2.5-3.0%)
- Standard CapEx assumptions

### Bull Case
- Optimistic revenue growth (high end of projections)
- Significant margin expansion
- Lower WACC (reduced risk premium)
- Higher terminal growth (3.5-5.0%)
- Reduced CapEx intensity

**Formula Implementation:**

**DO NOT use nested IF formulas scattered throughout.** Instead, create a consolidation column that uses INDEX or OFFSET formulas to pull from the appropriate scenario block.

**Recommended pattern (using INDEX):**
`=INDEX(B10:D10, 1, $B$6)` where `B10:D10` = Bear/Base/Bull values, `1` = row offset, `$B$6` = case selector cell (1, 2, or 3)

**Then reference the consolidation column** in all projections:
`Revenue Year 1: =D29*(1+$E$10)` where $E$10 is the consolidation column value for Year 1 growth.

This approach centralizes scenario logic, making the model easier to audit and maintain.
