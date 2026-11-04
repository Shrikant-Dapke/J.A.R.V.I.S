import { defineConfig, type PluginOption } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";

// Dev HMR needs a WebSocket connection to the dev server. The shipped
// (production) CSP is the strict meta tag in index.html; this plugin only
// relaxes connect-src while the Vite dev server is serving index.html.
function devOnlyCspRelax(): PluginOption {
  return {
    name: "jarvis-dev-csp",
    apply: "serve",
    transformIndexHtml(html) {
      return html.replace(
        "connect-src 'self'",
        "connect-src 'self' ws://127.0.0.1:5173 http://127.0.0.1:5173",
      );
    },
  };
}

export default defineConfig({
  base: "./",
  plugins: [react(), devOnlyCspRelax()],
  resolve: {
    alias: {
      "@": resolve(__dirname, "src/renderer"),
    },
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
  },
  build: {
    outDir: "dist/renderer",
    emptyOutDir: true,
    sourcemap: false,
    target: "chrome128",
  },
});
