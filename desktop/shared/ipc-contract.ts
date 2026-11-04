/**
 * Single source of truth for the renderer → main IPC surface (Phase 6.1).
 *
 * This module is compiled into the Electron main bundle, the preload
 * bundle, the renderer bundle, and the contract tests, so both sides of
 * every channel can never drift apart. It is TYPES-ONLY at runtime for
 * the shared result envelope: the channel/method tables are real data
 * used by the preload bridge and the main-process registry.
 *
 * Rules baked in here (see desktop/README.md § security):
 * - Every channel name is namespaced `jarvis:` and explicitly listed.
 * - The preload bridge exposes ONLY the methods in PRELOAD_METHODS —
 *   never raw ipcRenderer, never Node.
 * - Renderer network access does not exist; every backend call happens
 *   in the main process with auth attached there.
 */

/** All IPC channel names start with this prefix. */
export const IPC_CHANNEL_PREFIX = "jarvis:";

/** Explicit, exhaustive channel registry. Add new channels here only. */
export const IPC_CHANNELS = {
  backendStatus: "jarvis:backend-status",
  checkHealth: "jarvis:check-health",
  getSystemInfo: "jarvis:get-system-info",
} as const;

export type IpcChannel = (typeof IPC_CHANNELS)[keyof typeof IPC_CHANNELS];

/* ------------------------------------------------------------------ */
/* Result envelope (wire shape both sides agree on)                    */
/* ------------------------------------------------------------------ */

export type IpcErrorCode =
  | "UNKNOWN_CHANNEL"
  | "BAD_PAYLOAD"
  | "UNAUTHORIZED_FRAME"
  | "AUTH_NOT_CONFIGURED"
  | "BACKEND_UNREACHABLE"
  | "TIMEOUT"
  | "HTTP_STATUS"
  | "UNEXPECTED";

export type IpcResult<T> =
  | { ok: true; data: T }
  | { ok: false; code: IpcErrorCode; message: string; httpStatus?: number };

export function ipcOk<T>(data: T): IpcResult<T> {
  return { ok: true, data };
}

export function ipcError<T = never>(
  code: IpcErrorCode,
  message: string,
  httpStatus?: number,
): IpcResult<T> {
  return httpStatus === undefined
    ? { ok: false, code, message }
    : { ok: false, code, message, httpStatus };
}

/* ------------------------------------------------------------------ */
/* Wire payloads / projections (kept deliberately small and serializable) */
/* ------------------------------------------------------------------ */

/** GET /api/health — see backend/app/main.py (unauthenticated). */
export type HealthSnapshot = {
  name: string;
  status: string;
  version: string;
};

/**
 * GET /api/system-info — the UI projection of the backend ToolResult
 * (backend/app/schemas/tool.py). The full generated schema type lives in
 * electron/api/schema.gen.ts; the renderer only ever sees this narrowed,
 * UI-facing projection.
 */
export type SystemInfoSummary = {
  toolName: string;
  status: "success" | "already_exists" | "failure";
  success: boolean;
  message: string;
  /** Bounded string map from the backend (≤32 items, string→string). */
  data: Record<string, string> | null;
  timestamp: string;
};

/** Local backend auth/session configuration as seen by the main process. */
export type BackendStatus = {
  apiOrigin: string;
  tokenConfigured: boolean;
  tokenSource: "env" | "backendEnvFile" | null;
  /** Session identifier, masked (e.g. `a1b2c3…9f0e`) — full value never leaves the main process. */
  sessionIdMasked: string;
  appVersion: string;
  runtime: { electron: string; node: string };
};

/** Measured GET /api/health round trip, for the status view. */
export type HealthCheck = {
  snapshot: HealthSnapshot;
  latencyMs: number;
  checkedAt: string;
};

/* ------------------------------------------------------------------ */
/* Preload bridge surface (the ONLY API the renderer can reach)         */
/* ------------------------------------------------------------------ */

/**
 * The preload bridge is generated from this table: one row per exposed
 * method. The registry, the preload, the renderer service, and the tests
 * all consume this table, so an unlisted channel is unreachable by
 * construction.
 */
export const PRELOAD_METHODS = [
  { method: "backendStatus", channel: IPC_CHANNELS.backendStatus },
  { method: "checkHealth", channel: IPC_CHANNELS.checkHealth },
  { method: "getSystemInfo", channel: IPC_CHANNELS.getSystemInfo },
] as const;

export type PreloadMethodName = (typeof PRELOAD_METHODS)[number]["method"];

/** The object exposed on `window.jarvis`. */
export type JarvisApi = {
  backendStatus(): Promise<IpcResult<BackendStatus>>;
  checkHealth(): Promise<IpcResult<HealthCheck>>;
  getSystemInfo(): Promise<IpcResult<SystemInfoSummary>>;
};

/** All 6.1 channels take a canonical empty payload ({}). */
export type EmptyPayload = Record<string, never>;

/** Human-readable copy for error codes (renderer-side). */
export const IPC_ERROR_COPY: Record<IpcErrorCode, string> = {
  UNKNOWN_CHANNEL: "Internal error: unknown IPC channel.",
  BAD_PAYLOAD: "Internal error: malformed IPC payload.",
  UNAUTHORIZED_FRAME: "Internal error: IPC frame not authorized.",
  AUTH_NOT_CONFIGURED: "Backend authentication is not configured (JARVIS_API_TOKEN).",
  BACKEND_UNREACHABLE: "JARVIS backend is unreachable. Start the backend and retry.",
  TIMEOUT: "The backend did not respond in time.",
  HTTP_STATUS: "The backend returned an error response.",
  UNEXPECTED: "An unexpected error occurred.",
};
