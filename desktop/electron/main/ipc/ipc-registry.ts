/**
 * Pure IPC dispatch registry (no electron imports — contract tests run
 * it in plain Node).
 *
 * The bridge from Electron's ipcMain is a thin adapter (ipc/handlers.ts)
 * that delegates HERE. Rules enforced for every dispatch:
 *   1. the channel name must be exactly one of IPC_CHANNELS,
 *   2. the calling frame's URL must be an authorized app origin,
 *   3. the payload must pass that channel's validator from the shared
 *      PAYLOAD_VALIDATORS table (BAD_PAYLOAD with fixed copy otherwise),
 *   4. handlers return the FINAL IpcResult envelope themselves (no
 *      double wrapping), and thrown errors never leak stack traces —
 *      they collapse to the bounded, fixed UNEXPECTED code.
 */

import {
  IPC_CHANNEL_PREFIX,
  IPC_CHANNELS,
  PAYLOAD_VALIDATORS,
  ipcError,
  type IpcPayloadMap,
  type IpcResult,
} from "../../../shared/ipc-contract";

const KNOWN_CHANNELS: ReadonlySet<string> = new Set(Object.values(IPC_CHANNELS));

/**
 * One handler per channel, typed with its channel's validated payload.
 * The table literal is type-checked against IpcPayloadMap, so a handler
 * consuming the wrong payload shape fails the build.
 */
export type IpcHandlers = {
  [K in keyof IpcPayloadMap]: (
    payload: IpcPayloadMap[K],
  ) => Promise<IpcResult<unknown>>;
};

export type IpcRegistry = {
  /** Authoritative renderer entry point. */
  dispatch(
    channel: string,
    rawPayload: unknown,
    callerUrl: string | null,
  ): Promise<IpcResult<unknown>>;
};

/** Fixed, non-sensitive internal-error text (never forwards internals). */
const INTERNAL_ERROR_TEXT = "Internal error: unexpected failure.";

export function createIpcRegistry(
  handlers: IpcHandlers,
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
      // The validator/generic join is the one deliberate cast: the tables
      // are pairwise-complete by construction (keyof IpcPayloadMap) and a
      // contract test asserts every channel appears in BOTH tables.
      const validator = PAYLOAD_VALIDATORS[channel as keyof IpcPayloadMap];
      const validated = validator(rawPayload);
      if (!validated.ok) {
        return ipcError("BAD_PAYLOAD", validated.reason);
      }
      try {
        const handler = handlers[channel as keyof IpcPayloadMap] as (
          payload: unknown,
        ) => Promise<IpcResult<unknown>>;
        // Handlers own the envelope: they return ok(...) or a backend
        // failure directly; the registry adds no second wrapper.
        return await handler(validated.value);
      } catch (error) {
        // Bounded, fixed text only. Handler internals (messages, stack
        // traces) stay in the main-process console, never the renderer.
        console.error("[ipc] handler failure:", error instanceof Error ? error.message : error);
        return ipcError("UNEXPECTED", INTERNAL_ERROR_TEXT);
      }
    },
  };
}
