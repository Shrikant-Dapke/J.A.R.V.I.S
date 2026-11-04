/**
 * Electron security configuration (pure module; contract-tested).
 *
 * Invariants (Phase 6.1 acceptance criteria):
 *   - contextIsolation is ALWAYS enabled,
 *   - nodeIntegration is ALWAYS disabled,
 *   - the preload runs in the OS sandbox,
 *   - webSecurity remains on,
 *   - renderer navigation is locked to the app's own origin,
 *   - window opening and permissions are denied.
 *
 * main.ts consumes the helpers here; the tests in desktop/tests assert
 * the exact values so a future regression cannot silently weaken them.
 */

export type JarvisWebPreferences = {
  preload: string;
  contextIsolation: true;
  nodeIntegration: false;
  sandbox: true;
  webSecurity: true;
  allowRunningInsecureContent: false;
  experimentalFeatures: false;
  spellcheck: false;
};

export function createWebPreferences(preloadPath: string): JarvisWebPreferences {
  return {
    preload: preloadPath,
    contextIsolation: true,
    nodeIntegration: false,
    sandbox: true,
    webSecurity: true,
    allowRunningInsecureContent: false,
    experimentalFeatures: false,
    spellcheck: false,
  };
}

export type JarvisWindowOptions = {
  width: number;
  height: number;
  minWidth: number;
  minHeight: number;
  show: boolean;
  autoHideMenuBar: true;
  backgroundColor: string;
  title: string;
  webPreferences: JarvisWebPreferences;
};

export function createWindowOptions(
  webPreferences: JarvisWebPreferences,
  size: { width: number; height: number } = { width: 1280, height: 800 },
): JarvisWindowOptions {
  return {
    width: size.width,
    height: size.height,
    minWidth: 960,
    minHeight: 640,
    show: false,
    autoHideMenuBar: true,
    backgroundColor: "#05070D",
    title: "JARVIS",
    webPreferences,
  };
}

/**
 * Navigation allowlist: the renderer may only ever be at (and navigate
 * to) the Vite dev origin — or, in shipped/dist hosting, this app's own
 * built index.html on the file: protocol. Everything else (remote http,
 * javascript:, data:, unknown schemes) is denied.
 */
export function shouldAllowNavigation(
  targetUrl: string,
  opts: { devServerUrl: string | null; isDistHosting: boolean },
): boolean {
  let url: URL;
  try {
    url = new URL(targetUrl);
  } catch {
    return false;
  }
  if (opts.devServerUrl) {
    try {
      if (url.origin === new URL(opts.devServerUrl).origin) return true;
    } catch {
      return false;
    }
  }
  if (opts.isDistHosting && url.protocol === "file:") {
    return url.pathname.replace(/\\/g, "/").endsWith("/index.html");
  }
  return false;
}

export type OpenWindowDecision = { action: "deny" };

/** Deny every window.open / target=_blank attempt from the renderer. */
export function denyOpenWindow(): OpenWindowDecision {
  return { action: "deny" };
}

/** Deny every permission request/check (camera, mic, notifications…). */
export function denyPermission(): boolean {
  return false;
}
