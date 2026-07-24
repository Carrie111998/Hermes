# Setup and Environment

## Setup (Automated)

Run the setup script to handle everything:

```bash
bash "${HERMES_HOME:-$HOME/.hermes}/skills/creative/touchdesigner-mcp/scripts/setup.sh"
```

The script will:
1. Check if TD is running
2. Download twozero.tox if not already cached
3. Add `twozero_td` MCP server to Hermes config (if missing)
4. Test the MCP connection on port 40404
5. Report what manual steps remain (drag .tox into TD, enable MCP toggle)

## Manual steps (one-time, cannot be automated)

1. **Drag `~/Downloads/twozero.tox` into the TD network editor** → click Install
2. **Enable MCP:** click twozero icon → Settings → mcp → "auto start MCP" → Yes
3. **Restart Hermes session** to pick up the new MCP server

After setup, verify:

```bash
nc -z 127.0.0.1 40404 && echo "twozero MCP: READY"
```

Hub health check: `GET http://localhost:40404/mcp` returns JSON with instance
PID, project name, and TD version.

## Environment Notes

- **Non-Commercial TD** caps resolution at 1280×1280. Use `outputresolution = 'custom'` and set width/height explicitly.
- **Codecs:** `prores` (preferred on macOS) or `mjpa` as fallback. H.264/H.265/AV1 require a Commercial license.
- Always call `td_get_par_info` before setting params — names vary by TD version.
- twozero is a free plugin (no payment/license — confirmed April 2026) and is
  context-aware: it knows the selected OP and the currently open network.

## Security Notes

- MCP runs on localhost only (port 40404). No authentication — any local process can send commands.
- `td_execute_python` has unrestricted access to the TD Python environment and filesystem as the TD process user.
- `setup.sh` downloads twozero.tox from the official 404zero.com URL. Verify the download if concerned.
- The skill never sends data outside localhost. All MCP communication is local.
