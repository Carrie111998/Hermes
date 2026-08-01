# RCA: Hermes Agent fails on corporate TLS-inspecting proxy networks

**Status:** fixed by `fix(agent): inject OS trust store via truststore on Windows/macOS for corporate CA support`
**Severity:** P2 — Hermes Agent is unusable on any corporate network that deploys a TLS-inspecting proxy via Group Policy / MDM. No data loss, but a full availability outage for affected operators.

## Summary

On corporate networks with TLS-inspecting proxies (common across enterprise deployments globally), IT deploys the proxy's root CA to the **operating system** trust store via Group Policy / MDM. Python's `ssl` module, however, defaults to the statically bundled `certifi` `cacert.pem`, which never sees the corporate root CA. Every outbound HTTPS call then fails with a certificate-verification error before Hermes can reach its provider.

## Symptoms

```
SSLError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
unable to get local issuer certificate (_ssl.c:1000)
```

Hermes fails at the first provider call. `hermes doctor` may pass (certifi bundle is healthy) but every `chat` / completion request fails.

## Root cause

1. **OS trust store vs. certifi mismatch.** Corporate / MDM root CAs land in:
   - Windows: `LocalMachine\Root` certificate store
   - macOS: System / login keychain

   Python's `ssl.create_default_context()` loads CAs from the `certifi` package's `cacert.pem`, which is a static PEM bundle shipped with the package. It does **not** consult the OS trust store. So the corporate proxy's interception CA — installed by the IT admin into the OS store — is invisible to Python, and TLS verification fails.

2. **`ssl_guard.py` false-positive on truststore contexts.** Even if an operator manually installed `truststore` and injected it, `agent/ssl_guard.py:_validate_bundle_path()` called `ctx.get_ca_certs()` and treated an empty / failing return as a corrupt bundle. When truststore injects an OS-backed `SSLContext`, `get_ca_certs()` raises `NotImplementedError` because the OS trust store doesn't expose CA enumeration the way a PEM-backed context does. This turned a working corporate-CA setup into a startup failure.

## Fix

Three coordinated, opt-in, non-breaking changes:

### 1. `agent/ssl_verify.py` — build a `truststore.SSLContext` on demand

Added `_build_truststore_context()` which returns an `ssl.SSLContext` backed by the OS trust store (Windows cert store, macOS keychain) when invoked. This is NOT a global injection — it's passed as the `verify=` argument to owned HTTP clients (httpx / OpenAI / requests) via the new `resolve_httpx_verify_with_truststore()` entry point, so truststore's CA set is applied ONLY to connections Hermes owns.

**Why not `truststore.inject_into_ssl()` at import time?** Truststore's own documentation warns libraries and packages against calling `inject_into_ssl()` as a side effect of import — it mutates a process-global default and cannot guarantee process-wide ordering for all import paths. Instead we construct a `truststore.SSLContext` and pass it through the existing `resolve_httpx_verify` resolution chain, which feeds the `verify=` parameter of the HTTP clients Hermes constructs. This respects Truststore's contract and keeps the change scoped to owned clients.

Gates (all must pass for the truststore context to be returned):
- `truststore` package is importable (opt-in via the new `[truststore]` extra)
- `sys.platform` is `win32` or `darwin` (Linux distros already bridge the OS store via `ca-certificates.crt` + certifi; truststore is a no-op there)
- Operator has set `network.trust_store: true` in `config.yaml`

Never raises — a failure returns `None` so callers fall back to the certifi default (`True`).

### 2. `agent/ssl_guard.py` — tolerate `NotImplementedError` from `get_ca_certs()`

`_validate_bundle_path()` now wraps `ctx.get_ca_certs()` in a `try/except NotImplementedError`. When truststore's OS-backed context can't enumerate CAs, we trust the load (the bundle loaded fine via `ssl.create_default_context(cafile=...)`) and skip the emptiness check rather than firing a false positive on a working corporate-CA setup. The genuine empty-list case (`certs == []`) still raises so a truly corrupt bundle is caught.

