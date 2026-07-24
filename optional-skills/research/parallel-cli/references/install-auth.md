# Parallel CLI — Installation & Authentication

---

## Installation

Try the least invasive install path available for the environment.

### Homebrew

```bash
brew install parallel-web/tap/parallel-cli
```

### npm

```bash
npm install -g parallel-web-cli
```

### Python package

```bash
pip install "parallel-web-tools[cli]"
```

### Standalone installer

```bash
curl -fsSL https://parallel.ai/install.sh | bash
```

If you want an isolated Python install, `pipx` can also work:

```bash
pipx install "parallel-web-tools[cli]"
pipx ensurepath
```

## Authentication

Interactive login:

```bash
parallel-cli login
```

Headless / SSH / CI:

```bash
parallel-cli login --device
```

API key environment variable:

```bash
export PARALLEL_API_KEY="***"
```

Verify current auth status:

```bash
parallel-cli auth
```

If auth requires browser interaction, run with `pty=true`.
