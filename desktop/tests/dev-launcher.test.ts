/** Regression coverage for Windows Electron process visibility. */

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const launcher = readFileSync(resolve(process.cwd(), "scripts", "dev.mjs"), "utf8");

function spawnBlock(marker: string): string {
  const start = launcher.indexOf(marker);
  assert.notEqual(start, -1, `launcher must contain ${marker}`);
  const end = launcher.indexOf("\n  });", start);
  assert.notEqual(end, -1, `${marker} spawn block must close`);
  return launcher.slice(start, end);
}

test("Electron development spawn keeps the GUI process visible on Windows", () => {
  const electronSpawn = spawnBlock("electronChild = spawn(electronBinary");
  assert.match(electronSpawn, /windowsHide:\s*false/);
});

test("Vite helper remains hidden while Electron is visible", () => {
  const viteSpawn = spawnBlock("viteChild = spawn(process.execPath");
  assert.match(viteSpawn, /windowsHide:\s*true/);
});
