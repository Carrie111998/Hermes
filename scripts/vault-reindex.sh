#!/usr/bin/env bash
set -euo pipefail
# Nightly incremental semantic reindex of all configured vaults (see
# ~/.hermes/vault_index/vaults.json). No LLM reasoning - pure embedding
# computation, safe for no-agent cron mode. Content-hash based, so
# unchanged files are skipped every run.
exec ~/.hermes/vault_index/venv/bin/python ~/.hermes/vault_index/index_vault.py
