# Hermes Route Authority

This directory is the versioned authority for the local Hermes semantic route
bridge and its production registry.

- `manage.py` verifies and reapplies the source overlay after an upstream
  Hermes update.
- `manifest.json` records the exact patch and managed-file checksums.
- `registry/` contains workflow policy only; provider credentials and session
  state remain in Hermes configuration and runtime data directories.

The installed checkout at `/Users/zhengsy/.hermes/hermes-agent` is a managed
runtime target. It may be replaced by an upstream update, so local source
changes must be authored here and reapplied through the authority manager.
