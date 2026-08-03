<p align="center">
  <img src="assets/iyari-logo-completo.png" alt="IYARI — tu aliado en cada decisión" width="420">
</p>

<p align="center">
  <a href="https://iyari.io"><img src="https://img.shields.io/badge/web-iyari.io-orange" alt="iyari.io"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/built%20by-Digital%20Services%20LLC-blue" alt="Built by Digital Services LLC">
</p>

# IYARI

**Tu aliado en cada decisión.**

IYARI is a self-improving AI agent built by **Digital Services LLC**. It creates skills from experience, improves them during use, persists knowledge across sessions, searches its own past conversations, and builds a deepening model of who you are over time.

Run it on a $5 VPS, a GPU cluster, or serverless infrastructure that costs nearly nothing when idle. It's not tied to your laptop — talk to it from Telegram while it works on a cloud VM.

IYARI is a fork of [Hermes Agent](https://github.com/NousResearch/hermes-agent), originally built by Nous Research, developed and maintained independently by Digital Services LLC under the MIT license.

<table>
<tr><td><b>A real terminal interface</b></td><td>Full TUI with multiline editing, slash-command autocomplete, conversation history, interrupt-and-redirect, and streaming tool output.</td></tr>
<tr><td><b>Lives where you do</b></td><td>Telegram, Discord, Slack, WhatsApp, Signal, and CLI — all from a single gateway process.</td></tr>
<tr><td><b>A closed learning loop</b></td><td>Agent-curated memory with periodic nudges, autonomous skill creation, FTS5 session search with LLM summarization, <a href="https://github.com/plastic-labs/honcho">Honcho</a> dialectic user modeling. Compatible with the <a href="https://agentskills.io">agentskills.io</a> open standard.</td></tr>
<tr><td><b>Scheduled automations</b></td><td>Built-in cron scheduler with delivery to any platform — reports, backups, audits, all in natural language, running unattended.</td></tr>
<tr><td><b>Delegates and parallelizes</b></td><td>Spawn isolated subagents for parallel workstreams; call tools programmatically from Python scripts to collapse multi-step pipelines into a single turn.</td></tr>
<tr><td><b>Runs anywhere, not just your laptop</b></td><td>Local, Docker, SSH, Daytona, Singularity, Modal, and Vercel Sandbox terminal backends. Daytona and Modal offer serverless persistence — your environment hibernates when idle and wakes on demand.</td></tr>
<tr><td><b>Native Windows support</b></td><td>Runs without WSL — CLI, gateway, TUI, and tools all work natively via bundled Git Bash for shell commands.</td></tr>
</table>

## Use any model you want

Nous Portal, OpenRouter, OpenAI, DeepSeek, Kimi, your own endpoint, and many others. Switch with `hermes model` — no code changes, no lock-in.

## Quickstart

```bash
git clone https://github.com/digital-services-llc/iyari.git
cd iyari
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv && uv pip install -e ".[all]"
hermes setup
```

> **Note:** during the brand transition, the CLI command remains `hermes`. Everything else — docs, web, branding — is IYARI.

## Documentation

📖 Full docs: **[iyari.io](https://iyari.io)**

## Community

- 🐛 [Issues](https://github.com/digital-services-llc/iyari/issues) — bug reports and feature requests go here, in our own repo
- 🌐 [iyari.io](https://iyari.io)
- ✉️ team@iyari.io

## License

MIT — see [LICENSE](LICENSE).

Originally built by [Nous Research](https://nousresearch.com/). Fork maintained by **Digital Services LLC** — © 2026.

*Iyari — contigo cada día.*
