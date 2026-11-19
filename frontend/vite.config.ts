/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev only: `/api/*` -> local API, with the key injected HERE (server side of the Vite process).
// ADP_API_KEY is read from the shell environment (not VITE_-prefixed, so never bundled).
const apiKey = process.env["ADP_API_KEY"] ?? "";
const target = process.env["ADP_API_URL"] ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target,
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
        headers: apiKey ? { "X-API-Key": apiKey } : {},
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
