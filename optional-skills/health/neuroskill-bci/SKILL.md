---
name: neuroskill-bci
description: >
  Connect to a running NeuroSkill instance and incorporate the user's real-time
  cognitive and emotional state (focus, relaxation, mood, cognitive load, drowsiness,
  heart rate, HRV, sleep staging, and 40+ derived EXG scores) into responses.
  Requires a BCI wearable (Muse 2/S or OpenBCI) and the NeuroSkill desktop app
  running locally.
platforms: [linux, macos, windows]
version: 1.0.0
author: Hermes Agent + Nous Research
license: MIT
metadata:
  hermes:
    tags: [BCI, neurofeedback, health, focus, EEG, cognitive-state, biometrics, neuroskill]
    category: health
    related_skills: []
---

# NeuroSkill BCI Integration

Connect Hermes to a running [NeuroSkill](https://neuroskill.com/) instance to read
real-time brain and body metrics from a BCI wearable. Use this to give
cognitively-aware responses, suggest interventions, and track mental performance
over time.

> **⚠️ Research Use Only** — NeuroSkill is an open-source research tool. It is
> NOT a medical device and has NOT been cleared by the FDA, CE, or any regulatory
> body. Never use these metrics for clinical diagnosis or treatment.

## Routing

| Intent | Read |
|--------|------|
| Metric definitions, bands, ratios, HRV, sleep staging, composite state patterns, ZUNA embeddings | `references/metrics.md` |
| Full interpretation guide and healthy target ranges | `references/metrics.md` |
| Any guided protocol (70+ practices), trigger table, contraindications, execution guidelines | `references/protocols.md` |
| WebSocket/HTTP API, server discovery, event payloads, JSON response shapes, `jq` snippets | `references/api.md` |
| Per-command CLI recipes: `session`, `sessions`, `search`, `search-labels`, `interactive`, `compare`, `sleep`, `label`, `listen`, `umap`, `timer`, `calibrate`, `notify`, `raw` | `references/api.md` (CLI Task Recipes) |
| Full `status` response field list beyond `scores` | `references/api.md` (CLI Task Recipes) |
| Worked example interactions ("How am I doing?", "How did I sleep?", ...) | `references/api.md` (CLI Task Recipes) |

---

## Prerequisites

- **Node.js 20+** installed (`node --version`)
- **NeuroSkill desktop app** running with a connected BCI device
- **BCI hardware**: Muse 2, Muse S, or OpenBCI (4-channel EEG + PPG + IMU via BLE)
- `npx neuroskill status` returns data without errors

### Verify Setup
```bash
node --version                    # Must be 20+
npx neuroskill status             # Full system snapshot
npx neuroskill status --json      # Machine-parseable JSON
```

If `npx neuroskill status` returns an error, tell the user:
- Make sure the NeuroSkill desktop app is open
- Ensure the BCI device is powered on and connected via Bluetooth
- Check signal quality — green indicators in NeuroSkill (≥0.7 per electrode)
- If `command not found`, install Node.js 20+

---

## CLI Reference: `npx neuroskill <command>`

All commands support `--json` (raw JSON, pipe-safe) and `--full` (human summary + JSON).

| Command | Description |
|---------|-------------|
| `status` | Full system snapshot: device, scores, bands, ratios, sleep, history |
| `session [N]` | Single session breakdown with first/second half trends (0=most recent) |
| `sessions` | List all recorded sessions across all days |
| `search` | ANN similarity search for neurally similar historical moments |
| `compare` | A/B session comparison with metric deltas and trend analysis |
| `sleep [N]` | Sleep stage classification (Wake/N1/N2/N3/REM) with analysis |
| `label "text"` | Create a timestamped annotation at the current moment |
| `search-labels "query"` | Semantic vector search over past labels |
| `interactive "query"` | Cross-modal 4-layer graph search (text → EXG → labels) |
| `listen` | Real-time event streaming (default 5s, set `--seconds N`) |
| `umap` | 3D UMAP projection of session embeddings |
| `calibrate` | Open calibration window and start a profile |
| `timer` | Launch focus timer (Pomodoro/Deep Work/Short Focus presets) |
| `notify "title" "body"` | Send an OS notification via the NeuroSkill app |
| `raw '{json}'` | Raw JSON passthrough to the server |

### Global Flags
| Flag | Description |
|------|-------------|
| `--json` | Raw JSON output (no ANSI, pipe-safe) |
| `--full` | Human summary + colorized JSON |
| `--port <N>` | Override server port (default: auto-discover, usually 8375) |
| `--ws` | Force WebSocket transport |
| `--http` | Force HTTP transport |
| `--k <N>` | Nearest neighbors count (search, search-labels) |
| `--seconds <N>` | Duration for listen (default: 5) |
| `--trends` | Show per-session metric trends (sessions) |
| `--dot` | Graphviz DOT output (interactive) |

---

## 1. Checking Current State

### Get Live Metrics
```bash
npx neuroskill status --json
```

**Always use `--json`** for reliable parsing. The default output is colorized
human-readable text.

### Interpreting the Output

Parse the JSON and translate metrics into natural language. Never report raw
numbers alone — always give them meaning:

**DO:**
> "Your focus is solid right now at 0.70 — that's flow state territory. Heart
> rate is steady at 68 bpm and your FAA is positive, which suggests good
> approach motivation. Great time to tackle something complex."

**DON'T:**
> "Focus: 0.70, Relaxation: 0.40, HR: 68"

Key interpretation thresholds (see `references/metrics.md` for the full guide):
- **Focus > 0.70** → flow state territory, protect it
- **Focus < 0.40** → suggest a break or protocol
- **Drowsiness > 0.60** → fatigue warning, micro-sleep risk
- **Relaxation < 0.30** → stress intervention needed
- **Cognitive Load > 0.70 sustained** → mind dump or break
- **TBR > 1.5** → theta-dominant, reduced executive control
- **FAA < 0** → withdrawal/negative affect — consider FAA rebalancing
- **SNR < 3 dB** → unreliable signal, suggest electrode repositioning

---

## 2. Proactive State Awareness

### Session Start Check
At the beginning of a session, optionally run a status check if the user mentions
they're wearing their device or asks about their state:
```bash
npx neuroskill status --json
```

Inject a brief state summary:
> "Quick check-in: focus is building at 0.62, relaxation is good at 0.55, and your
> FAA is positive — approach motivation is engaged. Looks like a solid start."

### When to Proactively Mention State

Mention cognitive state **only** when:
- User explicitly asks ("How am I doing?", "Check my focus")
- User reports difficulty concentrating, stress, or fatigue
- A critical threshold is crossed (drowsiness > 0.70, focus < 0.30 sustained)
- User is about to do something cognitively demanding and asks for readiness

**Do NOT** interrupt flow state to report metrics. If focus > 0.75, protect the
session — silence is the correct response.

---

## 3. Suggesting Protocols

When metrics indicate a need, suggest a protocol from `references/protocols.md`.
Always ask before starting — never interrupt flow state:

> "Your focus has been declining for the past 15 minutes and TBR is climbing past
> 1.5 — signs of theta dominance and mental fatigue. Want me to walk you through
> a Theta-Beta Neurofeedback Anchor? It's a 90-second exercise that uses rhythmic
> counting and breath to suppress theta and lift beta."

Key triggers:
- **Focus < 0.40, TBR > 1.5** → Theta-Beta Neurofeedback Anchor or Box Breathing
- **Relaxation < 0.30, stress_index high** → Cardiac Coherence or 4-7-8 Breathing
- **Cognitive Load > 0.70 sustained** → Cognitive Load Offload (mind dump)
- **Drowsiness > 0.60** → Ultradian Reset or Wake Reset
- **FAA < 0 (negative)** → FAA Rebalancing
- **Flow State (focus > 0.75, engagement > 0.70)** → Do NOT interrupt
- **High stillness + headache_index** → Neck Release Sequence
- **Low RMSSD (< 25ms)** → Vagal Toning

## Error Handling

| Error | Likely Cause | Fix |
|-------|-------------|-----|
| `npx neuroskill status` hangs | NeuroSkill app not running | Open NeuroSkill desktop app |
| `device.state: "disconnected"` | BCI device not connected | Check Bluetooth, device battery |
| All scores return 0 | Poor electrode contact | Reposition headband, moisten electrodes |
| `signal_quality` values < 0.7 | Loose electrodes | Adjust fit, clean electrode contacts |
| SNR < 3 dB | Noisy signal | Minimize head movement, check environment |
| `command not found: npx` | Node.js not installed | Install Node.js 20+ |

---

## References

- [NeuroSkill Paper — arXiv:2603.03212](https://arxiv.org/abs/2603.03212) (Kosmyna & Hauptmann, MIT Media Lab)
- [NeuroSkill Desktop App](https://github.com/NeuroSkill-com/skill) (GPLv3)
- [NeuroLoop CLI Companion](https://github.com/NeuroSkill-com/neuroloop) (GPLv3)
- [MIT Media Lab Project](https://www.media.mit.edu/projects/neuroskill/overview/)
