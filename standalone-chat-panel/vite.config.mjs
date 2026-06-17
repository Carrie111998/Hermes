import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(fileURLToPath(import.meta.url));
const DEFAULT_DASHBOARD_URL = "http://127.0.0.1:9119";

function dashboardUrl() {
  const raw = process.env.HERMES_DASHBOARD_URL || DEFAULT_DASHBOARD_URL;
  return raw.replace(/\/+$/, "");
}

async function fetchDashboardToken(url) {
  const response = await fetch(`${url}/chat`);
  if (!response.ok) {
    throw new Error(`Hermes dashboard ${url}/chat returned HTTP ${response.status}`);
  }

  const html = await response.text();
  const match = html.match(/window\.__HERMES_SESSION_TOKEN__="([^"]+)"/);
  if (!match) {
    throw new Error(
      "Hermes session token was not found. Start the dashboard on loopback with embedded chat enabled.",
    );
  }
  return match[1];
}

export default async function config({ command }) {
  const target = dashboardUrl();
  const token = command === "serve" ? await fetchDashboardToken(target) : "";

  return {
    root,
    build: {
      outDir: "dist",
      emptyOutDir: true,
    },
    server: {
      host: "127.0.0.1",
      port: Number(process.env.HERMES_PANEL_PORT || "9131"),
      strictPort: false,
      proxy:
        command === "serve"
          ? {
              "/hermes/ws": {
                target,
                ws: true,
                changeOrigin: true,
                rewrite: () => `/api/ws?token=${encodeURIComponent(token)}`,
              },
              "/hermes/upload": {
                target,
                changeOrigin: true,
                headers: {
                  "X-Hermes-Session-Token": token,
                },
                rewrite: () => "/api/chat/uploads",
              },
              "/hermes/status": {
                target,
                changeOrigin: true,
                rewrite: () => "/api/status",
              },
            }
          : undefined,
    },
  };
}
