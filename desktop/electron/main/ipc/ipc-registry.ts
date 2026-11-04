/**
 * Pure IPC dispatch registry (no electron imports — contract tests run
 * it in plain Node).
 *
 * The bridge from Electron's ipcMain is a thin adapter (ipc/handlers.ts)
 * that delegates HERE. Rules enforced for every dispatch:
 *   1. the channel name must be exactly one of IPC_CHANNELS,
 *   2. the calling frame's URL must be an authorized app origin,
 *   3. payloads must be the canonical empty object for 6.1 channels,
 *   4. handlers return the FINAL IpcResult envelope themselves (no
 *      double wrapping), and thrown errors never leak stack traces —
 *      they collapse to the bounded UNEXPECTED code.
 */

import {
  IPC_CHANNEL_PREFIX,
  IPC_CHANNELS,
  ipcError,
  type EmptyPayload,
  type IpcChannel,
  type IpcResult,
} from "../../../shared/ipc-contract";

const KNOWN_CHANNELS: ReadonlySet<string> = new Set(Object.values(IPC_CHANNELS));

export type IpcHandler = (payload: EmptyPayload) => Promise<IpcResult<unknown>>;

export type IpcRegistry = {
  /** Authoritative renderer entry point. */
  dispatch(channel: string, rawPayload: unknown, callerUrl: string | null): Promise<IpcResult<unknown>>;
};

/** True for `undefined` or a plain, empty, non-array object. */
export function isEmptyPayload(value: unknown): value is EmptyPayload {
  if (value === undefined) return true;
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  return Object.keys(value).length === 0;
}

/** Fixed, non-sensitive internal-error text (never forwards internals). */
const INTERNAL_ERROR_TEXT = "Internal error: unexpected failure.";

export function createIpcRegistry(
  handlers: Record<IpcChannel, IpcHandler>,
  senderIsTrusted: (callerUrl: string | null) => boolean,
): IpcRegistry {
  return {
    async dispatch(channel, rawPayload, callerUrl) {
      if (!channel.startsWith(IPC_CHANNEL_PREFIX) || !KNOWN_CHANNELS.has(channel)) {
        return ipcError("UNKNOWN_CHANNEL", `Channel '${channel.slice(0, 64)}' is not registered.`);
      }
      if (!senderIsTrusted(callerUrl)) {
        return ipcError("UNAUTHORIZED_FRAME", "The calling frame is not an authorized app frame.");
      }
      if (!isEmptyPayload(rawPayload)) {
        return ipcError("BAD_PAYLOAD", "This channel accepts an empty payload only.");
      }
      try {
        // Handlers own the envelope: they return ok(...) or a backend
        // failure directly; the registry adds no second wrapper.
        return await handlers[channel as IpcChannel]({});
      } catch (error) {
        // Bounded, fixed text only. Handler internals (messages, stack
        // traces) stay in the main-process console, never the renderer.
        console.error("[ipc] handler failure:", error instanceof Error ? error.message : error);
        return ipcError("UNEXPECTED", INTERNAL_ERROR_TEXT);
      }
    },
  };
}
