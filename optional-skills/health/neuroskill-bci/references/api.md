# NeuroSkill WebSocket & HTTP API Reference

NeuroSkill runs a local server (default port **8375**) discoverable via mDNS
(`_skill._tcp`). It exposes both WebSocket and HTTP endpoints.

---

## Server Discovery

```bash
# Auto-discovery (built into the CLI — usually just works)
npx neuroskill status --json

# Manual port discovery
NEURO_PORT=$(lsof -i -n -P | grep neuroskill | grep LISTEN | awk '{print $9}' | cut -d: -f2 | head -1)
echo "NeuroSkill on port: $NEURO_PORT"
```

The CLI auto-discovers the port. Use `--port <N>` to override.

---

## HTTP REST Endpoints

### Universal Command Tunnel
```bash
# POST / — accepts any command as JSON
curl -s -X POST http://127.0.0.1:8375/ \
  -H "Content-Type: application/json" \
  -d '{"command":"status"}'
```

### Convenience Endpoints
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/v1/status` | System status |
| GET | `/v1/sessions` | List sessions |
| POST | `/v1/label` | Create label |
| POST | `/v1/search` | ANN search |
| POST | `/v1/compare` | A/B comparison |
| POST | `/v1/sleep` | Sleep staging |
| POST | `/v1/notify` | OS notification |
| POST | `/v1/say` | Text-to-speech |
| POST | `/v1/calibrate` | Open calibration |
| POST | `/v1/timer` | Open focus timer |
| GET | `/v1/dnd` | Get DND status |
| POST | `/v1/dnd` | Force DND on/off |
| GET | `/v1/calibrations` | List calibration profiles |
| POST | `/v1/calibrations` | Create profile |
| GET | `/v1/calibrations/{id}` | Get profile |
| PATCH | `/v1/calibrations/{id}` | Update profile |
| DELETE | `/v1/calibrations/{id}` | Delete profile |

---

## WebSocket Events (Broadcast)

Connect to `ws://127.0.0.1:8375/` to receive real-time events:

### EXG (Raw EEG Samples)
```json
{"event": "EXG", "electrode": 0, "samples": [12.3, -4.1, ...], "timestamp": 1740412800.512}
```

### PPG (Photoplethysmography)
```json
{"event": "PPG", "channel": 0, "samples": [...], "timestamp": 1740412800.512}
```

### IMU (Inertial Measurement Unit)
```json
{"event": "IMU", "ax": 0.01, "ay": -0.02, "az": 9.81, "gx": 0.1, "gy": -0.05, "gz": 0.02}
```

### Scores (Computed Metrics)
```json
{
  "event": "scores",
  "focus": 0.70, "relaxation": 0.40, "engagement": 0.60,
  "rel_delta": 0.28, "rel_theta": 0.18, "rel_alpha": 0.32,
  "rel_beta": 0.17, "hr": 68.2, "snr": 14.3
}
```

### EXG Bands (Spectral Analysis)
```json
{"event": "EXG-bands", "channels": [...], "faa": 0.12}
```

### Labels
```json
{"event": "label", "label_id": 42, "text": "meditation start", "created_at": 1740413100}
```

### Device Status
```json
{"event": "muse-status", "state": "connected"}
```

---

## JSON Response Formats

