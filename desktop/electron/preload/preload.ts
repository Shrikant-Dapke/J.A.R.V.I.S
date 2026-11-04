/**
 * Preload bridge — the ONLY surface connecting the renderer to the
 * main process (Phase 6.1). It runs in the sandboxed preload world with
 * contextIsolation ON and nodeIntegration OFF, exposes a narrow,
 * promise-based API generated from the shared IPC contract, and never
 * forwards raw Node/`ipcRenderer` objects to web frames.
 */

import { contextBridge, ipcRenderer } from "electron";
import { PRELOAD_METHODS } from "../../shared/ipc-contract";

// Structural narrowing keeps every call `Promise<IpcResult<T>>` and
// avoids the any-typed index escape hatch on ipcRenderer.
type IpcInvoke = (
  channel: string,
  payload: unknown,
) => Promise<unknown>;

const invoke = ipcRenderer.invoke as IpcInvoke;

const bridge = Object.fromEntries(
  PRELOAD_METHODS.map((entry) => [
    entry.method,
    (payload: unknown = {}) => invoke(entry.channel, payload),
  ]),
);

contextBridge.exposeInMainWorld("jarvis", bridge);