### 3. `pyproject.toml` — new optional `[truststore]` extra + `uv.lock` regenerated

```toml
truststore = ["truststore>=0.10.0,<2"]
```

Not included in `[all]` — opt-in, so default installs stay lean and the supply-chain blast radius is minimal. Pinned per the supply-chain policy (post-1.0, so `<2` ceiling). `uv.lock` regenerated with `truststore v0.10.4`.

### 4. `hermes_cli/config.py` — `network.trust_store` config key

Added `network.trust_store: false` to `DEFAULT_CONFIG`. Per `AGENTS.md:102-107`, non-secret behavioral settings belong in `config.yaml`, not `.env` (which is for secrets only). The opt-in is a config key, not an env var.

Wired into the two concrete call sites:
- `agent/agent_runtime_helpers.py:create_openai_client()` — reads `network.trust_store` from `load_config_readonly()` and passes it to `resolve_httpx_verify_with_truststore()`.
- `agent/auxiliary_client.py:_resolve_aux_verify()` — same pattern, so auxiliary calls (compression, vision, web_extract, title generation) honour the same setting.

## Recovery

For an operator on a corporate TLS-inspecting proxy network:

```bash
# 1. Install the optional truststore extra
uv pip install -e ".[truststore]"

# 2. Enable in config.yaml
#    (usually ~/.hermes/config.yaml)
#    network:
#      trust_store: true

# 3. Verify
hermes doctor
hermes chat -q "Hello"
```

If `truststore` injection is not desired (e.g. a sandboxed environment ships its own trust store), simply leave `network.trust_store: false` (the default) or remove the `[truststore]` extra — the behaviour is unchanged.

## Security note

This does **not** weaken TLS verification or introduce a vulnerability (per `SECURITY.md` §3.2):

- `truststore.SSLContext()` only **adds** the OS trust store to Python's CA set — it does not disable verification, pin certs, or trust arbitrary CAs. It makes Python behave like browsers, curl, and PowerShell on that OS.
- The corporate root CA was deployed by the operator's IT admin via Group Policy / MDM — the same trust authority that already controls the OS. Trusting it in Python aligns Python with the trust decision the admin already made; it does not grant any new trust.
- The approach passes the `truststore.SSLContext` as the `verify=` argument to HTTP clients Hermes owns — it does NOT globally mutate `ssl.create_default_context()` via `inject_into_ssl()`, which respects Truststore's own guidance against library-level global injection.
- All existing escape hatches (`HERMES_SKIP_SSL_GUARD`, `ssl_verify: false` per-provider) are unchanged and still work.
- TLS verification errors on corporate proxies are an availability/usability issue, not a security vulnerability — they prevent legitimate connections without enabling any new attack. See `SECURITY.md` §3.2 "Documented break-glass settings."

## Environment

- Reported: Windows 10, enterprise corporate network, TLS-inspecting proxy with MDM-deployed root CA.
- Generalizes to: any Windows / macOS deployment behind a TLS-inspecting proxy where the interception CA is in the OS trust store but not in certifi.
- Linux is unaffected (distros bridge `/etc/ssl/certs/ca-certificates.crt` into certifi already); truststore is skipped there even when `network.trust_store` is enabled.

## References

- `SECURITY.md` §2.2 (OS-level isolation is the only boundary), §3.2 (break-glass settings out of scope)
- `docs/rca-ssl-cacert-post-git-pull.md` (prior SSL guard RCA — the guard this change extends)
- `CONTRIBUTING.md` "Dependency pinning policy" (supply-chain hardening — this PR adds a bounded `<2` dep)
- `AGENTS.md:102-107` (non-secret config goes in `config.yaml`, not `.env`)
- Truststore docs: https://truststore.readthedocs.io/
