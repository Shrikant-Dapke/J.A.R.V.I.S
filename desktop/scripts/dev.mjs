// Launches Electron in development mode against the Vite dev server.
// Renderer keeps serving from http://127.0.0.1:5173 (strict port) and the
// main process connects with the dev-only CSP relaxation from vite.config.ts.
//
// Usage: 1) npm run dev:renderer   2) npm run dev   (optionally JARVIS_DEVTOOLS=1)

import { spawn } from "node:child_process";
import { createRequire } from "node:module";

const REQUIRE = createRequire(import.meta.url);
// Under Node, `require("electron")` resolves to the packaged binary path.
const electronBinary = REQUIRE("electron");

const child = spawn(electronBinary, ["."], {
  stdio: "inherit",
  env: {
    ...process.env,
    NODE_ENV: "development",
    VITE_DEV_SERVER_URL: "http://127.0.0.1:5173",
    // DevTools remain opt-in: set JARVIS_DEVTOOLS=1 before starting.
  },
  windowsHide: true,
});

child.on("exit", (code, signal) => {
  process.exit(code ?? (signal ? 1 : 0));
});
