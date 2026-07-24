# Parallel CLI — Command Reference & Workflow Recipes

Per-command flags, examples, and the recommended Hermes usage patterns.

---

## Search

Use for current web lookups with structured results.

```bash
parallel-cli search "What is Anthropic's latest AI model?" --json
parallel-cli search "SEC filings for Apple" --include-domains sec.gov --json
parallel-cli search "bitcoin price" --after-date 2026-01-01 --max-results 10 --json
parallel-cli search "latest browser benchmarks" --mode one-shot --json
parallel-cli search "AI coding agent enterprise reviews" --mode agentic --json
```

Useful constraints:
- `--include-domains` to narrow trusted sources
- `--exclude-domains` to strip noisy domains
- `--after-date` for recency filtering
- `--max-results` when you need broader coverage

If you expect follow-up questions, save output:

```bash
parallel-cli search "latest React 19 changes" --json -o /tmp/react-19-search.json
```

When summarizing results:
- lead with the answer
- include dates, names, and concrete facts
- cite only returned sources
- avoid inventing URLs or source titles

## Extraction

Use to pull clean content or markdown from a URL.

```bash
parallel-cli extract https://example.com --json
parallel-cli extract https://company.com --objective "Find pricing info" --json
parallel-cli extract https://example.com --full-content --json
parallel-cli fetch https://example.com --json
```

Use `--objective` when the page is broad and you only need one slice of information.

## Deep research

Use for deeper multi-step research tasks that may take time.

Common processor tiers:
- `lite` / `base` for faster, cheaper passes
- `core` / `pro` for more thorough synthesis
- `ultra` for the heaviest research jobs

### Synchronous

```bash
parallel-cli research run \
  "Compare the leading AI coding agents by pricing, model support, and enterprise controls" \
  --processor core \
  --json
```

### Async launch + poll

```bash
parallel-cli research run \
  "Compare the leading AI coding agents by pricing, model support, and enterprise controls" \
  --processor ultra \
  --no-wait \
  --json

parallel-cli research status trun_xxx --json
parallel-cli research poll trun_xxx --json
parallel-cli research processors --json
```

### Context chaining / follow-up

```bash
parallel-cli research run "What are the top AI coding agents?" --json
parallel-cli research run \
  "What enterprise controls does the top-ranked one offer?" \
  --previous-interaction-id trun_xxx \
  --json
```

Recommended Hermes workflow:
1. launch with `--no-wait --json`
2. capture the returned run/task ID
3. if the user wants to continue other work, keep moving
4. later call `status` or `poll`
5. summarize the final report with citations from the returned sources

## Enrichment

Use when the user has CSV/JSON/tabular inputs and wants additional columns inferred from web research.

### Suggest columns

```bash
parallel-cli enrich suggest "Find the CEO and annual revenue" --json
```

### Plan a config

```bash
parallel-cli enrich plan -o config.yaml
```

### Inline data

```bash
parallel-cli enrich run \
  --data '[{"company": "Anthropic"}, {"company": "Mistral"}]' \
  --intent "Find headquarters and employee count" \
  --json
```

### Non-interactive file run

```bash
parallel-cli enrich run \
  --source-type csv \
  --source companies.csv \
  --target enriched.csv \
  --source-columns '[{"name": "company", "description": "Company name"}]' \
  --intent "Find the CEO and annual revenue"
```

### YAML config run

```bash
parallel-cli enrich run config.yaml
```

### Status / polling

```bash
parallel-cli enrich status <task_group_id> --json
parallel-cli enrich poll <task_group_id> --json
```

Use explicit JSON arrays for column definitions when operating non-interactively.
Validate the output file before reporting success.

## FindAll

Use for web-scale entity discovery when the user wants a discovered dataset rather than a short answer.

```bash
parallel-cli findall run "Find AI coding agent startups with enterprise offerings" --json
parallel-cli findall run "AI startups in healthcare" -n 25 --json
parallel-cli findall status <run_id> --json
parallel-cli findall poll <run_id> --json
parallel-cli findall result <run_id> --json
parallel-cli findall schema <run_id> --json
```

This is a better fit than ordinary search when the user wants a discovered set of entities that can be reviewed, filtered, or enriched later.

## Monitor

Use for ongoing change detection over time.

```bash
parallel-cli monitor list --json
parallel-cli monitor get <monitor_id> --json
parallel-cli monitor events <monitor_id> --json
parallel-cli monitor delete <monitor_id> --json
```

Creation is usually the sensitive part because cadence and delivery matter:

```bash
parallel-cli monitor create --help
```

Use this when the user wants recurring tracking of a page or source rather than a one-time fetch.

## Recommended Hermes usage patterns

### Fast answer with citations
1. Run `parallel-cli search ... --json`
2. Parse titles, URLs, dates, excerpts
3. Summarize with inline citations from the returned URLs only

### URL investigation
1. Run `parallel-cli extract URL --json`
2. If needed, rerun with `--objective` or `--full-content`
3. Quote or summarize the extracted markdown

### Long research workflow
1. Run `parallel-cli research run ... --no-wait --json`
2. Store the returned ID
3. Continue other work or periodically poll
4. Summarize the final report with citations

### Structured enrichment workflow
1. Inspect the input file and columns
2. Use `enrich suggest` or provide explicit enriched columns
3. Run `enrich run`
4. Poll for completion if needed
5. Validate the output file before reporting success
