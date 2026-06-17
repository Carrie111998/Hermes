import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { createPanelAuthStore } from "./panelAuth.mjs";
import { registerPanelBff } from "./panelBff.mjs";

const root = dirname(fileURLToPath(import.meta.url));
const DEFAULT_API_SERVER_URL = "http://127.0.0.1:9120";

function apiServerUrl() {
  const raw = process.env.HERMES_API_SERVER_URL || DEFAULT_API_SERVER_URL;
  return raw.replace(/\/+$/, "");
}

function apiServerKey() {
  return process.env.HERMES_API_SERVER_KEY || process.env.API_SERVER_KEY || "";
}

export default function config() {
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
    },
    plugins: [
      {
        name: "ultra-studio-panel-bff",
        configureServer(server) {
          const authStore = createPanelAuthStore();
          registerPanelBff(server, {
            apiServerUrl: apiServerUrl(),
            apiServerKey: apiServerKey(),
            authStore,
          });
        },
      },
    ],
  };
}
