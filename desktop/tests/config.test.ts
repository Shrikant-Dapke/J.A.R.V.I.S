/** Configuration contract tests: token sources, ports, loopback origin. */

import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  DesktopConfigError,
  loadDesktopConfig,
  parseEnvFileText,
  parsePort,
} from "../electron/main/config";

test("env file parser handles comments, CRLF, BOM, and quotes", () => {
  const text = "\uFEFF# comment\r\nJARVIS_API_TOKEN=abc\r\n\nPORT=8123\r\nQUOTED=\"yes\"\nSINGLE='ok'";
  const parsed = parseEnvFileText(text);
  assert.deepEqual(parsed, {
    JARVIS_API_TOKEN: "abc",
    PORT: "8123",
    QUOTED: "yes",
    SINGLE: "ok",
  });
});

test("ports must be integers in range", () => {
  assert.equal(parsePort("8000"), 8000);
  assert.throws(() => parsePort("0"), DesktopConfigError);
  assert.throws(() => parsePort("70000"), DesktopConfigError);
  assert.throws(() => parsePort("abc"), DesktopConfigError);
  assert.throws(() => parsePort("-1"), DesktopConfigError);
});

test("non-loopback hosts fail closed", () => {
  assert.throws(
    () =>
      loadDesktopConfig({
        env: {
          JARVIS_API_TOKEN: "t".repeat(40),
          JARVIS_BACKEND_HOST: "192.168.1.20",
        },
        backendEnvPath: null,
      }),
    /loopback/,
  );
});

test("weak tokens fail closed like the backend validator", () => {
  assert.throws(
    () =>
      loadDesktopConfig({
        env: { JARVIS_API_TOKEN: "short" },
        backendEnvPath: null,
      } as never),
    /32/,
  );
});

test("backend/.env supplies token and port; env overrides win", () => {
  const dir = mkdtempSync(join(tmpdir(), "jarvis-config-"));
  const envPath = join(dir, ".env");
  try {
    writeFileSync(
      envPath,
      ["# local config", "JARVIS_API_TOKEN=" + "z".repeat(40), "PORT=8123", "HOST=127.0.0.1"].join(
        "\n",
      ),
      "utf8",
    );

    const fromFile = loadDesktopConfig({ env: {}, backendEnvPath: envPath });
    assert.equal(fromFile.tokenSource, "backendEnvFile");
    assert.equal(fromFile.token, "z".repeat(40));
    assert.equal(fromFile.port, 8123);
    assert.equal(fromFile.apiOrigin, "http://127.0.0.1:8123");

    const envWins = loadDesktopConfig({
      env: { JARVIS_API_TOKEN: "e".repeat(40), JARVIS_BACKEND_PORT: "9123" },
      backendEnvPath: envPath,
    });
    assert.equal(envWins.tokenSource, "env");
    assert.equal(envWins.token, "e".repeat(40));
    assert.equal(envWins.port, 9123);
    assert.equal(envWins.apiOrigin, "http://127.0.0.1:9123");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("missing .env is neutral (no token, default port)", () => {
  const config = loadDesktopConfig({ env: {}, backendEnvPath: null });
  assert.equal(config.token, null);
  assert.equal(config.tokenSource, null);
  assert.equal(config.port, 8000);
  assert.equal(config.apiOrigin, "http://127.0.0.1:8000");
});
