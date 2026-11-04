// Regenerates the desktop-side API contract snapshots from the JARVIS
// backend's FastAPI OpenAPI schema and codes TypeScript types from it.
//
// - Reads the schema by importing app.main (no server, no network, no
//   backend files are modified; the backend .venv is used if present).
// - Writes desktop/openapi.json (the committed snapshot) and
//   desktop/electron/api/schema.gen.ts (coded endpoint/schemas types).
// - Fails loudly if the backend cannot be imported or the generator fails.

import { createRequire } from "node:module";
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const REQUIRE = createRequire(import.meta.url);
const DESKTOP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const REPO_ROOT = resolve(DESKTOP_ROOT, "..");
const BACKEND_DIR = process.env.JARVIS_BACKEND_DIR
  ? resolve(process.env.JARVIS_BACKEND_DIR)
  : resolve(REPO_ROOT, "backend");

const OPENAPI_OUT = resolve(DESKTOP_ROOT, "openapi.json");
const API_TYPES_OUT = resolve(DESKTOP_ROOT, "electron", "main", "api", "schema.gen.ts");

const DUMP_CODE = `
import json, os
from app.main import app
spec = app.openapi()
with open(os.environ["OPENAPI_OUT"], "w", encoding="utf-8") as fh:
    json.dump(spec, fh, indent=2)
    fh.write("\\n")
`;

function backendPython() {
  const venvPython = resolve(BACKEND_DIR, ".venv", "Scripts", "python.exe");
  if (process.platform === "win32" && existsSync(venvPython)) return venvPython;
  return "python";
}

function run(spec) {
  const result = spawnSync(spec.cmd, spec.args, spec.options);
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(
      `${spec.label} failed (${result.status}):\n${String(result.stderr)}${String(result.stdout)}`,);
  }
}

// 1. Dump the backend OpenAPI schema (read-only import of app.main).
if (!existsSync(resolve(BACKEND_DIR, "app", "main.py"))) {
  console.error(`Backend not found at ${BACKEND_DIR} (use JARVIS_BACKEND_DIR to override).`);
  process.exit(1);
}
mkdirSync(DESKTOP_ROOT, { recursive: true });
run({
  label: "openapi dump (backend import)",
  cmd: backendPython(),
  args: ["-X", "utf8", "-c", DUMP_CODE],
  options: {
    cwd: BACKEND_DIR,
    encoding: "utf8",
    env: { ...process.env, OPENAPI_OUT, PYTHONUTF8: "1" },
  },
});

// 2. Generate coded TS types from the committed snapshot.
// Invoke the generator's JS entrypoint directly (no .cmd shim, no shell).
const generatorEntry = resolve(DESKTOP_ROOT, "node_modules", "openapi-typescript", "bin", "cli.js");
if (!existsSync(generatorEntry)) {
  console.error(`openapi-typescript entrypoint not found at ${generatorEntry} — run 'npm install'.`);
  process.exit(1);
}
run({
  label: "openapi-typescript",
  cmd: process.execPath,
  args: [generatorEntry, OPENAPI_OUT, "-o", API_TYPES_OUT],
  options: { cwd: DESKTOP_ROOT, encoding: "utf8" },
});

// 3. Report what was generated.
const specText = JSON.parse(readFileSync(OPENAPI_OUT, "utf8"));
const paths = Object.keys(specText.paths ?? {});
console.log(`openapi.json rewritten: ${paths.length} paths`);
console.log(`types written:  ${API_TYPES_OUT}`);
if (!paths.includes("/api/health") || !paths.includes("/api/chat")) {
  console.error("Generated spec is missing expected endpoints; refusing to silently pass.");
  process.exit(1);
}

function getNpx() {
  // On Windows spawnSync needs the .cmd shim (or shell: true resolves it).
  return { cmd: "npx" };
}
