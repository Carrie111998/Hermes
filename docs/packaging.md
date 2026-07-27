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

## Current boundary

Source installation, local artifact installation, and the checked-in local
installer are supported. A published package-index release and
container-registry image remain release work. Do not use the upstream Hermes
installer for this independent project.
