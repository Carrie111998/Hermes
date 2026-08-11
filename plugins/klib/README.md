# klib

Search the klib knowledge library from Telegram (or any Hermes gateway) with
the `/klib` slash command.

The plugin is disabled unless it is enabled in the user's Hermes plugin
configuration and the `klib` block is configured in `~/.hermes/config.yaml`:

```yaml
klib:
  enabled: true
  base_url: "http://127.0.0.1:8765"
  key_file: "/path/to/klib-api-key"
```

## Usage

```text
/klib <query>
/klib read <path>
/klib semantic <query>
```

Queries use klib's lexical search mode by default and show at most five
distinct files per page. Searches with more than five distinct results include
Telegram Next/Prev buttons. Pagination uses KLIB's cursor contract, so the
Next button fetches another page instead of relying on a fixed local overfetch.
Older KLIB responses without pagination metadata retain a bounded local
fallback. Prefix a query with the case-sensitive
`semantic ` keyword to use klib's semantic search mode instead.
Repeated line hits from the same file are collapsed using the first hit in
klib's response. The `read` form fetches and returns the full page text, with
the Telegram reply capped at 2800 characters.

Klib returns query text, file paths, snippets, and page content as raw text.
When KLIB supplies verified Google Drive provenance, each result also includes
a `[Google Drive](...)` link. Missing or unverified provenance is shown without
inventing a URL.
The downstream gateway formatter applies MarkdownV2 escaping once; result file
labels are marked bold with standard `**label**` syntax.

Pagination sessions are kept in memory for 30 minutes and are bound to the
Telegram chat that started the search. Expired, unknown, invalid, or
cross-chat pagination callbacks are rejected without revealing search results.

The `read` sub-command recognizes a case-sensitive `read ` prefix. As a
consequence, a search phrase beginning with those exact characters (for
example, `/klib read the manual`) is interpreted as a page path.

The `semantic` mode recognizes a case-sensitive `semantic ` prefix. As a
consequence, a search phrase beginning with those exact characters (for
example, `/klib semantic architecture`) is interpreted as a semantic-mode
query rather than a literal query beginning with `semantic`; that ambiguity
is an accepted tradeoff for simple slash dispatch. The `semantic ` prefix is
distinct from the `read ` prefix, so it does not select page-reading mode.

When `key_file` is set, the plugin reads and trims the file contents and sends
them as the `Authorization: Bearer <key>` HTTP request header. If `key_file` is omitted, the
request is sent without authentication.
