/**
 * Explicit client session identity for the local API.
 *
 * The backend requires `X-JARVIS-Session` matching
 * `^[A-Za-z0-9_-]{16,128}$` (backend/app/core/security.py) and binds
 * conversations, plans, and approvals to (owner, session). One Electron
 * process = one session identifier, generated once at startup and held
 * only in the main process. The renderer never receives the raw value.
 */

import { randomBytes } from "node:crypto";

/** Mirrors backend/app/core/security.py exactly; contract-tested. */
export const SESSION_ID_PATTERN = /^[A-Za-z0-9_-]{16,128}$/;

/**
 * Generate one compliant session identifier (64 hex chars — a subset of
 * the allowed alphabet and inside the 16..128 length bound).
 */
export function generateSessionId(byteLength = 32): string {
  const id = randomBytes(byteLength).toString("hex");
  if (!SESSION_ID_PATTERN.test(id)) {
    // Defensive self-check; a double-width bug would surface here loudly.
    throw new Error("Generated session identifier failed the backend pattern");
  }
  return id;
}

let cachedSessionId: string | null = null;

/** Stable per-process session id (one Electron run = one identity). */
export function getSessionId(): string {
  cachedSessionId ??= generateSessionId();
  return cachedSessionId;
}

/** Renderer-safe masking; the raw value never crosses the bridge. */
export function maskSessionId(id: string): string {
  if (id.length <= 12) return "…";
  return `${id.slice(0, 6)}…${id.slice(-4)}`;
}
