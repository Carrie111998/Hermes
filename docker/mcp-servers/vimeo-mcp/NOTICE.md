# Vendored: @mcp/vimeo (Vimeo MCP server)

This directory is a **vendored copy** of the Vimeo MCP server, baked into the
Hermes Docker image so the `web-dev` profile's `vimeo` MCP server is fully
self-contained (no clone or network fetch at boot, survives a volume reset).

- **Upstream:** https://github.com/galacoder/vimeo-mcp (`@mcp/vimeo` v0.1.0)
- **License:** MIT (see `LICENSE`)
- **Vendored:** 2026-07-06, from `main`.

## Local modifications

- `src/vimeo-client.ts` — the `new Vimeo(null, null, accessToken)` call (PAT
  mode) is cast to satisfy TypeScript strict typing; the runtime value is
  unchanged. Upstream emits JS but `tsc` exits non-zero on this line, which
  would fail the Docker build.
- `package.json` — removed the `prepare` (auto-build-on-install) script; the
  image builds explicitly via `npm run build` in the Dockerfile.

## Build

The Dockerfile builds this in place (`npm ci && npm run build && npm prune
--omit=dev`) at `/opt/hermes/docker/mcp-servers/vimeo-mcp/dist/index.js`. The
`web-dev` profile launches it via
`node /opt/hermes/docker/mcp-servers/vimeo-mcp/dist/index.js` and authenticates
with `VIMEO_ACCESS_TOKEN` (see `docker/profiles/web-dev/config.yaml`).