### `status`
```jsonc
{
  "command": "status", "ok": true,
  "device": {
    "state": "connected",     // "connected" | "connecting" | "disconnected"
    "name": "Muse-A1B2",
    "battery": 73,
    "firmware": "1.3.4",
    "EXG_samples": 195840,
    "ppg_samples": 30600,
    "imu_samples": 122400
  },
  "session": {
    "start_utc": 1740412800,
    "duration_secs": 1847,
    "n_epochs": 369
  },
  "signal_quality": {
    "tp9": 0.95, "af7": 0.88, "af8": 0.91, "tp10": 0.97
  },
  "scores": {
    "focus": 0.70, "relaxation": 0.40, "engagement": 0.60,
    "meditation": 0.52, "mood": 0.55, "cognitive_load": 0.33,
    "drowsiness": 0.10, "hr": 68.2, "snr": 14.3, "stillness": 0.88,
    "bands": { "rel_delta": 0.28, "rel_theta": 0.18, "rel_alpha": 0.32, "rel_beta": 0.17, "rel_gamma": 0.05 },
    "faa": 0.042, "tar": 0.56, "bar": 0.53, "tbr": 1.06,
    "apf": 10.1, "coherence": 0.614, "mu_suppression": 0.031
  },
  "embeddings": { "today": 342, "total": 14820, "recording_days": 31 },
  "labels": { "total": 58, "recent": [{"id": 42, "text": "meditation start", "created_at": 1740413100}] },
  "sleep": { "total_epochs": 1054, "wake_epochs": 134, "n1_epochs": 89, "n2_epochs": 421, "n3_epochs": 298, "rem_epochs": 112, "epoch_secs": 5 },
  "history": { "total_sessions": 63, "recording_days": 31, "current_streak_days": 7, "total_recording_hours": 94.2, "longest_session_min": 187, "avg_session_min": 89 }
}
```

### `sessions`
```jsonc
{
  "command": "sessions", "ok": true,
  "sessions": [
    { "day": "20260224", "start_utc": 1740412800, "end_utc": 1740415510, "n_epochs": 541 },
    { "day": "20260223", "start_utc": 1740380100, "end_utc": 1740382665, "n_epochs": 513 }
  ]
}
```

### `session` (single session breakdown)
```jsonc
{
  "ok": true,
  "metrics": { "focus": 0.70, "relaxation": 0.40, "n_epochs": 541 /* ... ~50 metrics */ },
  "first":   { "focus": 0.64 /* first-half averages */ },
  "second":  { "focus": 0.76 /* second-half averages */ },
  "trends":  { "focus": "up", "relaxation": "down" /* "up" | "down" | "flat" */ }
}
```

### `compare` (A/B comparison)
```jsonc
{
  "command": "compare", "ok": true,
  "insights": {
    "deltas": {
      "focus": { "a": 0.62, "b": 0.71, "abs": 0.09, "pct": 14.5, "direction": "up" },
      "relaxation": { "a": 0.45, "b": 0.38, "abs": -0.07, "pct": -15.6, "direction": "down" }
    },
    "improved": ["focus", "engagement"],
    "declined": ["relaxation"]
  },
  "sleep_a": { /* sleep summary for session A */ },
  "sleep_b": { /* sleep summary for session B */ },
  "umap": { "job_id": "abc123" }
}
```

### `search` (ANN similarity)
```jsonc
{
  "command": "search", "ok": true,
  "result": {
    "results": [{
      "neighbors": [{ "distance": 0.12, "metadata": {"device": "Muse-A1B2", "date": "20260223"} }]
    }],
    "analysis": {
      "distance_stats": { "mean": 0.15, "min": 0.08, "max": 0.42 },
      "temporal_distribution": { /* hour-of-day distribution */ },
      "top_days": [["20260223", 5], ["20260222", 3]]
    }
  }
}
```

### `sleep` (sleep staging)
```jsonc
{
  "command": "sleep", "ok": true,
  "summary": { "total_epochs": 1054, "wake_epochs": 134, "n1_epochs": 89, "n2_epochs": 421, "n3_epochs": 298, "rem_epochs": 112, "epoch_secs": 5 },
  "analysis": { "efficiency_pct": 87.3, "onset_latency_min": 12.5, "rem_latency_min": 65.0, "bouts": { /* wake/n3/rem bout counts and durations */ } },
  "epochs": [{ "utc": 1740380100, "stage": 0, "rel_delta": 0.15, "rel_theta": 0.22, "rel_alpha": 0.38, "rel_beta": 0.20 }]
}
```

