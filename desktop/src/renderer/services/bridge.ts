/**
 * Renderer-side bridge service. The ONLY way the UI reaches the backend:
 * typed calls into the narrow preload bridge, with user-facing error copy
 * resolved from the shared error-code table (IPC_ERROR_COPY). No fetch,
 * no tokens, no session IDs in the renderer.
 */

import {
  IPC_ERROR_COPY,
  type BackendStatus,
  type HealthCheck,
  type IpcErrorCode,
  type IpcResult,
  type JarvisApi,
  type SystemInfoSummary,
} from "../../../shared/ipc-contract";

export class UnexpectedBridgeError extends Error {
  readonly context: string;
  constructor(context: string, detail: string) {
    super(`${context}: ${detail}`);
    this.name = "UnexpectedBridgeError";
    this.context = context;
  }
}

/**
 * Guard against a broken/missing preload (e.g. the dist renderer was
 * opened without the Electron shell). Fails loudly instead of racing on
 * an undefined bridge.
 */
function resolveBridge(): Readonly<JarvisApi> {
  const bridge = (globalThis.window as { jarvis?: unknown } | undefined)?.jarvis;
  if (!bridge) {
    throw new UnexpectedBridgeError(
      "preload",
      'window.jarvis is unavailable — the preload bridge is missing or the app was opened outside Electron.',
    );
  }
  return bridge as Readonly<JarvisApi>;
}

/** Narrow a bridge response to the shared result envelope. */
export function asIpcResult<T>(
  context: string,
  value: unknown,
): IpcResult<T> {
  if (typeof value !== "object" || value === null || !("ok" in value)) {
    throw new UnexpectedBridgeError(context, "malformed IPC response");
  }
  return value as IpcResult<T>;
}

/** English copy for an error result (never leaks internals). */
export function errorCopy(code: IpcErrorCode, fallback: string): string {
  return IPC_ERROR_COPY[code] ?? fallback;
}

const BRIDGE_CONTEXT = "jarvis bridge";

export async function fetchBackendStatus(): Promise<IpcResult<BackendStatus>> {
  return asIpcResult<BackendStatus>(BRIDGE_CONTEXT, await resolveBridge().backendStatus());
}

export async function fetchHealthCheck(): Promise<IpcResult<HealthCheck>> {
  return asIpcResult<HealthCheck>(BRIDGE_CONTEXT, await resolveBridge().checkHealth());
}

export async function fetchSystemInfo(): Promise<IpcResult<SystemInfoSummary>> {
  return asIpcResult<SystemInfoSummary>(BRIDGE_CONTEXT, await resolveBridge().getSystemInfo());
}
