## Community Memory Providers

Beyond the 8 built-in providers, the following **standalone memory provider plugins** are available as community-maintained packages. They integrate with Hermes via the same `MemoryProvider` ABC and plugin discovery system — install them into `~/.hermes/plugins/`, and Hermes picks them up automatically.

### Memex Zero RAG

[Memex Zero RAG](https://github.com/JPeetz/MeMex-Zero-RAG) is a citation-first knowledge system inspired by Karpathy's LLM Wiki. The [memex-hermes-plugin](https://github.com/JPeetz/memex-hermes-plugin) exposes it to Hermes as a memory provider that stores individual, cited **Facts** rather than free-form vector chunks.

:::warning v0.1.1 status
This plugin is at **v0.1.1** — file-backed local storage only, no PyPI package yet, install by git clone. HTTP/MCP backends, FTS/BM25 search, and time-based confidence decay are planned for **v0.2**. See the [CHANGELOG](https://github.com/JPeetz/memex-hermes-plugin/blob/main/CHANGELOG.md) for the honest status delta.
:::

| | |
|---|---|
| **Best for** | Multi-session project knowledge, citation-critical workflows, teams that want a single-writer store of hand-curated Facts rather than a vector recall soup |
| **Requires** | `git clone` into `~/.hermes/plugins/memex` + `MEMEX_ENDPOINT=file:///path` (or `hermes memory setup memex`) |
| **Data storage** | Local filesystem, mode `0o700`, JSON facts under the endpoint path |
| **Cost** | Free (open-source, see plugin repo for license) |
| **Tools** | `memex_search`, `memex_read`, `memex_list`, `memex_write`, `memex_flag`, `memex_revalidate` (six total; the last three are write-gated) |

**Key differentiators:**

- **Citations required for `source` / `entity` / `concept` facts** — `memex_write` raises `CitationRequiredError` if a citation field is missing on those fact types.
- **Write-gate on the primary agent** — mutating tools (`memex_write`, `memex_flag`, `memex_revalidate`) are enabled **only** when the plugin is loaded by the primary agent. Subagents and cron jobs get read-only access, preserving a single-writer invariant on the shared store.
- **Bounded prefetch** — on every turn Hermes calls `prefetch(query)`; the plugin runs a bounded 2-second `memex_search()` on a daemon worker thread and injects the top-5 FactRefs as a `<memex-context>` block into the system prompt. Timeouts, empty results, and client failures all yield an empty block silently — no errors surface to the model.
- **Immutable + flag/revalidate workflow** — facts can be flagged for revalidation, then confirmed, updated, or retired via `memex_revalidate`. Attempting to mutate an immutable fact raises `ImmutableFactError`.
- **Local-first, no API key required in v0.1** — `MEMEX_API_KEY` is reserved for the HTTP endpoints landing in v0.2.
- **Atomic + mode-0600 config writes** — the fallback `~/.hermes/memex.json` writer uses `tempfile.mkstemp` + `fchmod 0o600` + atomic rename; no mode-0644 window.

**Setup:**

```bash
# Clone the plugin into the Hermes plugin directory
git clone https://github.com/JPeetz/memex-hermes-plugin.git ~/.hermes/plugins/memex

# Option A — env var (activates on next Hermes start)
export MEMEX_ENDPOINT=file:///path/to/your/memex-store

# Option B — interactive setup (writes ~/.hermes/memex.json)
hermes memory setup memex

# Verify
hermes memory status   # should show: memex — Status: available ✓
```

The plugin activates when `MEMEX_ENDPOINT` is set in the environment **or** when `~/.hermes/memex.json` contains an `endpoint` value (whichever is present; env var wins).

**Configuration (env vars):**

| Env var | Required | Default | Purpose |
|---|---|---|---|
| `MEMEX_ENDPOINT` | yes | — | Where the fact store lives. `file://...` in v0.1; HTTP/MCP in v0.2+. |
| `MEMEX_API_KEY` | no | — | Reserved for HTTP endpoints (v0.2+). |
| `MEMEX_SESSION_SCOPE` | no | `profile` | `session` / `profile` / `global`. Honored in v0.2+. |
| `MEMEX_PREFETCH_TIMEOUT` | no | `2.0` | Prefetch deadline in seconds. Must be a positive finite float. |

**Config file:** `~/.hermes/memex.json` (fallback when env vars are unset)

```json
{
  "endpoint": "file:///path/to/your/memex-store",
  "api_key": null
}
```

Endpoint resolution precedence (fixed in v0.1.1): `MEMEX_ENDPOINT` env var → `~/.hermes/memex.json` → fallback of `~/.hermes/memex/`.

**Known limitations in v0.1.1:**

- **`file://` endpoints only** — HTTP/MCP backends land in v0.2.
- **Substring scan, no BM25** — `memex_search` is a substring match over title/body/tags. Full-text search is v0.2.
- **No in-plugin decay maths** — confidence is stored as provided; time-based decay is a backend concern for v0.2+.
- **`memex_list` capped at 500 rows** at the tool boundary. Larger stores must paginate via `tags` / `types` / `statuses` filters.
- **`statuses=["retired"]` returns empty** — the client hides retired facts from `list()` before the tool-boundary post-filter runs.
- **Single-writer store** — file locking is advisory; concurrent multi-process writes will race.

**One-external-provider invariant:** per Hermes PLUGIN-ABI §Q1, only one external memory provider may be active per Hermes session. If another external provider is already registered, Hermes will refuse to enable this one. Run `hermes memory list` to see which is active.

**Plugin source:** [github.com/JPeetz/memex-hermes-plugin](https://github.com/JPeetz/memex-hermes-plugin) · [DESIGN.md](https://github.com/JPeetz/memex-hermes-plugin/blob/main/DESIGN.md) · [CHANGELOG.md](https://github.com/JPeetz/memex-hermes-plugin/blob/main/CHANGELOG.md)

---

To add your own community memory provider, publish a standalone plugin following the [Memory Provider Plugin guide](/developer-guide/memory-provider-plugin) and open a PR to add it to this section.
