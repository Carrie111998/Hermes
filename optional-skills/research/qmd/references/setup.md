# QMD Setup — Prerequisites, Install, Collections

---

## Prerequisites

### Node.js >= 22 (required)

```bash
# Check version
node --version  # must be >= 22

# macOS — install or upgrade via Homebrew
brew install node@22

# Linux — use NodeSource or nvm
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs
# or with nvm:
nvm install 22 && nvm use 22
```

### SQLite with Extension Support (macOS only)

macOS system SQLite lacks extension loading. Install via Homebrew:

```bash
brew install sqlite
```

### Install qmd

```bash
npm install -g @tobilu/qmd
# or with Bun:
bun install -g @tobilu/qmd
```

First run auto-downloads 3 local GGUF models (~2GB total):

| Model | Purpose | Size |
|-------|---------|------|
| embeddinggemma-300M-Q8_0 | Vector embeddings | ~300MB |
| qwen3-reranker-0.6b-q8_0 | Result reranking | ~640MB |
| qmd-query-expansion-1.7B | Query expansion | ~1.1GB |

### Verify Installation

```bash
qmd --version
qmd status
```

(The command quick-reference table lives in `SKILL.md`.)

## Setup Workflow

### 1. Add Collections

Point qmd at directories containing your documents:

```bash
# Add a notes directory
qmd collection add ~/notes --name notes

# Add project docs
qmd collection add ~/projects/myproject/docs --name project-docs

# Add meeting transcripts
qmd collection add ~/meetings --name meetings

# List all collections
qmd collection list
```

### 2. Add Context Descriptions

Context metadata helps the search engine understand what each collection
contains. This significantly improves retrieval quality:

```bash
qmd context add qmd://notes "Personal notes, ideas, and journal entries"
qmd context add qmd://project-docs "Technical documentation for the main project"
qmd context add qmd://meetings "Meeting transcripts and action items from team syncs"
```

### 3. Generate Embeddings

```bash
qmd embed
```

This processes all documents in all collections and generates vector
embeddings. Re-run after adding new documents or collections.

### 4. Verify

```bash
qmd status   # shows index health, collection stats, model info
```
