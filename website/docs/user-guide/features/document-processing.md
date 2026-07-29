---
title: Document Processing
sidebar_label: Document Processing
sidebar_position: 18
---

# Document Processing

Hermes includes a read-only document/corpus processing CLI for turning local files into source-anchored reports and proposal-only drafts.

Use it when you want Hermes to inspect documents without adding a new always-on model tool and without letting an auxiliary model mutate notes or source files directly.

```bash
hermes documents summarize ./notes/example.md
hermes documents merge-draft ./notes/a.md ./notes/b.md
hermes documents plan --manifest ./sources.json
hermes documents integrity-check ./report.json
```

`hermes docs` is an alias for `hermes documents`.

## What it does

The pipeline is intentionally conservative:

1. Builds a source registry with path, type, size, hash/snapshot, rights status, sensitivity, routing policy, origin, and retention metadata.
2. Extracts text locally where practical.
3. Chunks text with source anchors: path, line range, page/slide/sheet/chapter where available, chunk index, extractor, hash, quote, and exact-token markers.
4. Runs a fail-closed external-processing gate before any auxiliary LLM call.
5. Optionally calls one of the configured `auxiliary.document_*` tasks.
6. Emits reports/proposals only. It does not edit Obsidian, delete sources, or write final merged notes.

## Supported local inputs

| Type | Adapter |
| --- | --- |
| Markdown/text | stdlib text reader |
| HTML/XHTML | stdlib `html.parser` text extraction |
| SRT/VTT transcripts | stdlib timestamp/counter stripping |
| PDF | PyMuPDF/`fitz` when installed; otherwise fail-closed |
| DOCX | stdlib ZIP/XML extraction from `word/document.xml` |
| PPTX | stdlib ZIP/XML extraction from slides |
| XLSX | stdlib ZIP/XML extraction from worksheets and shared strings |
| EPUB | stdlib ZIP/HTML extraction from HTML/XHTML chapters |

The Office/EPUB adapters are lightweight source-text extractors, not full layout renderers. Use stronger specialist tooling when exact layout, OCR, tables, equations, or forms matter.

## Rights and privacy gate

External auxiliary calls are blocked unless all of this is true for every source:

- `--rights-status allowed`
- `--routing-policy external_allowed`
- `--sensitivity public` or `internal`
- extraction succeeded

Defaults are deliberately safe:

```bash
hermes documents summarize ./private.md
```

That runs locally and produces a report, but `--use-auxiliary` will be skipped because the default routing policy is `local_only` and rights are `unknown`.

To allow an external auxiliary model for low-risk content:

```bash
hermes documents summarize ./sample.md \
  --rights-status allowed \
  --sensitivity public \
  --routing-policy external_allowed \
  --use-auxiliary
```

`metadata_only` can be used for planning. It allows metadata-based corpus planning without sending extracted document text.

## Commands

### `summarize`

Build a source registry, extract/chunk local text, produce local source summaries, and optionally call `auxiliary.document_summarization`.

```bash
hermes documents summarize ./a.md ./b.docx --format json --output report.json
```

With auxiliary summarization:

```bash
hermes documents summarize ./a.md \
  --rights-status allowed \
  --sensitivity internal \
  --routing-policy external_allowed \
  --use-auxiliary
```

### `merge-draft`

Creates a proposal-only merge draft via `auxiliary.document_merge_draft` when `--use-auxiliary` is allowed. The output includes a write boundary and is not applied to any file automatically.

```bash
hermes documents merge-draft ./old.md ./new.md \
  --rights-status allowed \
  --sensitivity public \
  --routing-policy external_allowed \
  --use-auxiliary \
  --output merge-proposal.json \
  --format json
```

### `plan`

Creates a metadata-only corpus plan. This is suitable before processing a mixed or sensitive corpus.

```bash
hermes documents plan --manifest sources.json --format markdown
```

With auxiliary planning:

```bash
hermes documents plan --manifest sources.json --use-auxiliary
```

The auxiliary task is `document_corpus_planner`; it receives registry metadata, not extracted document text.

### `integrity-check`

Runs deterministic checks over a JSON report/proposal. Currently it checks whether each auxiliary material claim's `exact_quote` is present in the referenced unit text.

```bash
hermes documents integrity-check report.json --format json
```

By default this is a local check and does not call a model. Add `--use-auxiliary` to also call `auxiliary.document_integrity_check` after the report's original source gate passes:

```bash
hermes documents integrity-check report.json --use-auxiliary --format json
```

## Manifest input

You can provide source metadata in JSON:

```json
{
  "sources": [
    {
      "path": "./notes/a.md",
      "source_id": "notes-a",
      "source_type": "markdown",
      "title": "Notes A",
      "rights_status": "allowed",
      "sensitivity": "internal",
      "routing_policy": "external_allowed",
      "origin": "user_supplied",
      "retention_policy": "pilot_derived_90d"
    }
  ]
}
```

Run it:

```bash
hermes documents summarize --manifest sources.json --format json
```

## Auxiliary routing

The CLI uses these auxiliary task keys:

```yaml
auxiliary:
  document_summarization:
    provider: auto
    model: ''
    timeout: 180
  document_merge_draft:
    provider: auto
    model: ''
    timeout: 300
  document_integrity_check:
    provider: auto
    model: ''
    timeout: 300
  document_corpus_planner:
    provider: auto
    model: ''
    timeout: 120
```

`auto` may route through the main model depending on your setup. If the goal is reducing main-model token use, pin these explicitly after configuring credentials:

```bash
hermes config set auxiliary.document_summarization.provider gemini
hermes config set auxiliary.document_summarization.model gemini-flash-lite-latest
```

Repeat for the other document tasks as needed, then run:

```bash
hermes config check
```

## Safety boundaries

- The pipeline is read-only with respect to source artifacts.
- Auxiliary output is treated as draft analysis, not a source of truth.
- Reports include source anchors and exact quotes for material claims.
- Any final file write or Obsidian merge should happen in a separate reviewed step after backup, preview/diff, and integrity checks.
- Secrets do not belong in manifests, config, docs, or reports.
