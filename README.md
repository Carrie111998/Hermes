# Douglas Agent

<p align="center">
  <a href="https://github.com/NousResearch/hermes-agent/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="License: MIT"></a>
  <a href="https://github.com/NousResearch/hermes-agent"><img src="https://img.shields.io/badge/Built%20on-Hermes%20Agent-blueviolet?style=for-the-badge" alt="Built on Hermes Agent"></a>
  <a href="README.es.md"><img src="https://img.shields.io/badge/Lang-Español-orange?style=for-the-badge" alt="Español"></a>
</p>

**Douglas Agent** is an AI agent for content creation and publishing, built on top of [Hermes Agent](https://github.com/NousResearch/hermes-agent) by [Nous Research](https://nousresearch.com), used under the MIT License.

> Douglas Agent is a product built *on* Hermes — not a replacement for it, and not affiliated with or endorsed by Nous Research. Everything Hermes already does (terminal, scheduler, messaging platforms, memory, skills, plugins, multi-profile isolation, and more) is inherited as-is; Douglas adds its own layer on top for content workflows. See [`CAPABILITIES.md`](CAPABILITIES.md) for a full inventory of what's inherited versus what's new.

---

## Install

Douglas Agent doesn't ship its own installer yet — it installs through Hermes's real infrastructure, and `douglas` works as a full alias for `hermes` from the moment install finishes (same binary, same config, same everything — see [`douglas/README.md`](douglas/README.md) for how the compatibility layer works).

### Linux, macOS, WSL2, Termux

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
```

### Windows (native, PowerShell)

```powershell
iex (irm https://hermes-agent.nousresearch.com/install.ps1)
```

After installation:

```bash
source ~/.bashrc    # reload shell (or: source ~/.zshrc)
douglas             # start chatting — identical to `hermes`
```

Data lives under `~/.douglas` (or `%LOCALAPPDATA%\douglas` on Windows) by default, falling back to an existing `~/.hermes` install if you already have one — see [`douglas/README.md`](douglas/README.md) for the full resolution chain. `DOUGLAS_HOME`, `DOUGLAS_*` env vars, and `douglas-config.yaml`-shaped overrides all work as documented there.

For install troubleshooting (antivirus false positives, Windows-specific issues, Termux notes), see the [upstream Hermes README](https://github.com/NousResearch/hermes-agent#readme) — the installer is identical, so the same steps apply.

---

## Getting Started

```bash
douglas              # Interactive CLI — start a conversation
douglas model        # Choose your LLM provider and model
douglas gateway       # Start the messaging gateway (Telegram, Discord, etc.)
douglas setup         # Run the full setup wizard
douglas doctor         # Diagnose any issues
```

Every `hermes` command and flag works identically as `douglas` — see the [Hermes CLI guide](https://hermes-agent.nousresearch.com/docs/user-guide/cli) for the full command reference, and [`CAPABILITIES.md`](CAPABILITIES.md) for what's already built before you build anything new.

---

## Why build on Hermes

Hermes Agent already ships a terminal UI, a messaging gateway (Telegram, Discord, Slack, WhatsApp, Signal, Email), a persistent cron scheduler, 8 memory providers, a skills/plugin system compatible with [agentskills.io](https://agentskills.io), multi-profile isolation, and 8 execution backends (local, Docker, SSH, Singularity, Modal, Daytona, Vercel Sandbox). Douglas Agent reuses all of it rather than rebuilding any of it — the [`douglas/`](douglas/) directory holds everything genuinely new (media hosting, social publishing, brand/content skills, billing), and [`douglas/CORE_PATCHES.md`](douglas/CORE_PATCHES.md) tracks every place Douglas had to touch Hermes's own code, with the reasoning for each.

---

## Development

Same setup as Hermes — see the [Hermes Contributing Guide](https://github.com/NousResearch/hermes-agent/blob/main/CONTRIBUTING.md) for the full development environment, code style, and test setup. Douglas-specific rules (what's safe to touch, what to never touch, how to keep `git merge upstream/main` working) live in [`douglas/README.md`](douglas/README.md) and the "DOUGLAS AGENT" section at the bottom of [`AGENTS.md`](AGENTS.md).

```bash
uv pip install -e ".[all,dev]"
scripts/run_tests.sh              # Hermes core test suite
pytest tests-douglas/ -q          # Douglas compatibility-layer tests
```

---

## Community & Support

Douglas Agent doesn't have its own support channels yet. For anything related to the underlying agent (bugs, questions about how a Hermes feature works), the [Hermes Discord](https://discord.gg/NousResearch) and [Hermes issue tracker](https://github.com/NousResearch/hermes-agent/issues) are the right place — please don't file Douglas-specific product issues there.

---

## License

MIT — see [LICENSE](LICENSE) and [NOTICE](NOTICE) for full attribution to Nous Research.