### `label`
```json
{"command": "label", "ok": true, "label_id": 42}
```

### `search-labels` (semantic search)
```jsonc
{
  "command": "search-labels", "ok": true,
  "results": [{
    "text": "deep focus block",
    "EXG_metrics": { "focus": 0.82, "relaxation": 0.35, "engagement": 0.75, "hr": 65.0, "mood": 0.60 },
    "EXG_start": 1740412800, "EXG_end": 1740412805,
    "created_at": 1740412802,
    "similarity": 0.92
  }]
}
```

### `umap` (3D projection)
```jsonc
{
  "command": "umap", "ok": true,
  "result": {
    "points": [{ "x": 1.23, "y": -0.45, "z": 2.01, "session": "a", "utc": 1740412800 }],
    "analysis": {
      "separation_score": 1.84,
      "inter_cluster_distance": 2.31,
      "intra_spread_a": 0.82, "intra_spread_b": 0.94,
      "centroid_a": [1.23, -0.45, 2.01],
      "centroid_b": [-0.87, 1.34, -1.22]
    }
  }
}
```

---

## Useful `jq` Snippets

```bash
# Get just focus score
npx neuroskill status --json | jq '.scores.focus'

# Get all band powers
npx neuroskill status --json | jq '.scores.bands'

# Check device battery
npx neuroskill status --json | jq '.device.battery'

# Get signal quality
npx neuroskill status --json | jq '.signal_quality'

# Find improving metrics after a session
npx neuroskill session 0 --json | jq '[.trends | to_entries[] | select(.value == "up") | .key]'

# Sort comparison deltas by improvement
npx neuroskill compare --json | jq '.insights.deltas | to_entries | sort_by(.value.pct) | reverse'

# Get sleep efficiency
npx neuroskill sleep --json | jq '.analysis.efficiency_pct'

# Find closest neural match
npx neuroskill search --json | jq '[.result.results[].neighbors[]] | sort_by(.distance) | .[0]'

# Extract TBR from labeled stress moments
npx neuroskill search-labels "stress" --json | jq '[.results[].EXG_metrics.tbr]'

# Get session timestamps for manual compare
npx neuroskill sessions --json | jq '{start: .sessions[0].start_utc, end: .sessions[0].end_utc}'
```

---

## Data Storage

- **Local database**: `~/.skill/YYYYMMDD/` (SQLite + HNSW index)
- **ZUNA embeddings**: 128-D vectors, 5-second epochs
- **Labels**: Stored in SQLite, indexed with bge-small-en-v1.5 embeddings
- **All data is local** — nothing is sent to external servers

---

## CLI Task Recipes

Command-by-command usage moved out of the skill body. The `## N.` numbering is
carried over verbatim from the original skill body sections.

### Status: Key Fields in the Response


The `scores` object contains all live metrics (0–1 scale unless noted):

```jsonc
{
  "scores": {
    "focus": 0.70,           // β / (α + θ) — sustained attention
    "relaxation": 0.40,      // α / (β + θ) — calm wakefulness
    "engagement": 0.60,      // active mental investment
    "meditation": 0.52,      // alpha + stillness + HRV coherence
    "mood": 0.55,            // composite from FAA, TAR, BAR
    "cognitive_load": 0.33,  // frontal θ / temporal α · f(FAA, TBR)
    "drowsiness": 0.10,      // TAR + TBR + falling spectral centroid
    "hr": 68.2,              // heart rate in bpm (from PPG)
    "snr": 14.3,             // signal-to-noise ratio in dB
    "stillness": 0.88,       // 0–1; 1 = perfectly still
    "faa": 0.042,            // Frontal Alpha Asymmetry (+ = approach)
    "tar": 0.56,             // Theta/Alpha Ratio
    "bar": 0.53,             // Beta/Alpha Ratio
    "tbr": 1.06,             // Theta/Beta Ratio (ADHD proxy)
    "apf": 10.1,             // Alpha Peak Frequency in Hz
    "coherence": 0.614,      // inter-hemispheric coherence
    "bands": {
      "rel_delta": 0.28, "rel_theta": 0.18,
      "rel_alpha": 0.32, "rel_beta": 0.17, "rel_gamma": 0.05
    }
  }
}
```

