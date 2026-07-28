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
```

Queries use klib's lexical search mode and show at most five distinct files.
Repeated line hits from the same file are collapsed using the first hit in
klib's response. The `read` form fetches and returns the full page text, with
the Telegram reply capped at 2800 characters.

The `read` sub-command recognizes a case-sensitive `read ` prefix. As a
consequence, a search phrase beginning with those exact characters (for
example, `/klib read the manual`) is interpreted as a page path.

When `key_file` is set, the plugin reads and trims the file contents and sends
them as the `Authorization: Bearer <key>` HTTP request header. If `key_file` is omitted, the
request is sent without authentication.
