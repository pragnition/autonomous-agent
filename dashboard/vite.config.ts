import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vitest/config"

// In development the API is core's dashboard port, usually through `ssh -L 18086:127.0.0.1:18086 <host>`.
// Tailscale Serve adds the identity header in production; here it comes from DASHBOARD_DEV_LOGIN.
const target = process.env.DASHBOARD_API ?? "http://127.0.0.1:18086"
const login = process.env.DASHBOARD_DEV_LOGIN ?? ""

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(import.meta.dirname, "src") } },
  server: {
    proxy: {
      "/api": {
        target,
        changeOrigin: false, // keep the browser's Host: core checks Origin against it
        headers: login ? { "Tailscale-User-Login": login } : {},
      },
    },
  },
  // One bundle (~240 kB gzipped, most of it the chart library the Overview needs anyway) for a tailnet-only page
  build: { chunkSizeWarningLimit: 1000 },
  test: { environment: "node", include: ["src/**/*.test.{ts,tsx}"] },
})
