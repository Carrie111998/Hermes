import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    // Runs once, before any test file is loaded, so lockfile drift is reported
    // as drift instead of as a wall of missing-export failures. See
    // vitest.globalSetup.mjs.
    globalSetup: ["./vitest.globalSetup.mjs"],
    environment: "node",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
