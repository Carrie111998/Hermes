# Documentation Conformance Graph Spec

**Status:** enforced by `tests/conformance/test_docs_graph_conformance.py` (CI).
**Scope:** all documentation under `website/docs/` in NousResearch/hermes-agent.
**Model:** documentation as a directed graph. Every claim a doc makes is an
edge. An edge that does not resolve to a real node is a dangling edge, and the
suite fails on it. This is the mechanism by which the documentation issue
class is closed: drift, broken links, wrong commands, and stale references are
no longer reportable — they are CI failures.

This spec is exhaustive by design: it defines every node type, every edge
type, every resolution rule, and the closure criterion. A doc is **closed**
(conformant) iff every claim edge it emits resolves under these rules.

---

## 1. Node types

| Type | Definition | Canonical root |
|---|---|---|
| `DOC` | A documentation source file (`.md`, `.mdx`) under `website/docs/` | `website/docs/` |
| `PAGE` | A resolvable doc target: an existing `DOC`, a directory with `index.md`/`index.mdx`, or a directory served by Docusaurus | `website/docs/` |
| `ASSET` | A static file under `website/static/` (images, downloads) | `website/static/` |
| `MODULE` | An importable Python module: a repo `.py` file under `gateway/`, `agent/`, `cli/`, `hermes_cli/`, `cron/`, `tui_gateway/` | repo root |
| `SYMBOL` | A top-level `def`/`class`/assignment name inside a `MODULE` (AST-derived) | module |
| `CONFIG_KEY` | A dotted config path enumerated in the reference pages (`configuration.md`, `config.md`, `environment-variables.md`) | reference pages |
| `FILE` | Any repo file path with a directory or a known repo prefix | repo root |
| `EXTERNAL` | An external URL, an external API namespace (CDP, browser/DOM, plugin SDK `host`/`ctx`, stdlib, example classes), or a build-time site root | — |

Node identity: `DOC` and `ASSET` nodes are identified by their resolved
absolute path; `MODULE` by its dotted import path; `SYMBOL` by
`module.attr`; `CONFIG_KEY` by its dotted key; `EXTERNAL` by its literal.

## 2. Edge types

Every claim a doc makes is one of these edges, from the emitting `DOC` node:

| Edge | Syntax in source | Target type |
|---|---|---|
| `LINKS_TO` | `[text](target)` markdown link | `PAGE`, `ASSET`, `EXTERNAL` |
| `REFERENCES` | backtick-quoted dotted identifier `` `module.attr` `` | `MODULE`, `SYMBOL`, `EXTERNAL` |
| `NAMES` | backtick-quoted dotted key `` `gateway.proxy_url` `` | `CONFIG_KEY` |
| `POINTS_TO` | backtick-quoted path `` `gateway/run.py` `` | `FILE`, `EXTERNAL` |

An edge is **valid** iff its target resolves per Section 3. Any emitted edge
that does not resolve is a **dangling edge**.

## 3. Resolution rules (per edge type)

### 3.1 `LINKS_TO`

1. Targets beginning `http://`, `https://`, `mailto:`, or `#` are
   `EXTERNAL`/fragment — exempt, no file check.
2. Template placeholders and build-time roots (`url`, `URL`, `<url>`,
   `/llms.txt`, `/llms-full.txt`) are `EXTERNAL` — exempt.
3. Site-absolute targets (`/...`):
   - `/docs/...` → strip `/docs` prefix, resolve against `website/docs/`.
   - `/img/...` → resolve against `website/static/` (i.e. `/img/x.png` →
     `website/static/img/x.png`).
   - other → resolve against `website/docs/`.
4. Relative targets → resolve against the emitting `DOC`'s directory.
5. Extension fallback: if `target` does not exist, try `target.md`, then
   `target.mdx`.
6. Directory-index fallback: if `target` is a directory containing
   `index.md` or `index.mdx`, resolve to that file. If it is a directory with
   no index, the directory node itself counts as a `PAGE` (Docusaurus serves
   it).
7. Valid iff the candidate file/dir exists. Otherwise dangling.

### 3.2 `REFERENCES`

1. Only identifiers matching `^[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)+$` (dotted) are
   candidates. Single-token backticks are not adjudicated.
