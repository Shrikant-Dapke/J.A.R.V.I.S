/**
 * Electron ipcMain glue (thin): forwards whitelisted channels from the
 * preloaded renderer into the pure dispatch registry and applies the
 * trusted-frame check from app-security (same allowlist as navigation:
 * the dev server origin in dev, the built index.html in dist hosting).
 */

import { ipcMain, type IpcMainInvokeEvent } from "electron";
import { createIpcRegistry, type IpcRegistry } from "./ipc-registry";
import type { HttpBackendApiClient } from "../api/client";
import { ipcOk, type BackendStatus } from "../../../shared/ipc-contract";
import {
  DEFAULT_HEALTH_TIMEOUT_MS,
  DEFAULT_SYSTEM_INFO_TIMEOUT_MS,
} from "../api/client";
import { shouldAllowNavigation } from "../app-security";

export type IpcHandlerDeps = {
  client: HttpBackendApiClient;
  getStatus: () => BackendStatus;
  /** Authorized caller origins (dev server / dist index). */
  navigationTrust: { devServerUrl: string | null; isDistHosting: boolean };
};

function callerUrl(event: IpcMainInvokeEvent): string | null {
  try {
    return event.senderFrame?.url ?? null;
  } catch {
    return null;
  }
}

export function createJarvisIpcRegistry(deps: IpcHandlerDeps): IpcRegistry {
  return createIpcRegistry(
    {
      "jarvis:backend-status": async () => ipcOk(deps.getStatus()),
      "jarvis:check-health": async () => deps.client.getHealth(DEFAULT_HEALTH_TIMEOUT_MS),
      "jarvis:get-system-info": async () =>
        deps.client.getSystemInfo(DEFAULT_SYSTEM_INFO_TIMEOUT_MS),
    },
    (url) => shouldAllowNavigation(url ?? "", deps.navigationTrust),
  );
}

/** Attach the registry to Electron's process-global ipcMain. */
export function registerIpcHandlers(registry: IpcRegistry): void {
  ipcMain.handle("jarvis:backend-status", (event, payload) =>
    registry.dispatch("jarvis:backend-status", payload, callerUrl(event)),
  );
  ipcMain.handle("jarvis:check-health", (event, payload) =>
    registry.dispatch("jarvis:check-health", payload, callerUrl(event)),
  );
  ipcMain.handle("jarvis:get-system-info", (event, payload) =>
    registry.dispatch("jarvis:get-system-info", payload, callerUrl(event)),
  );
}
