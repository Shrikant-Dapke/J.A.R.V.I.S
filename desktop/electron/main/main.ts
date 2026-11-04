/**
 * JARVIS desktop — Electron main process (Phase 6.1 entry point).
 *
 * Responsibilities:
 *  - load desktop config (backend origin, token, session) from env /
 *    backend/.env and fail closed on invalid values,
 *  - enforce process-level security invariants (app-security.ts),
 *  - register the narrow IPC bridge handlers,
 *  - keep ALL backend communication here (renderer holds no tokens and
 *    performs no network I/O — see shared/ipc-contract.ts).
 */

import { app, BrowserWindow, Menu, dialog } from "electron";
import { join } from "node:path";
import { existsSync } from "node:fs";
import { loadDesktopConfig, type DesktopConfig } from "./config";
import { getSessionId, maskSessionId } from "./session";
import { HttpBackendApiClient } from "./api/client";
import { createJarvisIpcRegistry, registerIpcHandlers } from "./ipc/handlers";
import {
  createWebPreferences,
  createWindowOptions,
  shouldAllowNavigation,
  denyOpenWindow,
} from "./app-security";
import type { BackendStatus } from "../../shared/ipc-contract";

const RENDERER_INDEX = join(__dirname, "..", "..", "renderer", "index.html");
const PRELOAD_SCRIPT = join(__dirname, "..", "preload", "preload.js");
const DEV_SERVER_URL_ALLOWED = "http://127.0.0.1:5173";

type AppRuntime = {
  config: DesktopConfig;
  client: HttpBackendApiClient;
  navigationTrust: { devServerUrl: string | null; isDistHosting: boolean };
};

let runtime: AppRuntime | null = null;

function buildRuntime(): AppRuntime {
  const config = loadDesktopConfig();
  const client = new HttpBackendApiClient({
    baseUrl: new URL(config.apiOrigin),
    token: config.token,
    sessionId: getSessionId(),
  });

  // The dev origin is trusted only in unpackaged development builds
  // running against the documented strict port; anything else is dist
  // hosting of the built renderer.
  const devServerUrl =
    !app.isPackaged &&
    process.env.NODE_ENV === "development" &&
    process.env.VITE_DEV_SERVER_URL === DEV_SERVER_URL_ALLOWED
      ? DEV_SERVER_URL_ALLOWED
      : null;

  if (!devServerUrl && !existsSync(RENDERER_INDEX)) {
    throw new Error(
      `Built renderer not found at ${RENDERER_INDEX}. Run 'npm run build:renderer' first.`,
    );
  }

  return { config, client, navigationTrust: { devServerUrl, isDistHosting: devServerUrl === null } };
}

function getBackendStatus(): BackendStatus {
  const current = runtime ?? null;
  if (!current) throw new Error("Runtime is not initialized");
  return {
    apiOrigin: current.config.apiOrigin,
    tokenConfigured: current.config.token !== null,
    tokenSource: current.config.tokenSource,
    sessionIdMasked: maskSessionId(getSessionId()),
    appVersion: app.getVersion(),
    runtime: {
      electron: process.versions.electron ?? "unknown",
      node: process.versions.node,
    },
  };
}

function createMainWindow(): BrowserWindow {
  const window = new BrowserWindow(
    createWindowOptions(createWebPreferences(PRELOAD_SCRIPT)),
  );

  // Never show a blank window on load failure; surface it honestly.
  window.once("ready-to-show", () => window.show());

  void (runtime && runtime.navigationTrust.devServerUrl
    ? window.loadURL(runtime.navigationTrust.devServerUrl)
    : window.loadFile(RENDERER_INDEX));

  if (runtime && runtime.config.devtools && !app.isPackaged) {
    window.webContents.openDevTools({ mode: "detach" });
  }

  return window;
}

/** Process-wide hardening for every webContents this app ever creates. */
function attachSecurityGuards(): void {
  app.on("web-contents-created", (_event, contents) => {
    contents.on("will-navigate", (navigationEvent, targetUrl) => {
      const trust = runtime?.navigationTrust ?? { devServerUrl: null, isDistHosting: true };
      if (!shouldAllowNavigation(targetUrl, trust)) {
        navigationEvent.preventDefault();
        // Log scheme/host only — never full URLs contents in audit notes.
        try {
          const target = new URL(targetUrl);
          console.error(
            `[security] blocked navigation to ${target.protocol}//${target.host}`,
          );
        } catch {
          console.error("[security] blocked navigation to unparsable URL");
        }
      }
    });
    contents.setWindowOpenHandler(() => denyOpenWindow());
    contents.session.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
    contents.session.setPermissionCheckHandler(() => false);
  });
}

const gotSingleInstanceLock = app.requestSingleInstanceLock();

if (!gotSingleInstanceLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    const [existing] = BrowserWindow.getAllWindows();
    existing?.focus();
  });

  process.on("uncaughtException", (error) => {
    console.error("[fatal] uncaught exception:", error.message);
  });
  process.on("unhandledRejection", (reason) => {
    console.error("[fatal] unhandled rejection:", String(reason));
  });

  void app.whenReady().then(() => {
    try {
      runtime = buildRuntime();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (!app.isPackaged) console.error("[config]", message);
      void dialog.showErrorBox("JARVIS — configuration error", message);
      app.exit(1);
      return;
    }

    Menu.setApplicationMenu(null);
    attachSecurityGuards();
    registerIpcHandlers(createJarvisIpcRegistry({
      client: runtime.client,
      getStatus: getBackendStatus,
      navigationTrust: runtime.navigationTrust,
    }));

    createMainWindow();

    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createMainWindow();
    });
  });

  app.on("window-all-closed", () => {
    app.quit();
  });
}
