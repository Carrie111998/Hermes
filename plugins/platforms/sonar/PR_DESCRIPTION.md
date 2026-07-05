# feat(gateway): Sonar platform plugin (sonar-cli NIP-44 DMs)

## Summary

Adds a first-class **Sonar** messaging platform for Hermes Gateway, using the official **`sonar-cli`** transport from [hedwig-corp/bitchat-to-sonar](https://github.com/hedwig-corp/bitchat-to-sonar).

Users get the same agent loop as Telegram (tools, memory, MCP, sessions, cron delivery) without a separate Python bridge subprocess per message.

## What's included

- `plugins/platforms/sonar/` — platform plugin (`adapter.py`, `plugin.yaml`, `README.md`)
- `tests/gateway/test_sonar_platform.py` — registration + chunking smoke tests

## How it works

- **Inbound:** `sonar-cli listen` NDJSON (`type=message`, `sender`, `content`)
- **Outbound:** `sonar-cli send --to <npub> --text ...` with multipart chunking (~3200 chars)
- **Auth:** `gateway.platforms.sonar.extra.authorized_senders` or `SONAR_ALLOWED_SENDERS`
- **Cron:** `deliver=sonar` via `SONAR_HOME_CHANNEL` + `standalone_sender_fn`

## Configuration example

```yaml
gateway:
  platforms:
    sonar:
      enabled: true
      extra:
        sonar_cli_home: ~/.sonar-agent
        display_name: "Hermes Agent · Sonar"
        authorized_senders:
          - npub1...
```

## Prerequisites

- `sonar-cli` on PATH
- `sonar-cli init && sonar-cli publish` (identity under `SONAR_CLI_HOME`)

## Testing

```bash
pytest tests/gateway/test_sonar_platform.py -q
hermes gateway restart
# DM agent npub from an authorized sender; journalctl -u hermes-gateway -f
```

## Related

- Sonar CLI JSON contract should stay documented in **bitchat-to-sonar** (`docs/integrations/hermes.md`)
- Community skill `sonar-hermes-bridge` remains a legacy standalone bridge; gateway plugin is the long-term path

## Checklist

- [x] Follows `gateway/platforms/ADDING_A_PLATFORM.md` plugin pattern (like IRC/ntfy)
- [x] `register_platform` with env_enablement, apply_yaml_config, standalone_sender
- [x] Plain-text platform hint (no markdown in Sonar DMs)
- [ ] Maintainer review: naming, security defaults (allowlist required unless `SONAR_ALLOW_ALL_USERS`)