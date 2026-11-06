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
import { ipcError, ipcOk, PRELOAD_METHODS, type ApprovalIdPayload, type BackendStatus, type ChatSendPayload, type ConversationIdPayload, type ConversationListPayload, type EmptyPayload, type IpcResult, type PlanIdPayload, type PlanStepPayload, type TtsSpeakPayload, type VoiceFeedPayload, type VoiceStatusView } from "../../../shared/ipc-contract";
import { pcm16BytesToFloat32 } from "../../../shared/voice-audio";
import { VOICE_ERROR_COPY } from "../../../shared/voice-engine";
import { shouldAllowNavigation } from "../app-security";
import type { VoiceEngine } from "../voice/voice-engine";
import { createMicSequenceGate } from "../voice/mic-sequence-gate";

export type IpcHandlerDeps = {
  client: HttpBackendApiClient;
  getStatus: () => BackendStatus;
  /** Authorized caller origins (dev server / dist index). */
  navigationTrust: { devServerUrl: string | null; isDistHosting: boolean };
  /**
   * Real voice engine (Phase 6.4-b). Optional so tests and builds without
   * the native voice stack keep the honest, fail-closed stub rows.
   */
  voice?: VoiceEngine;
};

function callerUrl(event: IpcMainInvokeEvent): string | null {
  try {
    return event.senderFrame?.url ?? null;
  } catch {
    return null;
  }
}

/** Map a VoiceEngineOutcome onto the command channels' EmptyPayload result. */
function voiceOutcomeToIpc(outcome: Awaited<ReturnType<VoiceEngine["arm"]>>): IpcResult<EmptyPayload> {
  return outcome.ok ? ipcOk({}) : ipcError(outcome.code, outcome.message);
}

export function createJarvisIpcRegistry(deps: IpcHandlerDeps): IpcRegistry {
  const micSequenceGate = createMicSequenceGate();
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
    // Phase 6.4-b voice commands. With an injected engine every row maps
    // through the real state machine; without one the rows still fail
    // CLOSED with the honest ENGINE_UNAVAILABLE copy — no stub audio, no
    // synthetic recognition, no fake status rows.
    "jarvis:voice-status": async (): Promise<IpcResult<VoiceStatusView>> => {
      if (!deps.voice) {
        return ipcError("ENGINE_UNAVAILABLE", VOICE_ERROR_COPY["ENGINE_UNAVAILABLE"]);
      }
      return ipcOk(deps.voice.status());
    },
    "jarvis:voice-prepare-models": async (): Promise<IpcResult<VoiceStatusView>> => {
      if (!deps.voice) {
        return ipcError("ENGINE_UNAVAILABLE", VOICE_ERROR_COPY["ENGINE_UNAVAILABLE"]);
      }
      const prepared = await deps.voice.prepareModels();
      if (prepared.ok) return ipcOk(deps.voice.status());
      return ipcError(prepared.code, prepared.message);
    },
    "jarvis:voice-arm": async () => {
      if (!deps.voice) {
        return ipcError("ENGINE_UNAVAILABLE", VOICE_ERROR_COPY["ENGINE_UNAVAILABLE"]);
      }
      const armed = await deps.voice.arm();
      if (armed.ok) micSequenceGate.reset();
      return voiceOutcomeToIpc(armed);
    },
    "jarvis:voice-begin": async () => {
      if (!deps.voice) {
        return ipcError("ENGINE_UNAVAILABLE", VOICE_ERROR_COPY["ENGINE_UNAVAILABLE"]);
      }
      const begun = await deps.voice.begin();
      if (begun.ok) micSequenceGate.reset();
      return voiceOutcomeToIpc(begun);
    },
    "jarvis:voice-end": async () => {
      if (!deps.voice) {
        return ipcError("ENGINE_UNAVAILABLE", VOICE_ERROR_COPY["ENGINE_UNAVAILABLE"]);
      }
      // The transcript itself flows through the voice-events channel; the
      // invoke result carries only the state change acknowledgement.
      const ended = await deps.voice.end();
      return ended.ok ? ipcOk({}) : ipcError(ended.code, ended.message);
    },
    "jarvis:voice-cancel": async () => {
      if (!deps.voice) {
        return ipcError("ENGINE_UNAVAILABLE", VOICE_ERROR_COPY["ENGINE_UNAVAILABLE"]);
      }
      return voiceOutcomeToIpc(deps.voice.cancel());
    },
    "jarvis:voice-feed": async (payload: VoiceFeedPayload) => {
      if (!deps.voice) {
        return ipcError("ENGINE_UNAVAILABLE", VOICE_ERROR_COPY["ENGINE_UNAVAILABLE"]);
      }
      const sequence = micSequenceGate.accept(payload.sequence);
      if (!sequence.accepted) {
        // Replays are idempotent: acknowledge the duplicate without
        // feeding the recognizer a second time.
        return ipcOk({});
      }
      // The channel validator has already bounded the chunk (PCM16 LE,
      // even byte count, ≤ VOICE_PCM_CHUNK_MAX_BYTES; sequence legality
      // is enforced by the engine state machine guards). Decode to the
      // engine's native mono float32 at the fixed 16 kHz rate.
      return voiceOutcomeToIpc(deps.voice.feed(pcm16BytesToFloat32(payload.pcm)));
    },
    "jarvis:tts-speak": async (payload: TtsSpeakPayload) => {
      if (!deps.voice) {
        return ipcError("ENGINE_UNAVAILABLE", VOICE_ERROR_COPY["ENGINE_UNAVAILABLE"]);
      }
      return voiceOutcomeToIpc(await deps.voice.speak(payload.text));
    },
    "jarvis:tts-stop": async () => {
      if (!deps.voice) {
        return ipcError("ENGINE_UNAVAILABLE", VOICE_ERROR_COPY["ENGINE_UNAVAILABLE"]);
      }
      return voiceOutcomeToIpc(deps.voice.stop());
    },
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
