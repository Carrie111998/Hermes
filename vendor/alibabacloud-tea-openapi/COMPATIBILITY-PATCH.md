# Alibaba OpenAPI cryptography compatibility patch

This directory vendors the published `alibabacloud-tea-openapi==0.4.5`
source distribution used by Hermes's optional DingTalk integration.

## Provenance

- Upstream project: <https://github.com/aliyun/darabonba-openapi>
- Published artifact: `alibabacloud_tea_openapi-0.4.5.tar.gz`
- PyPI source SHA-256:
  `75fa1f4360a46e41f5bf5f8d4917e52efb6f64885839bc1328c35590670c97b9`
- License: Apache-2.0; the full license is preserved in `LICENSE`.

## Hermes patch

All upstream-code changes are confined to packaging metadata in `setup.py`.
For Python 3.9 and newer, the dependency bound changes as follows:

```diff
- cryptography>=3.0.0, <49.0.0
+ cryptography>=3.0.0, <51.0.0
```

The wheel also installs `LICENSE`, `README.md`, and this compatibility notice
under `share/doc/alibabacloud-tea-openapi`. This keeps license, provenance,
rationale, verification, and removal criteria in both source and wheel
artifacts; it does not alter the importable package.

No library implementation files are changed. The 0.4.5 implementation's
`rsa_sign` directly imports and uses `cryptography` for PEM-key loading,
PKCS#1 v1.5 padding, and SHA-256 signing. Hermes therefore verifies the real
RSA signing path under cryptography 50 rather than relying on import-only
coverage.

## Rationale

`cryptography==48.0.1` is affected by GHSA-g6cj-pr64-35w5,
GHSA-jwv3-5hgf-82ww, and GHSA-m2h6-j472-rp4c. Version 50.0.0 fixes all three.
Alibaba's current package metadata still caps the dependency below 49 even
though the affected OpenAPI signing/client paths remain compatible with 50.
The local metadata patch lets Hermes retain DingTalk while producing a
consistent, vulnerability-remediated environment that passes `pip check`.

## Verification and removal

Before updating this vendor package, compare its payload with the published
source distribution and reapply only the metadata change. Verify with:

```bash
uv lock --upgrade-package cryptography --upgrade-package msal
UV_PROJECT_ENVIRONMENT=venv uv sync --locked --extra dingtalk --extra dev
venv/bin/python -m pip check
scripts/run_tests.sh tests/gateway/test_dingtalk.py tests/hermes_cli/test_dingtalk_auth.py
hermes security audit --json
```

Remove this vendor package and the `[tool.uv.sources]` entry as soon as an
official `alibabacloud-tea-openapi` release permits `cryptography>=50`.
