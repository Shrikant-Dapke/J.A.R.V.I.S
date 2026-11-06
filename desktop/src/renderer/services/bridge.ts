/**
 * Renderer-side bridge service. The ONLY way the UI reaches the backend:
 * typed calls into the narrow preload bridge, with user-facing error copy
 * resolved from the shared error-code table (IPC_ERROR_COPY). No fetch,
 * no tokens, no session IDs in the renderer.
 */

import {
  IPC_ERROR_COPY,
  type ApprovalIdPayload,
  type ApprovalListPayload,
  type ApprovalListView,
  type ApprovalRequestView,
  type BackendStatus,
  type ChatExchange,
  type ChatSendPayload,
  type ConversationDeletedView,
  type ConversationHistoryView,
  type ConversationIdPayload,
  type ConversationListPayload,
  type ConversationListView,
  type EmptyPayload,
  type HealthCheck,
  type IpcErrorCode,
  type IpcResult,
  type JarvisApi,
  type PlanDetailView,
  type PlanIdPayload,
  type PlanListView,
  type PlanRecoveryResolvedView,
  type PlanStepPayload,
  type PlanStepRequestOutcomeView,
  type SystemInfoSummary,
  type ToolExecutionView,
  type VoiceFeedPayload,
  type VoiceStatusView,
} from "../../../shared/ipc-contract";
import type { VoiceEventPayload } from "../../../shared/voice-engine";

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

export async function sendChatMessage(
  payload: ChatSendPayload,
): Promise<IpcResult<ChatExchange>> {
  return asIpcResult<ChatExchange>(BRIDGE_CONTEXT, await resolveBridge().sendChat(payload));
}

export async function fetchConversations(
  payload: ConversationListPayload = {},
): Promise<IpcResult<ConversationListView>> {
  return asIpcResult<ConversationListView>(
    BRIDGE_CONTEXT,
    await resolveBridge().listConversations(payload),
  );
}

export async function fetchConversation(
  payload: ConversationIdPayload,
): Promise<IpcResult<ConversationHistoryView>> {
  return asIpcResult<ConversationHistoryView>(
    BRIDGE_CONTEXT,
    await resolveBridge().getConversation(payload),
  );
}

export async function deleteConversationById(
  payload: ConversationIdPayload,
): Promise<IpcResult<ConversationDeletedView>> {
  return asIpcResult<ConversationDeletedView>(
    BRIDGE_CONTEXT,
    await resolveBridge().deleteConversation(payload),
  );
}

export async function fetchApprovals(
  payload: ApprovalListPayload = {},
): Promise<IpcResult<ApprovalListView>> {
  return asIpcResult<ApprovalListView>(BRIDGE_CONTEXT, await resolveBridge().listApprovals(payload));
}

export async function fetchApproval(
  payload: ApprovalIdPayload,
): Promise<IpcResult<ApprovalRequestView>> {
  return asIpcResult<ApprovalRequestView>(BRIDGE_CONTEXT, await resolveBridge().getApproval(payload));
}

export async function approveApprovalById(
  payload: ApprovalIdPayload,
): Promise<IpcResult<ToolExecutionView>> {
  return asIpcResult<ToolExecutionView>(
    BRIDGE_CONTEXT,
    await resolveBridge().approveApproval(payload),
  );
}

export async function rejectApprovalById(
  payload: ApprovalIdPayload,
): Promise<IpcResult<ApprovalRequestView>> {
  return asIpcResult<ApprovalRequestView>(
    BRIDGE_CONTEXT,
    await resolveBridge().rejectApproval(payload),
  );
}

export async function fetchPlans(
  payload: ApprovalListPayload = {},
): Promise<IpcResult<PlanListView>> {
  return asIpcResult<PlanListView>(BRIDGE_CONTEXT, await resolveBridge().listPlans(payload));
}

export async function fetchPlan(
  payload: PlanIdPayload,
): Promise<IpcResult<PlanDetailView>> {
  return asIpcResult<PlanDetailView>(BRIDGE_CONTEXT, await resolveBridge().getPlan(payload));
}

export async function requestPlanStepById(
  payload: PlanStepPayload,
): Promise<IpcResult<PlanStepRequestOutcomeView>> {
  return asIpcResult<PlanStepRequestOutcomeView>(
    BRIDGE_CONTEXT,
    await resolveBridge().requestPlanStep(payload),
  );
}

export async function resolvePlanRecoveryStep(
  payload: PlanStepPayload,
): Promise<IpcResult<PlanRecoveryResolvedView>> {
  return asIpcResult<PlanRecoveryResolvedView>(
    BRIDGE_CONTEXT,
    await resolveBridge().resolvePlanRecovery(payload),
  );
}

export async function voiceArm(): Promise<IpcResult<EmptyPayload>> {
  return asIpcResult<EmptyPayload>(BRIDGE_CONTEXT, await resolveBridge().voiceArm());
}

export async function voiceBegin(): Promise<IpcResult<EmptyPayload>> {
  return asIpcResult<EmptyPayload>(BRIDGE_CONTEXT, await resolveBridge().voiceBegin());
}

export async function voiceFeed(
  payload: VoiceFeedPayload,
): Promise<IpcResult<EmptyPayload>> {
  return asIpcResult<EmptyPayload>(BRIDGE_CONTEXT, await resolveBridge().voiceFeed(payload));
}

export async function voiceEnd(): Promise<IpcResult<EmptyPayload>> {
  return asIpcResult<EmptyPayload>(BRIDGE_CONTEXT, await resolveBridge().voiceEnd());
}

export async function voiceCancel(): Promise<IpcResult<EmptyPayload>> {
  return asIpcResult<EmptyPayload>(BRIDGE_CONTEXT, await resolveBridge().voiceCancel());
}

export async function prepareVoiceModels(): Promise<IpcResult<VoiceStatusView>> {
  return asIpcResult<VoiceStatusView>(
    BRIDGE_CONTEXT,
    await resolveBridge().voicePrepareModels(),
  );
}

export function subscribeVoiceEvents(
  listener: (event: VoiceEventPayload) => void,
): () => void {
  return resolveBridge().onVoiceEvent(listener);
}
