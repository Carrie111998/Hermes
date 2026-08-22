### Summary

Adds a declarative plugin configuration field bridge for the web dashboard and
desktop settings UI.

Plugins can now expose provider-specific configuration fields without requiring
core UI changes for every new plugin.

### What changed

- Supports `config_fields` in `plugin.yaml`
- Normalizes declared fields into the existing plugin `config_schema`
- Merges fully-qualified plugin fields into `/api/config/schema`
- Supports field metadata:
  - type
  - label
  - description
  - static options
- Desktop voice settings discover additional `stt.<provider>.*` and
  `tts.<provider>.*` fields from the backend schema
- Existing fields found in configuration remain visible even when they are not
  part of the static built-in field list
- Plugin-registered STT providers are included in the provider picker
- Core schema fields take precedence over plugin declarations
- Duplicate declarations between `config_fields` and `config_schema` are handled
  deterministically, with `config_schema` taking precedence
- Provider model discovery is not performed synchronously from the schema
  endpoint, preventing plugin code from blocking the web-server event loop

### Compatibility

- Existing `config_schema` manifests continue to work
- Existing static desktop voice fields remain unchanged
- Plugin fields are limited to fully-qualified configuration keys
- Core configuration schema entries cannot be overridden by plugins
- Invalid declarations remain non-fatal and are ignored or reported as warnings

### Tests

Added coverage for:

- `config_fields` manifest parsing
- `config_fields` and `config_schema` merging
- duplicate declaration precedence
- dashboard schema exposure
- core schema collision protection
- dynamic desktop voice field discovery
- configuration-presence fallback

Verification:

- Python tests: `232 passed, 1 skipped`
- Desktop tests: `8 passed`
- TypeScript compilation: passed
- Ruff: passed

Closes #87935
