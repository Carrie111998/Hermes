# Hermes Plugin: Tool Telemetry

> **By Team Maelstrom** — Named for the Moskstraumen, the Lofoten maelstrom that gave the world the word "maelstrom." Just as that tidal current system creates observable vortex patterns from invisible forces beneath the surface, this plugin makes the invisible patterns of tool usage visible and analyzable.

## What It Does

**hermes-plugin-tool-telemetry** is a passive observability plugin for Hermes Agent that collects structured telemetry on every tool call without affecting agent behavior. It hooks into the `pre_tool_call` and `post_tool_call` lifecycle hooks to record:

- Tool name, toolset, arguments (redacted of secrets)
- Call duration, success/failure status
- Error messages on failure
- Session ID and profile name
- Timestamp

Data is stored in a lightweight SQLite database at `~/.hermes/telemetry.db` (profile-aware). The plugin exposes three tools for the agent to query its own telemetry:

- `telemetry_summary` — aggregate statistics over a time window
- `telemetry_failures` — recent failure patterns with error clustering
- `telemetry_export` — export telemetry data as JSON for external analysis

## Why We Built It

Hermes agents make hundreds of tool calls per session, but there is no built-in way to observe patterns in those calls over time. This creates several blind spots:

1. **Silent degradation** — A tool that starts failing intermittently goes unnoticed until it causes a visible task failure.
2. **Performance regression** — Tool call latency can increase after updates with no signal until the user notices slowness.
3. **Tool underuse** — Capabilities that exist but are rarely or never invoked represent wasted potential.
4. **Error pattern blindness** — The same error occurring across sessions is invisible without aggregation.

The Moskstraumen is invisible to casual observation — you see flat water until the tidal forces align. Tool call telemetry is the same: the patterns are there, but you need instrumentation to see them.

## Installation

```bash
# Clone or copy to plugins directory
cp -r hermes-plugin-tool-telemetry ~/.hermes/plugins/tool-telemetry

# Enable the plugin
hermes config set plugins.enabled '["tool-telemetry"]'

# Verify
hermes plugins list
```

## Configuration

```yaml
# config.yaml
plugins:
  entries:
    tool-telemetry:
      # Maximum argument string length to store (prevents bloating)
      max_arg_length: 500
      # Redact strings matching these patterns
      redact_patterns:
        - "ghp_\\w+"        # GitHub tokens
        - "sk-\\w+"         # OpenAI keys
        - "AKIA\\w+"        # AWS keys
      # Retention period in days (0 = forever)
      retention_days: 30
```

## Privacy

- No tool arguments are stored verbatim — they are truncated and redacted.
- No message content, user data, or file contents are recorded.
- Only tool name, toolset, redacted args, duration, status, and timestamps.
- Data stays local in `~/.hermes/telemetry.db` — never transmitted.

## Lofoten Connection

The Moskstraumen (also called the Lofoten Maelstrom) is a system of tidal eddies between Moskenesøya and the island of Mosken. It is one of the strongest tidal currents in the world, with speeds measured at up to 5 m/s (Norwegian Hydrographic Service, 1986). Unlike most major maelstroms, it occurs in open sea rather than in a strait.

The word "maelstrom" itself derives from Moskstraumen — from the Nordic *mal* (grinding) and *strom* (stream), describing the grinding, swirling current. It appeared in Jules Verne's *Twenty Thousand Leagues Under the Sea* and Edgar Allan Poe's *A Descent into the Maelström*.

The connection: beneath the surface of every Hermes agent session, thousands of tool calls create currents and patterns — vortices of activity that are invisible without instrumentation. This plugin is the hydrographic survey that makes those patterns visible.

## License

MIT