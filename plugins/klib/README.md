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
```

Queries use klib's lexical search mode and show at most the top five results.

When `key_file` is set, the plugin reads and trims the file contents and sends
them as the `Authorization: Bearer <key>` HTTP request header. If `key_file` is omitted, the
request is sent without authentication.
