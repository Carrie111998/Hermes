# Honcho Config Reference, Troubleshooting & CLI

## Config Reference

Config file: `$HERMES_HOME/honcho.json` (profile-local) or `~/.honcho/config.json` (global).

### Key settings

| Key | Default | Description |
|-----|---------|-------------|
| `apiKey` | -- | API key ([get one](https://app.honcho.dev)) |
| `baseUrl` | -- | Base URL for self-hosted Honcho |
| `peerName` | -- | User peer identity |
| `aiPeer` | host key | AI peer identity |
| `workspace` | host key | Shared workspace ID |
| `recallMode` | `hybrid` | `hybrid`, `context`, or `tools` |
| `observation` | all on | Per-peer `observeMe`/`observeOthers` booleans |
| `writeFrequency` | `async` | `async`, `turn`, `session`, or integer N |
| `sessionStrategy` | `per-directory` | `per-directory`, `per-repo`, `per-session`, `global` |
| `messageMaxChars` | `25000` | Max chars per message (chunked if exceeded) |

### Dialectic settings

| Key | Default | Description |
|-----|---------|-------------|
| `dialecticReasoningLevel` | `low` | `minimal`, `low`, `medium`, `high`, `max` |
| `dialecticDynamic` | `true` | Auto-bump reasoning by query complexity. `false` = fixed level |
| `dialecticDepth` | `1` | Number of dialectic rounds per query (1-3) |
| `dialecticDepthLevels` | -- | Optional array of per-round levels, e.g. `["low", "high"]` |
| `dialecticMaxInputChars` | `10000` | Max chars for dialectic query input |

### Context budget and injection

| Key | Default | Description |
|-----|---------|-------------|
| `contextTokens` | uncapped | Max tokens for the combined base context injection (summary + representation + card). Opt-in cap — omit to leave uncapped, set to an integer to bound injection size. |
| `injectionFrequency` | `every-turn` | `every-turn` or `first-turn` |
| `contextCadence` | `1` | Min turns between context API calls |
| `dialecticCadence` | `2` | Min turns between dialectic LLM calls (recommended 1–5) |

The `contextTokens` budget is enforced at injection time. If the session summary + representation + card exceed the budget, Honcho trims the summary first, then the representation, preserving the card. This prevents context blowup in long sessions.

### Memory-context sanitization

Honcho sanitizes the `memory-context` block before injection to prevent prompt injection and malformed content:

- Strips XML/HTML tags from user-authored conclusions
- Normalizes whitespace and control characters
- Truncates individual conclusions that exceed `messageMaxChars`
- Escapes delimiter sequences that could break the system prompt structure

This fix addresses edge cases where raw user conclusions containing markup or special characters could corrupt the injected context block.

## Troubleshooting

### "Honcho not configured"
Run `hermes honcho setup`. Ensure `memory.provider: honcho` is in `~/.hermes/config.yaml`.

### Memory not persisting across sessions
Check `hermes honcho status` -- verify `saveMessages: true` and `writeFrequency` isn't `session` (which only writes on exit).

### Profile not getting its own peer
Use `--clone` when creating: `hermes profile create <name> --clone`. For existing profiles: `hermes honcho sync`.

### Observation changes in dashboard not reflected
Observation config is synced from the server on each session init. Start a new session after changing settings in the Honcho UI.

### Messages truncated
Messages over `messageMaxChars` (default 25k) are automatically chunked with `[continued]` markers. If you're hitting this often, check if tool results or skill content is inflating message size.

### Context injection too large
If you see warnings about context budget exceeded, lower `contextTokens` or reduce `dialecticDepth`. The session summary is trimmed first when the budget is tight.

### Session summary missing
Session summary requires at least one prior turn in the current Honcho session. On cold start (new session, no history), the summary is omitted and Honcho uses the cold-start prompt strategy instead.

## CLI Commands

| Command | Description |
|---------|-------------|
| `hermes honcho setup` | Interactive setup wizard (cloud/local, identity, observation, recall, sessions) |
| `hermes honcho status` | Show resolved config, connection test, peer info for active profile |
| `hermes honcho enable` | Enable Honcho for the active profile (creates host block if needed) |
| `hermes honcho disable` | Disable Honcho for the active profile |
| `hermes honcho peer` | Show or update peer names (`--user <name>`, `--ai <name>`, `--reasoning <level>`) |
| `hermes honcho peers` | Show peer identities across all profiles |
| `hermes honcho mode` | Show or set recall mode (`hybrid`, `context`, `tools`) |
| `hermes honcho tokens` | Show or set token budgets (`--context <N>`, `--dialectic <N>`) |
| `hermes honcho sessions` | List known directory-to-session-name mappings |
| `hermes honcho map <name>` | Map current working directory to a Honcho session name |
| `hermes honcho identity` | Seed AI peer identity or show both peer representations |
| `hermes honcho sync` | Create host blocks for all Hermes profiles that don't have one yet |
| `hermes honcho migrate` | Step-by-step migration guide from OpenClaw native memory to Hermes + Honcho |
| `hermes memory setup` | Generic memory provider picker (selecting "honcho" runs the same wizard) |
| `hermes memory status` | Show active memory provider and config |
| `hermes memory off` | Disable external memory provider |