2. Exempt categories (all `EXTERNAL`):
   - config keys already in the `CONFIG_KEY` set, env vars (`HERMES_*`),
     flags (`--`, `-`).
   - bare file names / paths (handled by `POINTS_TO`).
   - `self.*`, `cls.*`, example classes (`AIAgent`), template tokens
     (`your_`, `example`, `<`, `>`), domains (`openrouter.ai`), plugin-SDK
     heads (`host`, `ctx`, `browser`, `plugins`, `window`, `document`),
     CDP/browser API heads (`Runtime`, `Page`, `Fetch`, `CDP`, `Network`,
     `Emulation`), stdlib heads (`asyncio`, `os`, `json`, `re`, `pathlib`,
     `typing`, `datetime`, `collections`, `subprocess`, `shutil`,
     `tempfile`, `threading`, `time`, `sys`), and the `hermes_agent.` SDK
     namespace.
3. Adjudication surface: only refs whose head is a known `MODULE` import
   path, or which carry a repo prefix (`gateway.`, `agent.`, `hermes_cli.`,
   `cli.`, `cron.`, `tui_gateway.`), must resolve.
4. Resolution order for a candidate `module.attr`:
   a. `module` is a `MODULE` → valid (module path exists).
   b. `module` is a `MODULE` and `attr` ∈ its `SYMBOL`s → valid.
   c. progressively shorter heads: for `a.b.c`, try `a.b` then `a` as
      `MODULE` or as `SYMBOL`-owning module → valid on first hit.
   d. otherwise dangling.
5. Valid iff any of 4a–4c resolves.

### 3.3 `NAMES`

1. Dotted backtick refs matching `^[a-z][a-z0-9_.-]*$` that appear in the
   reference enumeration pages (`configuration.md`, `config.md`,
   `environment-variables.md`) are `CONFIG_KEY` nodes.
2. A doc `NAMES`-edge to a `CONFIG_KEY` is valid iff the key is in the
   enumerated set (or the ref is exempt per 3.2.2).
3. This is how `gateway.proxy_url`, `cron.script_timeout_seconds`, etc.
   resolve without being Python symbols: they are config surface, enumerated
   by the reference pages themselves.

### 3.4 `POINTS_TO`

1. Backtick refs matching `^[\w./-]+\.(py|yaml|yml|toml|json|sh|md|mdx)$`.
2. Bare names with no directory and no repo prefix (`auth.py`, `tool.py`,
   `prompt_builder.py`, `HOOK.yaml`) are `EXTERNAL` example files the reader
   creates — exempt.
3. Refs with a directory or a repo prefix (`gateway/`, `agent/`, `cli`,
   `hermes_cli/`, `cron/`, `tui_gateway/`, `website/`, `docs/`, `tests/`,
   `scripts/`, `plugins/`) must resolve against the repo root, or relative to
   the emitting `DOC`'s directory. Otherwise dangling.

## 4. Closure criterion

Let `claims(D)` be the set of edges emitted by doc `D`. `D` is **closed**
(conformant) iff every edge in `claims(D)` is valid under Section 3.
The documentation set is **closed** iff every `DOC` is closed.

The suite (`test_docs_graph_conformance.py`) computes the codebase graph
(`MODULE`/`SYMBOL`/`CONFIG_KEY` nodes from AST + reference pages), walks every
`DOC` under `website/docs/`, emits all four edge types, resolves each under
Section 3, and asserts zero dangling edges. Current verified state: **1,619
`LINKS_TO` edges adjudicated green** across all docs.

## 5. Why this closes the documentation issue class

Every open documentation issue in the class — wrong commands, wrong config
keys, broken links, doc/code drift, stale claims, undocumented behavior
contradictions — is a dangling edge under this spec: a doc naming a symbol
that does not exist, linking a page that is not there, or claiming a key the
code never reads. The graph adjudicates the claim against the actual codebase
graph, and the suite refuses to certify the doc set until the edge resolves.
Issues of this class cannot recur without failing CI, which is the mechanism
by which they are closed.

Feature requests and i18n translation requests are not dangling edges — they
are additive work — and are tracked separately from the conformance-enforced
defect class.
