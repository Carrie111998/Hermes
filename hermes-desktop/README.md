# Containerized noVNC Desktop

This optional stack gives Hermes's built-in `computer_use` toolset a
persistent Linux/Xfce desktop with Chrome and browser-based noVNC takeover.
It does not add another computer-use tool or bypass Hermes approval checks.

Hermes talks MCP over stdio to the official `cua-driver` inside the container:

```text
Hermes computer_use approval gate
  -> hermes-desktop/cua-driver-docker
  -> docker compose exec -T desktop cua-driver mcp
```

## Setup

Requirements: Docker with Compose v2 and an installed `hermes` CLI.

```bash
cd hermes-desktop
./setup.sh
```

The script:

1. generates a VNC password in the ignored `hermes-desktop/.env`;
2. builds and starts the persistent desktop;
3. saves the non-secret wrapper path as
   `computer_use.driver_command` in the active Hermes `config.yaml`;
4. verifies noVNC, X11 control, persistence, and `cua-driver`.

Then run:

```bash
hermes -t computer_use chat
```

Open <http://localhost:6080/vnc.html> for live viewing or manual takeover.
Both noVNC (`6080`) and raw VNC (`5901`) bind to `127.0.0.1` by default.

## Safety

- Mutating actions still use the approval and hard-block rules in
  `tools/computer_use/tool.py`.
- No SSH service or direct command-execution backend is exposed.
- VNC and noVNC are loopback-only. Use an authenticated VPN or SSH tunnel for
  remote access; do not change the bindings to `0.0.0.0` on a public host.
- The `desktop_home` Docker volume retains cookies and login state. Treat it
  like a normal signed-in browser profile.
- Use noVNC yourself for passwords, MFA, CAPTCHAs, payments, and other
  sensitive steps.

## Commands

```bash
./verify.sh
docker compose logs --tail 100 desktop
docker compose restart desktop
docker compose down
```

`docker compose down` preserves the desktop volume. Adding `-v` deletes the
saved browser profile.

## Custom ports or resolution

Keep secrets in `.env`. Put non-secret Compose overrides in an untracked
`compose.override.yml`, for example:

```yaml
services:
  desktop:
    ports:
      - "127.0.0.1:5902:5901"
      - "127.0.0.1:6081:6080"
    environment:
      VNC_RESOLUTION: 1440x900
```

Run it explicitly:

```bash
docker compose -f docker-compose.yml -f compose.override.yml up -d
```

If the noVNC port changes, use that new URL for manual takeover. The wrapper
and `computer_use` MCP connection do not depend on the published ports.

## Troubleshooting

Run `./verify.sh` first. If the wrapper is no longer at the configured path
(for example, the checkout moved), rerun `./setup.sh` or set it directly:

```bash
hermes config set computer_use.driver_command \
  /absolute/path/to/hermes-agent/hermes-desktop/cua-driver-docker
```

To return to a host-installed driver:

```bash
hermes config unset computer_use.driver_command
hermes computer-use install
```
