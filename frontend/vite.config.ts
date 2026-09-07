import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Environment-agnostic: the API base URL is injected at runtime/build via VITE_API_BASE_URL.
// No account IDs, regions, secrets, or internal endpoints are hardcoded here.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
