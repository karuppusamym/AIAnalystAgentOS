/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const apiTarget = process.env.ANALYSTOS_API_URL ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // SSE-friendly: http-proxy streams responses; no buffering is applied by Vite.
      "/api": { target: apiTarget, changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    chunkSizeWarningLimit: 1200,
  },
  test: {
    environment: "jsdom",
    globals: false,
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["src/test/setup.ts"],
    // Journey tests render whole screens and wait up to 4 s per step (setup.ts); under a loaded full run the 5 s
    // default cut multi-step journeys short. A longer ceiling changes no assertion.
    testTimeout: 15000,
  },
});