Also includes: `device` (state, battery, firmware), `signal_quality` (per-electrode 0–1),
`session` (duration, epochs), `embeddings`, `labels`, `sleep` summary, and `history`.

## 2. Session Analysis

### Single Session Breakdown
```bash
npx neuroskill session --json         # most recent session
npx neuroskill session 1 --json       # previous session
npx neuroskill session 0 --json | jq '{focus: .metrics.focus, trend: .trends.focus}'
```

Returns full metrics with **first-half vs second-half trends** (`"up"`, `"down"`, `"flat"`).
Use this to describe how a session evolved:

> "Your focus started at 0.64 and climbed to 0.76 by the end — a clear upward trend.
> Cognitive load dropped from 0.38 to 0.28, suggesting the task became more automatic
> as you settled in."

### List All Sessions
```bash
npx neuroskill sessions --json
npx neuroskill sessions --trends      # show per-session metric trends
```

---

## 3. Historical Search

### Neural Similarity Search
```bash
npx neuroskill search --json                    # auto: last session, k=5
npx neuroskill search --k 10 --json             # 10 nearest neighbors
npx neuroskill search --start <UTC> --end <UTC> --json
```

Finds moments in history that are neurally similar using HNSW approximate
nearest-neighbor search over 128-D ZUNA embeddings. Returns distance statistics,
temporal distribution (hour of day), and top matching days.

Use this when the user asks:
- "When was I last in a state like this?"
- "Find my best focus sessions"
- "When do I usually crash in the afternoon?"

### Semantic Label Search
```bash
npx neuroskill search-labels "deep focus" --k 10 --json
npx neuroskill search-labels "stress" --json | jq '[.results[].EXG_metrics.tbr]'
```

Searches label text using vector embeddings (Xenova/bge-small-en-v1.5). Returns
matching labels with their associated EXG metrics at the time of labeling.

### Cross-Modal Graph Search
```bash
npx neuroskill interactive "deep focus" --json
npx neuroskill interactive "deep focus" --dot | dot -Tsvg > graph.svg
```

4-layer graph: query → text labels → EXG points → nearby labels. Use `--k-text`,
`--k-EXG`, `--reach <minutes>` to tune.

---

## 4. Session Comparison
```bash
npx neuroskill compare --json                   # auto: last 2 sessions
npx neuroskill compare --a-start <UTC> --a-end <UTC> --b-start <UTC> --b-end <UTC> --json
```

Returns metric deltas with absolute change, percentage change, and direction for
~50 metrics. Also includes `insights.improved[]` and `insights.declined[]` arrays,
sleep staging for both sessions, and a UMAP job ID.

Interpret comparisons with context — mention trends, not just deltas:
> "Yesterday you had two strong focus blocks (10am and 2pm). Today you've had one
> starting around 11am that's still going. Your overall engagement is higher today
> but there have been more stress spikes — your stress index jumped 15% and
> FAA dipped negative more often."

```bash
# Sort metrics by improvement percentage
npx neuroskill compare --json | jq '.insights.deltas | to_entries | sort_by(.value.pct) | reverse'
```

---

## 5. Sleep Data
```bash
npx neuroskill sleep --json                     # last 24 hours
npx neuroskill sleep 0 --json                   # most recent sleep session
npx neuroskill sleep --start <UTC> --end <UTC> --json
```

Returns epoch-by-epoch sleep staging (5-second windows) with analysis:
- **Stage codes**: 0=Wake, 1=N1, 2=N2, 3=N3 (deep), 4=REM
- **Analysis**: efficiency_pct, onset_latency_min, rem_latency_min, bout counts
- **Healthy targets**: N3 15–25%, REM 20–25%, efficiency >85%, onset <20 min

