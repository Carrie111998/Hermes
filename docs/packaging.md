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

The build is validated by `tests/test_packaging_build_guard.py`. A successful
artifact build does not imply that the package has been published to an index,
that a container image exists, or that provider credentials are configured.

## Current boundary

Source installation and local artifact installation are supported. A published
installer, package-index release, and container-registry image remain release
work. Do not use the upstream Hermes installer for this independent project.
