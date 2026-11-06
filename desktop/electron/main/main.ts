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

import {
  app,
  BrowserWindow,
  Menu,
  dialog,
  screen,
  type WebContents,
} from "electron";
import { join } from "node:path";
import { existsSync } from "node:fs";
import { loadDesktopConfig, type DesktopConfig } from "./config";
import { getSessionId, maskSessionId } from "./session";
import { HttpBackendApiClient } from "./api/client";
import { createJarvisIpcRegistry, registerIpcHandlers } from "./ipc/handlers";
import { createModelManager } from "./voice/model-manager";
import { createVoiceEngine, type VoiceEngine } from "./voice/voice-engine";
import { createUtilityProcessTransportFactory } from "./voice/voice-transport";
import {
  createWebPreferences,
  createWindowOptions,
  shouldAllowNavigation,
  denyOpenWindow,
  isMicPermissionAllowed,
} from "./app-security";
import type { BackendStatus } from "../../shared/ipc-contract";
import { VOICE_EVENTS_CHANNEL } from "../../shared/ipc-contract";

const RENDERER_INDEX = join(__dirname, "..", "..", "renderer", "index.html");
const PRELOAD_SCRIPT = join(__dirname, "..", "preload", "preload.js");
const DEV_SERVER_URL_ALLOWED = "http://127.0.0.1:5173";
const WINDOW_DIAGNOSTICS_ENABLED =
  process.env.NODE_ENV === "development" ||
  process.env.JARVIS_WINDOW_DIAGNOSTICS === "1";

type WindowLifecycleEvent =
  | "created"
  | "ready-to-show:before-show"
  | "ready-to-show:after-show"
  | "did-finish-load"
  | "show"
  | "hide"
  | "minimize"
  | "restore"
  | "focus"
  | "blur"
  | "close"
  | "closed"
  | "moved"
  | "resized"
  | "unresponsive"
  | "responsive"
  | "renderer-failure";

function logWindowState(
  window: BrowserWindow,
  event: WindowLifecycleEvent,
  details: Record<string, unknown> = {},
): void {
  if (!WINDOW_DIAGNOSTICS_ENABLED) return;

  const destroyed = window.isDestroyed();
  const state: Record<string, unknown> = {
    event,
    windowId: destroyed ? null : window.id,
    isDestroyed: destroyed,
    isVisible: false,
    isMinimized: false,
    bounds: null,
    ...details,
  };
  if (!destroyed) {
    state.isVisible = window.isVisible();
    state.isMinimized = window.isMinimized();
    state.bounds = window.getBounds();
  }
  console.log(`[window] ${JSON.stringify(state)}`);
}

function logStartupState(): void {
  if (!WINDOW_DIAGNOSTICS_ENABLED) return;
  console.log(
    `[window] ${JSON.stringify({
      event: "startup",
      argv: process.argv,
      cwd: process.cwd(),
      execPath: process.execPath,
      displays: screen.getAllDisplays().map((display) => ({
        id: display.id,
        bounds: display.bounds,
        workArea: display.workArea,
        scaleFactor: display.scaleFactor,
        isPrimary: display.id === screen.getPrimaryDisplay().id,
      })),
    })}`,
  );
}

/**
 * Voice runtime (Phase 6.4-b). Constructed AFTER `app` is ready (the
 * models root lives under the user-data profile). STRICTLY lazy: building
 * this creates the model manager and the engine facade only — no worker
 * process is forked and no model is loaded or DOWNLOADED until a user
 * action demands it (models appear only via `voice-prepare-models`).
 */
function createVoiceRuntime(): VoiceEngine {
  const baseDir = join(app.getPath("userData"), "models");
  const models = createModelManager({ baseDir });
  const engine = createVoiceEngine({
    models,
    createTransport: createUtilityProcessTransportFactory(),
    trace: (message) => console.log(message),
  });
  // The single main → renderer voice event channel. Payloads are already
  // validated by the engine (validateVoiceEventPayload) before reaching
  // here; the renderer receives bounded plain data only.
  engine.subscribe((event) => {
    for (const window of BrowserWindow.getAllWindows()) {
      if (!window.isDestroyed()) window.webContents.send(VOICE_EVENTS_CHANNEL, event);
    }
  });
  return engine;
}

