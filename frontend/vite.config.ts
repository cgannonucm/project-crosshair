import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Builds into the Python package so the server ships as a single artifact.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../server/crosshair/static",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/ws": { target: "ws://localhost:8137", ws: true },
      "/data": "http://localhost:8137",
      "/api": "http://localhost:8137",
    },
  },
});
