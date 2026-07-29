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

Two coordinated changes, both opt-in and non-breaking on default installs:

### 1. `agent/process_bootstrap.py` — inject the OS trust store at boot

Added `_maybe_inject_truststore()`, called at module import time so the injection is in place before any `httpx` / `openai` / `requests` client is constructed downstream.

Gates (all must pass for injection to run):
- `truststore` package is importable (opt-in via the new `[truststore]` extra)
- `HERMES_DISABLE_TRUSTSTORE` is not set to a truthy value (escape hatch)
- `sys.platform` is `win32` or `darwin` (Linux distros already bridge the OS store via `ca-certificates.crt` + certifi; no-op there)

Never raises — a failure leaves the certifi default in place and `ssl_guard.py` surfaces a clear error.

### 2. `agent/ssl_guard.py` — tolerate `NotImplementedError` from `get_ca_certs()`

`_validate_bundle_path()` now wraps `ctx.get_ca_certs()` in a `try/except NotImplementedError`. When truststore's OS-backed context can't enumerate CAs, we trust the load (the bundle loaded fine via `ssl.create_default_context(cafile=...)`) and skip the emptiness check rather than firing a false positive on a working corporate-CA setup. The genuine empty-list case (`certs == []`) still raises so a truly corrupt bundle is caught.

### 3. `pyproject.toml` — new optional `[truststore]` extra

```toml
truststore = ["truststore>=0.10.0,<2"]
```

Not included in `[all]` — opt-in, so default installs stay lean and the supply-chain blast radius is minimal. Pinned per the supply-chain policy (post-1.0, so `<2` ceiling).

## Recovery

For an operator on a corporate TLS-inspecting proxy network:

```bash
# Install the optional truststore extra
uv pip install -e ".[truststore]"

# Verify
hermes doctor

# If doctor still flags a broken bundle, use the existing escape hatch:
HERMES_SKIP_SSL_GUARD=1 hermes chat
```

If `truststore` injection is not desired (e.g. a sandboxed environment ships its own trust store):

```bash
export HERMES_DISABLE_TRUSTSTORE=1
```

## Security note

This does **not** weaken TLS verification or introduce a vulnerability (per `SECURITY.md` §3.2):

- `truststore.inject_into_ssl()` only **adds** the OS trust store to Python's CA set — it does not disable verification, pin certs, or trust arbitrary CAs. It makes Python behave like browsers, curl, and PowerShell on that OS.
- The corporate root CA was deployed by the operator's IT admin via Group Policy / MDM — the same trust authority that already controls the OS. Trusting it in Python aligns Python with the trust decision the admin already made; it does not grant any new trust.
- All existing escape hatches (`HERMES_SKIP_SSL_GUARD`, `ssl_verify: false` per-provider) are unchanged and still work.
- TLS verification errors on corporate proxies are an availability/usability issue, not a security vulnerability — they prevent legitimate connections without enabling any new attack. See `SECURITY.md` §3.2 "Documented break-glass settings."

## Environment

- Reported: Windows 10, enterprise corporate network, TLS-inspecting proxy with MDM-deployed root CA.
- Generalizes to: any Windows / macOS deployment behind a TLS-inspecting proxy where the interception CA is in the OS trust store but not in certifi.
- Linux is unaffected (distros bridge `/etc/ssl/certs/ca-certificates.crt` into certifi already); truststore injection is skipped there.

## References

- `SECURITY.md` §2.2 (OS-level isolation is the only boundary), §3.2 (break-glass settings out of scope)
- `docs/rca-ssl-cacert-post-git-pull.md` (prior SSL guard RCA — the guard this change extends)
- `CONTRIBUTING.md` "Dependency pinning policy" (supply-chain hardening — this PR adds a bounded `<2` dep)
- truststore docs: https://truststore.readthedocs.io/