type AppRuntime = {
  config: DesktopConfig;
  client: HttpBackendApiClient;
  navigationTrust: { devServerUrl: string | null; isDistHosting: boolean };
  voice: VoiceEngine;
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

  return { config, client, navigationTrust: { devServerUrl, isDistHosting: devServerUrl === null }, voice: createVoiceRuntime() };
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
  logWindowState(window, "created");

  window.on("show", () => logWindowState(window, "show"));
  window.on("hide", () => logWindowState(window, "hide"));
  window.on("minimize", () => logWindowState(window, "minimize"));
  window.on("restore", () => logWindowState(window, "restore"));
  window.on("focus", () => logWindowState(window, "focus"));
  window.on("blur", () => logWindowState(window, "blur"));
  window.on("close", () => logWindowState(window, "close"));
  window.on("closed", () => logWindowState(window, "closed"));
  window.on("moved", () => logWindowState(window, "moved"));
  window.on("resized", () => logWindowState(window, "resized"));
  window.on("unresponsive", () => logWindowState(window, "unresponsive"));
  window.on("responsive", () => logWindowState(window, "responsive"));

  let rendererFailureReported = false;
  const reportRendererFailure = (message: string): void => {
    if (rendererFailureReported) return;
    rendererFailureReported = true;
    console.error(`[window] renderer failed to load: ${message}`);
    if (!window.isDestroyed()) {
      window.show();
      window.focus();
      logWindowState(window, "renderer-failure", { message });
    }
  };

  // Show only after the renderer is ready; load failures are logged and
  // surfaced instead of leaving an invisible process behind.
  window.once("ready-to-show", () => {
    console.log(`[window] ready-to-show: ${window.webContents.getURL()}`);
    logWindowState(window, "ready-to-show:before-show");
    window.show();
    window.focus();
    logWindowState(window, "ready-to-show:after-show");
  });
  window.webContents.once("did-finish-load", () => {
    console.log(`[window] renderer loaded: ${window.webContents.getURL()}`);
    logWindowState(window, "did-finish-load");
    if (runtime?.navigationTrust.devServerUrl) {
      void window.webContents
        .executeJavaScript(
          `Promise.resolve(
            window.jarvis && typeof window.jarvis.backendStatus === "function"
              ? window.jarvis.backendStatus({})
              : null
          ).then((result) => Boolean(result && typeof result.ok === "boolean"))`,
          true,
        )
        .then((bridgeWorks) => {
          console.log(`[window] preload bridge IPC: ${bridgeWorks ? "available" : "missing"}`);
        })
        .catch((error) => {
          console.error(
            `[window] preload bridge IPC check failed: ${error instanceof Error ? error.message : String(error)}`,
          );
        });
    }
  });
  window.webContents.on(
    "did-fail-load",
    (_event, errorCode, errorDescription, validatedURL, isMainFrame) => {
      if (isMainFrame) {
        reportRendererFailure(
          `${errorDescription} (${errorCode}) at ${validatedURL}`,
        );
      }
    },
  );
  window.webContents.on("render-process-gone", (_event, details) => {
    reportRendererFailure(`render process gone: ${details.reason} (${details.exitCode})`);
  });

  const loadPromise = runtime && runtime.navigationTrust.devServerUrl
    ? window.loadURL(runtime.navigationTrust.devServerUrl)
    : window.loadFile(RENDERER_INDEX);
  void loadPromise.catch((error) => {
    reportRendererFailure(error instanceof Error ? error.message : String(error));
  });

  if (runtime && runtime.config.devtools && !app.isPackaged) {
    window.webContents.openDevTools({ mode: "detach" });
  }

  return window;
}

function getWebContentsUrl(contents: WebContents | null): string | null {
  if (!contents) return null;
  try {
    return contents.getURL() || null;
  } catch {
    return null;
  }
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
    contents.session.setPermissionRequestHandler((requestingContents, permission, callback, details) => {
      const trust = runtime?.navigationTrust ?? { devServerUrl: null, isDistHosting: true };
      const frameUrl = getWebContentsUrl(requestingContents);
      const requestDetails = details as {
        requestingUrl?: string;
        isMainFrame?: boolean;
        mediaTypes?: readonly string[];
      };
      callback(
        isMicPermissionAllowed(
          permission,
          requestDetails.requestingUrl ?? frameUrl,
          trust,
          {
            isMainFrame: requestDetails.isMainFrame,
            frameUrl,
            mediaTypes: requestDetails.mediaTypes,
          },
        ),
      );
    });
    contents.session.setPermissionCheckHandler(
      (requestingContents, permission, requestingOrigin, details) => {
        const trust = runtime?.navigationTrust ?? { devServerUrl: null, isDistHosting: true };
        const frameUrl = getWebContentsUrl(requestingContents);
        return isMicPermissionAllowed(
          permission,
          details.requestingUrl ?? frameUrl ?? requestingOrigin,
          trust,
          {
            isMainFrame: details.isMainFrame,
            frameUrl,
            mediaType: details.mediaType,
          },
        );
      },
    );
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
    logStartupState();
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
      voice: runtime.voice,
    }));

    createMainWindow();

    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createMainWindow();
    });
  });

  app.on("window-all-closed", () => {
    runtime?.voice.dispose();
    app.quit();
  });
}
