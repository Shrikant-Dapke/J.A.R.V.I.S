/**
 * Desktop-side configuration: where the backend lives and the local API
 * credentials the MAIN PROCESS (only) may attach to requests.
 *
 * External sources:
 *   1. Electron process environment
 *   2. backend/.env  (private, local-only; parsed exactly enough to read
 *      flat KEY=VALUE lines — the file format the backend itself uses)
 *
 * Keys honored:
 *   JARVIS_API_TOKEN / API_TOKEN   — bearer secret (same identity
 *       (`sha256(token)`) the backend derives, so the desktop app must
 *       resolve it from the same sources the backend does to keep
 *       conversations, plans, and approvals attached to ONE identity.
 *   JARVIS_BACKEND_PORT / PORT     — backend port (1..65535; 8000 default)
 *   JARVIS_BACKEND_HOST / HOST     — loopback-only host; non-loopback
 *       values fail closed exactly like backend/app/core/config.py.
 *   JARVIS_BACKEND_DIR             — override the backend root directory.
 *   JARVIS_DEVTOOLS                — '1' opens DevTools (dev builds only).
 *
 * The token is NEVER included in payload shapes exposed to the renderer
 * — see shared/ipc-contract.ts (BackendStatus only carries a boolean).
 */

import { existsSync, readFileSync, statSync } from "node:fs";
import { isAbsolute, join, resolve } from "node:path";
import { isLoopbackHost } from "./net";

export type TokenSource = "env" | "backendEnvFile";

export type DesktopConfig = {
  /** Validated loopback http origin, e.g. `http://127.0.0.1:8000`. */
  apiOrigin: string;
  host: string;
  port: number;
  token: string | null;
  tokenSource: TokenSource | null;
  devtools: boolean;
};

export class DesktopConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DesktopConfigError";
  }
}

/** Parse flat KEY=VALUE lines — comments (#), blank lines, CRLF, BOM, and
 *  one level of surrounding quotes are handled; anything else is left so
 *  the backend's own validation stays the single source of truth. */
export function parseEnvFileText(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  const cleaned = text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
  for (const rawLine of cleaned.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq <= 0) continue;
    const key = line.slice(0, eq).trim();
    if (!key) continue;
    let value = line.slice(eq + 1).trim();
    if (
      (value.startsWith('"') && value.endsWith('"') && value.length >= 2) ||
      (value.startsWith("'") && value.endsWith("'") && value.length >= 2)
    ) {
      value = value.slice(1, -1);
    }
    out[key] = value;
  }
  return out;
}

function readOpt(env: Record<string, string | undefined>, ...keys: string[]): string | null {
  for (const key of keys) {
    const value = env[key];
    if (value !== undefined && value.trim() !== "") return value.trim();
  }
  return null;
}

export function parsePort(value: string): number {
  const port = Number.parseInt(value, 10);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new DesktopConfigError(`Backend port '${value}' is not a valid port (1..65535).`);
  }
  return port;
}

/** Walk upward from `startDir` until backend/app/main.py exists. */
export function findBackendDir(startDir: string): string | null {
  let dir = resolve(startDir);
  for (let hop = 0; hop < 10; hop += 1) {
    if (existsSync(join(dir, "backend", "app", "main.py"))) return join(dir, "backend");
    const parent = resolve(dir, "..");
    if (parent === dir) return null;
    dir = parent;
  }
  return null;
}

function readBackendEnvFile(path: string | null): Record<string, string> {
  if (!path || !existsSync(path)) return {};
  try {
    return parseEnvFileText(readFileSync(path, "utf8"));
  } catch {
    // Unreadable .env is a missing file to the desktop app; the backend
    // will fail closed on protected routes and the UI shows that state.
    return {};
  }
}

function buildOrigin(host: string, port: number): string {
  if (!isLoopbackHost(host)) {
    throw new DesktopConfigError(
      `Backend host '${host}' is not loopback. The desktop app only talks to the local API by design.`,
    );
  }
  return host.includes(":") ? `http://[${host}]:${port}` : `http://${host}:${port}`;
}

export type LoadConfigOptions = {
  env?: NodeJS.ProcessEnv;
  /** Explicit backend/.env path (tests). Default: findBackendDir + `.env`. */
  backendEnvPath?: string | null;
};

export function loadDesktopConfig(options: LoadConfigOptions = {}): DesktopConfig {
  const env = (options.env ?? process.env) as Record<string, string | undefined>;

  const backendEnv =
    options.backendEnvPath !== undefined
      ? readBackendEnvFile(options.backendEnvPath)
      : readBackendEnvFile(findBackendEnvPath(env));

  const envToken =
    readOpt(env, "JARVIS_API_TOKEN") ?? (env.API_TOKEN?.trim() ? env.API_TOKEN.trim() : null);
  const fileToken =
    backendEnv["JARVIS_API_TOKEN"] ??
    (backendEnv["API_TOKEN"] ? backendEnv["API_TOKEN"] : undefined);

  const token = (envToken ?? (fileToken ?? null)) || null;
  const tokenSource: TokenSource | null = envToken
    ? "env"
    : fileToken
      ? "backendEnvFile"
      : null;
  if (token && token.length < 32) {
    throw new DesktopConfigError(
      "JARVIS_API_TOKEN must contain at least 32 characters (backend requirement).",
    );
  }

  const portValue =
    readOpt(env, "JARVIS_BACKEND_PORT") ??
    (backendEnv["PORT"] && backendEnv["PORT"].trim() !== "" ? backendEnv["PORT"] : null) ??
    "8000";
  const port = parsePort(portValue);

  const host = readOpt(env, "JARVIS_BACKEND_HOST") ?? backendEnv["HOST"] ?? "127.0.0.1";
  const apiOrigin = buildOrigin(host, port);

  return {
    apiOrigin,
    host,
    port,
    token,
    tokenSource,
    devtools: readOpt(env, "JARVIS_DEVTOOLS") === "1",
  };
}

function findBackendEnvPath(env: Record<string, string | undefined>): string | null {
  const override = readOpt(env, "JARVIS_BACKEND_DIR");
  if (override) {
    // Accept either the backend dir itself or the repo root.
    if (isAbsoluteDirWithMainPy(override)) return join(override, ".env");
  }
  const backendDir = findBackendDir(resolve(__dirname, "..", "..", ".."));
  return backendDir ? join(backendDir, ".env") : null;
}

function isAbsoluteDirWithMainPy(value: string): boolean {
  const p = resolve(value, "app", "main.py");
  return isAbsolute(value) && existsSync(p) && statSync(p).isFile();
}
