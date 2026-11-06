/** Electron security configuration tests (acceptance: verify config). */

import test from "node:test";
import assert from "node:assert/strict";
import {
  createWebPreferences,
  createWindowOptions,
  shouldAllowNavigation,
  denyOpenWindow,
  denyPermission,
  isMicPermissionAllowed,
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

test("microphone permission is limited to trusted audio-only main frames", () => {
  const dev = { devServerUrl: "http://127.0.0.1:5173", isDistHosting: false };
  const dist = { devServerUrl: null, isDistHosting: true };
  const devFrame = "http://127.0.0.1:5173/";
  const distFrame = "file:///C:/work/J.A.R.V.I.S/desktop/dist/renderer/index.html";

  assert.equal(isMicPermissionAllowed("media", devFrame, dev, ["audio"]), true);
  assert.equal(
    isMicPermissionAllowed("media", distFrame, dist, {
      isMainFrame: true,
      frameUrl: distFrame,
      mediaTypes: ["audio"],
    }),
    true,
  );

  assert.equal(
    isMicPermissionAllowed("media", devFrame, dev, {
      isMainFrame: false,
      frameUrl: devFrame,
      mediaTypes: ["audio"],
    }),
    false,
    "subframes are never authorized",
  );
  assert.equal(
    isMicPermissionAllowed("media", "http://localhost:5173/", dev, ["audio"]),
    false,
    "localhost is not the trusted development origin",
  );
  assert.equal(
    isMicPermissionAllowed("media", "https://attacker.example/", dist, ["audio"]),
    false,
    "remote origins are denied",
  );
  assert.equal(
    isMicPermissionAllowed("media", devFrame, dev, {
      isMainFrame: true,
      frameUrl: "https://attacker.example/",
      mediaTypes: ["audio"],
    }),
    false,
    "the authorized WebContents frame must also be trusted",
  );

  for (const mediaTypes of [[], ["video"], ["audio", "video"]]) {
    assert.equal(
      isMicPermissionAllowed("media", devFrame, dev, {
        isMainFrame: true,
        frameUrl: devFrame,
        mediaTypes,
      }),
      false,
      `media request ${mediaTypes.join(",")} must not grant the microphone`,
    );
  }
  assert.equal(
    isMicPermissionAllowed("notifications", devFrame, dev, ["audio"]),
    false,
    "non-media permissions remain denied",
  );
});

test("permission checks use the same trusted audio-only boundary", () => {
  const dev = { devServerUrl: "http://127.0.0.1:5173", isDistHosting: false };
  const frameUrl = "http://127.0.0.1:5173/";

  assert.equal(
    isMicPermissionAllowed("media", frameUrl, dev, {
      isMainFrame: true,
      frameUrl,
      mediaType: "audio",
    }),
    true,
  );
  for (const mediaType of ["video", "unknown", undefined]) {
    assert.equal(
      isMicPermissionAllowed("media", frameUrl, dev, {
        isMainFrame: true,
        frameUrl,
        mediaType,
      }),
      false,
      `check media type ${String(mediaType)} must be denied`,
    );
  }
  assert.equal(
    isMicPermissionAllowed("media", frameUrl, dev, {
      isMainFrame: true,
      frameUrl: null,
      mediaType: "audio",
    }),
    false,
    "a check without an authorized WebContents frame is denied",
  );
});

test("window.open and fallback permissions are denied", () => {
  assert.deepEqual(denyOpenWindow(), { action: "deny" });
  assert.equal(denyPermission(), false);
});
