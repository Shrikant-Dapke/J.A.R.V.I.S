/**
 * Preload bridge — the ONLY surface connecting the renderer to the
 * main process (Phase 6.1). It runs in the sandboxed preload world with
 * contextIsolation ON and nodeIntegration OFF, exposes a narrow,
 * promise-based API generated from the shared IPC contract, and never
 * forwards raw Node/`ipcRenderer` objects to web frames.
 */

import { contextBridge, ipcRenderer, type IpcRendererEvent } from "electron";
import {
  PRELOAD_METHODS,
  VOICE_EVENTS_CHANNEL,
} from "../../shared/ipc-contract";
import type { VoiceEventPayload } from "../../shared/voice-engine";

// Structural narrowing keeps every call `Promise<IpcResult<T>>` and
// avoids the any-typed index escape hatch on ipcRenderer.
type IpcInvoke = (
  channel: string,
  payload: unknown,
) => Promise<unknown>;

const invoke = ipcRenderer.invoke as IpcInvoke;

const onVoiceEvent = (
  listener: (event: VoiceEventPayload) => void,
): (() => void) => {
  const wrapped = (_event: IpcRendererEvent, payload: unknown): void => {
    listener(payload as VoiceEventPayload);
  };
  ipcRenderer.on(VOICE_EVENTS_CHANNEL, wrapped);
  return () => ipcRenderer.removeListener(VOICE_EVENTS_CHANNEL, wrapped);
};

const bridge = {
  ...Object.fromEntries(
    PRELOAD_METHODS.map((entry) => [
      entry.method,
      (payload: unknown = {}) => invoke(entry.channel, payload),
    ]),
  ),
  // Main → renderer events are deliberately outside PRELOAD_METHODS and
  // IPC_CHANNELS: they are never renderer-invokable commands.
  onVoiceEvent,
};

contextBridge.exposeInMainWorld("jarvis", bridge);
