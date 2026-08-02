import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8877",
    },
  },
  build: {
    chunkSizeWarningLimit: 900,
    // Content-hashed filenames: the URL changes whenever the bundle changes,
    // so a browser can NEVER serve a stale cached bundle (the #1 "looks the
    // same after refresh" bug). The no-store HTML always points at the current
    // hash; the boot guard self-heals any tab left on an old hash.
  },
});
