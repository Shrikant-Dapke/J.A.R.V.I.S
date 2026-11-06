// Launches the Electron shell against the Vite development server.
//
// `npm run dev` is intentionally self-contained: it reuses an already-running
// Vite server on the documented loopback origin, or starts one and waits for
// its HTTP readiness before launching Electron, so a cold renderer server
// cannot become an invisible load failure.

import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const REQUIRE = createRequire(import.meta.url);
const electronBinary = REQUIRE("electron");
const viteEntry = fileURLToPath(new URL("../node_modules/vite/bin/vite.js", import.meta.url));
const DEV_SERVER_URL = "http://127.0.0.1:5173/";
const STARTUP_TIMEOUT_MS = 30_000;

let viteChild = null;
let electronChild = null;
let ownsVite = false;
let shuttingDown = false;
let viteExit = null;
let viteError = null;

function describeError(error) {
  return error instanceof Error ? error.message : String(error);
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function checkViteReady() {
  try {
    const response = await fetch(DEV_SERVER_URL, {
      signal: AbortSignal.timeout(1_000),
    });
    await response.body?.cancel();
    return response.ok;
  } catch {
    return false;
  }
}

function startVite() {
  ownsVite = true;
  console.log(`[dev] Vite is not ready at ${DEV_SERVER_URL}; starting it.`);
  viteChild = spawn(process.execPath, [viteEntry], {
    cwd: process.cwd(),
    stdio: "inherit",
    env: { ...process.env, NODE_ENV: "development" },
    windowsHide: true,
  });
  viteChild.once("error", (error) => {
    viteError = error;
    console.error(`[dev] Vite failed to start: ${describeError(error)}`);
  });
  viteChild.once("exit", (code, signal) => {
    viteExit = { code, signal };
    if (!shuttingDown && electronChild === null) {
      console.error(
        `[dev] Vite exited before Electron started (code=${String(code)}, signal=${String(signal)}).`,
      );
    }
  });
}

async function waitForVite() {
  const deadline = Date.now() + STARTUP_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (viteError) {
      throw new Error(`Vite failed to start: ${describeError(viteError)}`);
    }
    if (viteExit) {
      throw new Error(
        `Vite exited before becoming ready (code=${String(viteExit.code)}, signal=${String(viteExit.signal)}).`,
      );
    }
    if (await checkViteReady()) return;
    await sleep(150);
  }
  throw new Error(`Vite did not become ready at ${DEV_SERVER_URL} within ${STARTUP_TIMEOUT_MS} ms.`);
}

function stopOwnedVite() {
  if (!ownsVite || !viteChild || viteChild.killed) return;
  viteChild.kill();
}

function finish(code) {
  if (shuttingDown) return;
  shuttingDown = true;
  stopOwnedVite();
  process.exit(code);
}

function handleSignal(signal) {
  if (shuttingDown) return;
  shuttingDown = true;
  console.log(`[dev] ${signal} received; stopping Electron development session.`);
  electronChild?.kill();
  stopOwnedVite();
  process.exit(0);
}

process.once("SIGINT", () => handleSignal("SIGINT"));
process.once("SIGTERM", () => handleSignal("SIGTERM"));

async function launch() {
  if (await checkViteReady()) {
    console.log(`[dev] using existing Vite server at ${DEV_SERVER_URL}`);
  } else {
    startVite();
    await waitForVite();
    console.log(`[dev] Vite is ready at ${DEV_SERVER_URL}`);
  }

  console.log("[dev] launching Electron with the compiled main/preload entrypoints.");
  electronChild = spawn(electronBinary, [process.cwd()], {
    cwd: process.cwd(),
    stdio: "inherit",
    env: {
      ...process.env,
      NODE_ENV: "development",
      VITE_DEV_SERVER_URL: DEV_SERVER_URL.slice(0, -1),
      ELECTRON_ENABLE_LOGGING: process.env.ELECTRON_ENABLE_LOGGING ?? "1",
    },
    // `windowsHide` also sets the Windows process startup show state. Electron
    // creates the BrowserWindow, but Windows keeps it invisible when this is
    // true even after BrowserWindow.show(). Keep the GUI process visible;
    // `windowsHide: true` remains appropriate for the Vite helper above.
    windowsHide: false,
  });

  electronChild.once("error", (error) => {
    console.error(`[dev] Electron failed to start: ${describeError(error)}`);
    finish(1);
  });
  electronChild.once("exit", (code, signal) => {
    if (shuttingDown) return;
    const exitCode = code ?? (signal ? 1 : 0);
    console.log(`[dev] Electron exited (code=${String(code)}, signal=${String(signal)}).`);
    finish(exitCode);
  });
}

void launch().catch((error) => {
  console.error(`[dev] startup failed: ${describeError(error)}`);
  shuttingDown = true;
  stopOwnedVite();
  process.exitCode = 1;
});
