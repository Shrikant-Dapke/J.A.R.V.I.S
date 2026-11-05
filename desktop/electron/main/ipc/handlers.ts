/**
 * Electron ipcMain glue (thin): forwards whitelisted channels from the
 * preloaded renderer into the pure dispatch registry and applies the
 * trusted-frame check from app-security (same allowlist as navigation:
 * the dev server origin in dev, the built index.html in dist hosting).
 *
 * Registration is driven by the shared PRELOAD_METHODS table, so a
 * channel cannot be exposed in the preload yet forgotten here (or vice
 * versa) — the parity is contract-tested too.
 */

import { ipcMain, type IpcMainInvokeEvent } from "electron";
import { createIpcRegistry, type IpcHandlers, type IpcRegistry } from "./ipc-registry";
import type { HttpBackendApiClient } from "../api/client";
import {
  APPROVALS_TIMEOUT_MS,
  APPROVAL_ACTION_TIMEOUT_MS,
  DEFAULT_CHAT_TIMEOUT_MS,
  DEFAULT_CONVERSATIONS_TIMEOUT_MS,
  DEFAULT_DELETE_TIMEOUT_MS,
  DEFAULT_HEALTH_TIMEOUT_MS,
  DEFAULT_HISTORY_TIMEOUT_MS,
  DEFAULT_SYSTEM_INFO_TIMEOUT_MS,
  PLAN_DETAIL_TIMEOUT_MS,
  PLAN_RECOVERY_TIMEOUT_MS,
  PLAN_STEP_REQUEST_TIMEOUT_MS,
  PLANS_TIMEOUT_MS,
} from "../api/client";
import {
  ipcOk,
  PRELOAD_METHODS,
  type ApprovalIdPayload,
  type BackendStatus,
  type ChatSendPayload,
  type ConversationIdPayload,
  type ConversationListPayload,
  type PlanIdPayload,
  type PlanStepPayload,
} from "../../../shared/ipc-contract";
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
  const handlers: IpcHandlers = {
    "jarvis:backend-status": async () => ipcOk(deps.getStatus()),
    "jarvis:check-health": async () => deps.client.getHealth(DEFAULT_HEALTH_TIMEOUT_MS),
    "jarvis:get-system-info": async () => deps.client.getSystemInfo(DEFAULT_SYSTEM_INFO_TIMEOUT_MS),
    "jarvis:send-chat": async (payload: ChatSendPayload) =>
      deps.client.sendChat(payload, DEFAULT_CHAT_TIMEOUT_MS),
    "jarvis:list-conversations": async (payload: ConversationListPayload) =>
      deps.client.listConversations(payload, DEFAULT_CONVERSATIONS_TIMEOUT_MS),
    "jarvis:get-conversation": async (payload: ConversationIdPayload) =>
      deps.client.getConversation(payload, DEFAULT_HISTORY_TIMEOUT_MS),
    "jarvis:delete-conversation": async (payload: ConversationIdPayload) =>
      deps.client.deleteConversation(payload, DEFAULT_DELETE_TIMEOUT_MS),
    "jarvis:list-approvals": async (payload: ConversationListPayload) =>
      deps.client.listApprovals(payload, APPROVALS_TIMEOUT_MS),
    "jarvis:get-approval": async (payload: ApprovalIdPayload) =>
      deps.client.getApproval(payload, APPROVALS_TIMEOUT_MS),
    "jarvis:approve-approval": async (payload: ApprovalIdPayload) =>
      deps.client.approveApproval(payload, APPROVAL_ACTION_TIMEOUT_MS),
    "jarvis:reject-approval": async (payload: ApprovalIdPayload) =>
      deps.client.rejectApproval(payload, APPROVALS_TIMEOUT_MS),
    "jarvis:list-plans": async (payload: ConversationListPayload) =>
      deps.client.listPlans(payload, PLANS_TIMEOUT_MS),
    "jarvis:get-plan": async (payload: PlanIdPayload) =>
      deps.client.getPlan(payload, PLAN_DETAIL_TIMEOUT_MS),
    "jarvis:request-plan-step": async (payload: PlanStepPayload) =>
      deps.client.requestPlanStep(payload, PLAN_STEP_REQUEST_TIMEOUT_MS),
    "jarvis:resolve-plan-recovery": async (payload: PlanStepPayload) =>
      deps.client.resolvePlanRecovery(payload, PLAN_RECOVERY_TIMEOUT_MS),
  };
  return createIpcRegistry(handlers, (url) =>
    shouldAllowNavigation(url ?? "", deps.navigationTrust),
  );
}

/** Attach the registry to Electron's process-global ipcMain. */
export function registerIpcHandlers(registry: IpcRegistry): void {
  for (const { channel } of PRELOAD_METHODS) {
    ipcMain.handle(channel, (event, payload) =>
      registry.dispatch(channel, payload, callerUrl(event)),
    );
  }
}
