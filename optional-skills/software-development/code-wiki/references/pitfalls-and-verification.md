# Pitfalls & Verification

The three hard red lines (no fabrication, no in-repo output without asking, no Mermaid over 50 nodes) are in SKILL.md. These are the rest.

## Pitfalls

- **Generic AI prose.** "This module is responsible for..." is content-free. Say what the module actually does in domain-specific terms.
- **Restating code as prose.** A module doc that says "the `process` function processes things by calling `process_item` on each item" is worse than just linking to the function.
- **Documenting tests, generated code, or vendored deps as if they were product code.** Skip them.
- **Mermaid special chars need quotes:** `A["Tool / Agent"]` not `A[Tool / Agent]`. `<br>` for line breaks inside a node.
- **Nested code fences in SKILL.md.** When writing a markdown example that contains a Mermaid block, use 4-backtick outer fences so the 3-backtick inner ` ```mermaid ` doesn't close the outer. (`references/procedure.md` does it.)
- **classDiagram generics** render as `~T~` (e.g. `List~Tool~`), not `<T>`.
- **GitHub Mermaid theme is fixed** — don't include `%%{init: ...}%%` blocks; they're stripped on render.

## Verification

After writing, verify:

1. **Mermaid blocks balance** — opens equal closes per file:
   ```bash
   for f in "$OUTPUT_DIR"/diagrams/*.md "$OUTPUT_DIR"/architecture.md; do
     opens=$(grep -c '^```mermaid' "$f")
     total=$(grep -c '^```' "$f")
     echo "$f: $opens mermaid blocks, $total total fences (expect total = opens*2)"
   done
   ```
2. **All expected files exist** —
   ```bash
   ls "$OUTPUT_DIR"/{README.md,architecture.md,getting-started.md,.codewiki-state.json} \
      "$OUTPUT_DIR"/modules/ "$OUTPUT_DIR"/diagrams/
   ```
3. **Module count matches what you intended** — `ls "$OUTPUT_DIR/modules" | wc -l` should equal the number of modules you committed to in Step 3.
4. **No fabricated paths** — sanity-check 2–3 source links resolve to real files.
