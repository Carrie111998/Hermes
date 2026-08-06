# Hermes Agent Shipped Karpathy's LLM Wiki Four Days After the Gist

On April 4, Andrej Karpathy published [a gist describing the "LLM Wiki"](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — a pattern for personal knowledge bases where the LLM doesn't just retrieve raw chunks at query time (RAG), it incrementally *builds and maintains* a persistent wiki of interlinked markdown files. He deliberately shared the idea rather than code, inviting people to instantiate it with their own agents.

Hermes shipped a working implementation in [v0.8.0 on April 8](RELEASE_v0.8.0.md) — the following Tuesday. It's been maturing ever since and is bundled with every install as the [`llm-wiki` skill](skills/research/llm-wiki/SKILL.md).

---

## The Pattern

Karpathy's design has three layers and three operations:

**Layers:**
1. **Raw sources** — immutable curated documents; the source of truth
2. **The wiki** — LLM-generated, interlinked markdown pages the agent owns entirely
3. **The schema** — a configuration document defining structure, conventions, and workflows

**Operations:**
- **Ingest** — read a new source, extract what matters, update every relevant page
- **Query** — synthesize answers from compiled knowledge, file valuable findings back
- **Lint** — health-check for contradictions, stale claims, and orphaned pages

The key insight: *"the wiki is a persistent, compounding artifact."* Cross-references already exist. Contradictions are already flagged. Synthesis reflects everything previously read. As Karpathy puts it, the tedious part of a knowledge base isn't the reading or thinking — it's the bookkeeping. That's exactly what LLMs never get tired of.

---

## How Hermes Implements It — Element by Element

| Gist concept | Hermes `llm-wiki` skill |
|---|---|
| **Raw sources** (immutable) | `raw/` tree (`articles/`, `papers/`, `transcripts/`, `assets/`) — agent reads, never modifies. Each source gets a `sha256` fingerprint so re-ingests skip unchanged content and **flag silent source drift** |
| **The wiki** | `entities/`, `concepts/`, `comparisons/`, `queries/` — agent-owned markdown with YAML frontmatter and `[[wikilinks]]` (minimum 2 outbound links per page) |
| **The schema** (CLAUDE.md-style) | `SCHEMA.md` — domain definition, naming conventions, tag taxonomy, page-creation thresholds, contradiction policy. Generated per-domain when the wiki is initialized |
| **index.md** | Sectioned content catalog, one-line summary per page, with scaling rules (sub-sections past 50 entries, topic map past 200) |
| **log.md** | Append-only chronological record with parseable `## [YYYY-MM-DD] action \| subject` prefixes, rotated yearly |
| **Ingest** | Six-step workflow: capture raw + hash → discuss takeaways → check existing pages → write/update → update index + log → report. One source can ripple across 5–15 pages — the compounding effect |
| **Query** | Index-first lookup, full-text search fallback for large wikis, synthesis with page citations, and valuable answers filed back into `queries/` |
| **Lint** | A 13-check audit: orphan pages, broken wikilinks, index completeness, frontmatter validation, stale content, contradictions, confidence signals, source drift, oversized pages, tag sprawl, log rotation |
| **qmd** (local search, mentioned in the gist) | Bundled as an [optional skill](optional-skills/research/qmd/SKILL.md) — BM25 + vector + LLM-rerank hybrid search over the same markdown, fully local |

The wiki is just a directory of markdown files. No database, no embeddings pipeline, no lock-in — open it in Obsidian or any editor.

---

## Where Hermes Goes Beyond the Gist

### Provenance and epistemic hygiene

Pages synthesizing multiple sources carry `^[raw/articles/source.md]` provenance markers per paragraph, so any claim traces back without re-reading raw files. Frontmatter supports `confidence: high|medium|low`, `contested: true`, and `contradictions: [page]` — and lint surfaces weak or conflicting claims so they don't silently harden into accepted wiki fact.

### Scheduled ingestion

Karpathy's pattern assumes you hand sources to the agent. Hermes automates the pipeline with its built-in cron scheduler:

```bash
hermes cron create "0 8 * * *" \
  "Check watched feeds for new posts. Ingest anything relevant into the wiki per its SCHEMA.md." \
  --skills "blogwatcher,llm-wiki" \
  --name "Morning wiki ingest" \
  --deliver telegram
```

Your wiki grows every morning before you wake up, and you get a Telegram summary of what changed.

### Query from anywhere

Hermes lives in Telegram, Discord, Slack, WhatsApp, Signal, and the CLI. Ask your wiki a question from your phone; the agent reads `index.md`, pulls the relevant pages, and answers with citations — then files the synthesis back if it was worth keeping.

### Obsidian, including headless

The wiki directory is a valid Obsidian vault out of the box — wikilinks, Graph View, Dataview-ready frontmatter. For agents running on servers, the skill documents an `obsidian-headless` sync setup (systemd unit included): the agent writes to `~/wiki` on your VPS while you browse the same vault on your laptop and phone, changes appearing within seconds.

### Model-agnostic

Run ingest on a cheap model and deep queries on a frontier one. Same wiki, any backend — Claude, GPT, Gemini, DeepSeek, Qwen, or a local model.

---

## Timeline

- **April 4, 2026** — Karpathy publishes the LLM Wiki gist
- **April 8, 2026** — Hermes v0.8.0 ships the `llm-wiki` skill ([#5635](https://github.com/NousResearch/hermes-agent/pull/5635))
- **v0.8.0 follow-up** — Obsidian Headless setup for servers ([#5660](https://github.com/NousResearch/hermes-agent/pull/5660))
- **v0.11.0** — provenance markers, source hashing, quality signals ([#13700](https://github.com/NousResearch/hermes-agent/pull/13700))
- **Today** — skill at v2.1.0, bundled with every install

Credit where due: the pattern is Karpathy's, and he explicitly framed the gist as an idea to adapt with your own agent. The gist's comment thread shows dozens of implementations across domains. This is ours — schema-driven, lint-enforced, cron-fed, and reachable from your messaging apps.

---

## Get Started

Hermes Agent is open source (MIT) and the skill is bundled — nothing extra to install.

```bash
pip install hermes-agent
hermes setup
```

Then just tell it:

> "Create a wiki for tracking AI/ML research at ~/wiki"

It writes `SCHEMA.md` customized to your domain, scaffolds the three layers, and asks for your first sources. From there:

> "Ingest https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f"

Fitting first entry.

Documentation: [hermes-agent.nousresearch.com](https://hermes-agent.nousresearch.com)

GitHub: [github.com/NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)

---

*Hermes Agent is built by [Nous Research](https://nousresearch.com). Open source, model-agnostic, runs on your infrastructure.*