```bash
npx neuroskill sleep --json | jq '.summary | {n3: .n3_epochs, rem: .rem_epochs}'
npx neuroskill sleep --json | jq '.analysis.efficiency_pct'
```

Use this when the user mentions sleep, tiredness, or recovery.

---

## 6. Labeling Moments
```bash
npx neuroskill label "breakthrough"
npx neuroskill label "studying algorithms"
npx neuroskill label "post-meditation"
npx neuroskill label --json "focus block start"   # returns label_id
```

Auto-label moments when:
- User reports a breakthrough or insight
- User starts a new task type (e.g., "switching to code review")
- User completes a significant protocol
- User asks you to mark the current moment
- A notable state transition occurs (entering/leaving flow)

Labels are stored in a database and indexed for later retrieval via `search-labels`
and `interactive` commands.

---

## 7. Real-Time Streaming
```bash
npx neuroskill listen --seconds 30 --json
npx neuroskill listen --seconds 5 --json | jq '[.[] | select(.event == "scores")]'
```

Streams live WebSocket events (EXG, PPG, IMU, scores, labels) for the specified
duration. Requires WebSocket connection (not available with `--http`).

Use this for continuous monitoring scenarios or to observe metric changes in real-time
during a protocol.

---

## 8. UMAP Visualization
```bash
npx neuroskill umap --json                      # auto: last 2 sessions
npx neuroskill umap --a-start <UTC> --a-end <UTC> --b-start <UTC> --b-end <UTC> --json
```

GPU-accelerated 3D UMAP projection of ZUNA embeddings. The `separation_score`
indicates how neurally distinct two sessions are:
- **> 1.5** → Sessions are neurally distinct (different brain states)
- **< 0.5** → Similar brain states across both sessions

---

## 11. Additional Tools

### Focus Timer
```bash
npx neuroskill timer --json
```
Launches the Focus Timer window with Pomodoro (25/5), Deep Work (50/10), or
Short Focus (15/5) presets.

### Calibration
```bash
npx neuroskill calibrate
npx neuroskill calibrate --profile "Eyes Open"
```
Opens the calibration window. Useful when signal quality is poor or the user
wants to establish a personalized baseline.

### OS Notifications
```bash
npx neuroskill notify "Break Time" "Your focus has been declining for 20 minutes"
```

### Raw JSON Passthrough
```bash
npx neuroskill raw '{"command":"status"}' --json
```
For any server command not yet mapped to a CLI subcommand.

---

## Example Interactions

**"How am I doing right now?"**
```bash
npx neuroskill status --json
```
→ Interpret scores naturally, mentioning focus, relaxation, mood, and any notable
  ratios (FAA, TBR). Suggest an action only if metrics indicate a need.

**"I can't concentrate"**
```bash
npx neuroskill status --json
```
→ Check if metrics confirm it (high theta, low beta, rising TBR, high drowsiness).
→ If confirmed, suggest an appropriate protocol from `references/protocols.md`.
→ If metrics look fine, the issue may be motivational rather than neurological.

**"Compare my focus today vs yesterday"**
```bash
npx neuroskill compare --json
```
→ Interpret trends, not just numbers. Mention what improved, what declined, and
  possible causes.

**"When was I last in a flow state?"**
```bash
npx neuroskill search-labels "flow" --json
npx neuroskill search --json
```
→ Report timestamps, associated metrics, and what the user was doing (from labels).

**"How did I sleep?"**
```bash
npx neuroskill sleep --json
```
→ Report sleep architecture (N3%, REM%, efficiency), compare to healthy targets,
  and note any issues (high wake epochs, low REM).

**"Mark this moment — I just had a breakthrough"**
```bash
npx neuroskill label "breakthrough"
```
→ Confirm label saved. Optionally note the current metrics to remember the state.
