/** Electron security configuration tests (acceptance: verify config). */

import test from "node:test";
import assert from "node:assert/strict";
import {
  createWebPreferences,
  createWindowOptions,
  shouldAllowNavigation,
  denyOpenWindow,
  denyPermission,
} from "../electron/main/app-security";

test("web preferences harden the renderer", () => {
  const prefs = createWebPreferences("PRELOAD-CJS.js");
  assert.equal(prefs.contextIsolation, true, "contextIsolation must stay enabled");
  assert.equal(prefs.nodeIntegration, false, "nodeIntegration must stay disabled");
  assert.equal(prefs.sandbox, true, "preload must run sandboxed");
  assert.equal(prefs.webSecurity, true);
  assert.equal(prefs.allowRunningInsecureContent, false);
  assert.equal(prefs.experimentalFeatures, false);
  assert.equal(prefs.spellcheck, false);
  assert.equal(prefs.preload, "PRELOAD-CJS.js");
});

test("window options pin the HUD frame defaults", () => {
  const options = createWindowOptions(createWebPreferences("x"));
  assert.equal(options.width, 1280);
  assert.equal(options.height, 800);
  assert.equal(options.minWidth, 960);
  assert.equal(options.minHeight, 640);
  assert.equal(options.show, false, "window shows only after ready-to-show");
  assert.equal(options.backgroundColor, "#05070D");
});

test("navigation allowlist only permits the app's own origins", () => {
  const dev = { devServerUrl: "http://127.0.0.1:5173", isDistHosting: false };
  const dist = { devServerUrl: null, isDistHosting: true };

  assert.equal(shouldAllowNavigation("http://127.0.0.1:5173/", dev), true, "dev origin allowed");
  assert.equal(shouldAllowNavigation("http://127.0.0.1:5173/assets/x.js", dev), true);
  assert.equal(shouldAllowNavigation("http://localhost:5173/", dev), false, "port hijack denied");
  assert.equal(shouldAllowNavigation("http://127.0.0.1:8000/", dev), false, "backend origin denied");
  assert.equal(
    shouldAllowNavigation("https://attacker.example/", dist),
    false,
    "remote origins denied",
  );
  assert.equal(
    shouldAllowNavigation("file:///C:/work/J.A.R.V.I.S/desktop/dist/renderer/index.html", dist),
    true,
    "built index allowed in dist hosting",
  );
  assert.equal(
    shouldAllowNavigation("file:///C:/work/private/secrets.html", dist),
    false,
    "non-app files denied",
  );
  assert.equal(
    shouldAllowNavigation("javascript:alert(1)", dev),
    false,
    "javascript: URLs denied",
  );
  assert.equal(shouldAllowNavigation("data:text/html,x", dev), false, "data: URLs denied");
  assert.equal(shouldAllowNavigation("not a url", dev), false, "unparsable URLs denied");
});

test("window.open and permissions are denied", () => {
  assert.deepEqual(denyOpenWindow(), { action: "deny" });
  assert.equal(denyPermission(), false);
});
