# Charterforge packaging

Charterforge is independently installable as a Python wheel or source
distribution. The package name is `charterforge`; the legacy `hermes*` entry
points remain migration aliases.

Build both artifacts from the repository root:

```bash
uv build --wheel --sdist --out-dir /tmp/charterforge-dist
```

Install a wheel into an isolated environment and verify the CLI:

```bash
uv venv /tmp/charterforge-venv --python 3.13
uv pip install --python /tmp/charterforge-venv/bin/python \
  /tmp/charterforge-dist/charterforge-0.19.0-py3-none-any.whl
CHARTERFORGE_HOME=$(mktemp -d) \
  /tmp/charterforge-venv/bin/charterforge --version
```

For a local, repeatable installation without using the upstream Hermes
installer, use the independent installer against a wheel, sdist, or checkout:

```bash
CHARTERFORGE_INSTALL_DIR="$HOME/.local/share/charterforge" \
  scripts/install-charterforge.sh /path/to/charterforge-0.19.0-py3-none-any.whl
CHARTERFORGE_HOME="$HOME/.charterforge" \
  "$HOME/.local/share/charterforge/bin/charterforge" --version
```

The installer creates an isolated Python 3.13 environment by default, refuses
to reuse a non-empty non-venv destination, and supports
`CHARTERFORGE_PYTHON` when an alternate compatible interpreter is required.

The build is validated by `tests/test_packaging_build_guard.py`. A successful
artifact build does not imply that the package has been published to an index,
that a container image exists, or that provider credentials are configured.

Local evidence from current `main` (`3b1023e37167b2b19738b084eb4552abe303c9e1`)
on 2026-07-27:

```text
uv build --wheel --sdist --out-dir /tmp/charterforge-dist-current
uv venv /tmp/charterforge-artifact-venv-current --python 3.13
uv pip install --python /tmp/charterforge-artifact-venv-current/bin/python \
  /tmp/charterforge-dist-current/charterforge-0.19.0-py3-none-any.whl
/tmp/charterforge-artifact-venv-current/bin/charterforge --version
Charterforge v0.19.0 (2026.7.20)
```

`.github/workflows/charterforge-artifacts.yml` repeats the wheel/sdist build and
isolated `charterforge --version` install smoke on pull requests, `main`, and
version-tag pushes, then retains the artifacts in GitHub Actions. It is an
artifact pipeline, not an automatic package-index publication; publication
still requires an explicitly configured trusted-publishing environment.

The independent `.github/workflows/charterforge-container.yml` similarly builds
the Docker image on pull requests, `main`, and version-tag pushes, runs the
container CLI smoke, and retains the image identity as an artifact. It does not
push to a registry; registry publication remains a separately authorized
deployment action.

## Current boundary

Source installation, local artifact installation, the checked-in local
installer, and independent artifact/container CI verification are supported.
A published package-index release and container-registry image remain release
work. Do not use the upstream Hermes installer for this independent project.
