/**
 * Single source of truth for the renderer → main IPC surface (Phase 6.2).
 *
 * This module is compiled into the Electron main bundle, the preload
 * bundle, the renderer bundle, and the contract tests, so both sides of
 * every channel can never drift apart. The channel/method/payload tables
 * are real data used by the preload bridge and the main-process registry.
 *
 * Rules baked in here (see desktop/README.md § security):
 * - Every channel name is namespaced `jarvis:` and explicitly listed.
 * - The preload bridge exposes ONLY the methods in PRELOAD_METHODS —
 *   never raw ipcRenderer, never Node.
 * - Renderer network access does not exist; every backend call happens
 *   in the main process with auth attached there.
 * - Every channel declares a payload validator; the registry rejects
 *   anything the validator does not accept (BAD_PAYLOAD) before a
 *   handler ever runs.
 */

/** All IPC channel names start with this prefix. */
export const IPC_CHANNEL_PREFIX = "jarvis:";

/**
 * Type-only import (erased at compile time — `shared/voice-engine.ts`
 * imports runtime values from here, so a runtime edge would be a cycle).
 */
import type { VoiceEventPayload } from "./voice-engine";

/** Explicit, exhaustive channel registry. Add new channels here only. */
export const IPC_CHANNELS = {
  backendStatus: "jarvis:backend-status",
  checkHealth: "jarvis:check-health",
  getSystemInfo: "jarvis:get-system-info",
  sendChat: "jarvis:send-chat",
  listConversations: "jarvis:list-conversations",
  getConversation: "jarvis:get-conversation",
  deleteConversation: "jarvis:delete-conversation",
  listApprovals: "jarvis:list-approvals",
  getApproval: "jarvis:get-approval",
  approveApproval: "jarvis:approve-approval",
  rejectApproval: "jarvis:reject-approval",
  listPlans: "jarvis:list-plans",
  getPlan: "jarvis:get-plan",
  requestPlanStep: "jarvis:request-plan-step",
  resolvePlanRecovery: "jarvis:resolve-plan-recovery",
  // Phase 6.4 voice commands (renderer → main). Handlers arrive with the
  // speech runtime phase (6.4-b); until then every row returns the honest
  // ENGINE_UNAVAILABLE failure — no stub audio, no synthetic recognition.
  voiceStatus: "jarvis:voice-status",
  voicePrepareModels: "jarvis:voice-prepare-models",
  voiceArm: "jarvis:voice-arm",
  voiceBegin: "jarvis:voice-begin",
  voiceEnd: "jarvis:voice-end",
  voiceCancel: "jarvis:voice-cancel",
  ttsSpeak: "jarvis:tts-speak",
  ttsStop: "jarvis:tts-stop",
  // Phase 6.4-c microphone feed: renderer audio-capture thread → main
  // process → voice engine. Bounded mono PCM16 at the fixed 16 kHz rate.
  voiceFeed: "jarvis:voice-feed",
} as const;

/**
 * The single MAIN → RENDERER voice event channel (recognition partials,
 * TTS audio, state changes). It is deliberately NOT in IPC_CHANNELS:
 * events are never dispatched renderer→main, so the registry must not
 * advertise them. The payload contract for this channel lives in
 * `shared/voice-engine.ts` (validateVoiceEventPayload).
 */
export const VOICE_EVENTS_CHANNEL = "jarvis:voice-events";

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
  | "UNEXPECTED"
  /** Voice commands before the speech runtime exists (Phase 6.4-b). */
  | "ENGINE_UNAVAILABLE"
  /** A voice command issued outside its required state (Phase 6.4-b). */
  | "VOICE_STATE"
  /** Voice model files are not installed (Phase 6.4-b; user must prepare). */
  | "MODEL_MISSING"
  /** Local speech recognition failed honestly (no fake transcript). */
  | "STT_FAILURE"
  /** Local synthesis failed honestly (no synthetic audio). */
  | "TTS_FAILURE"
  /** The user cancelled the active capture/synthesis. */
  | "RECORDING_CANCELLED"
  /** The renderer could not open/resume an audio output device. */
  | "AUDIO_OUTPUT_FAILURE"
  /** Microphone permission was denied (explicit, never assumed). */
  | "MICROPHONE_DENIED";

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
/* Payload map: one validated payload type per channel                 */
/* ------------------------------------------------------------------ */

/** All 6.1 channels take a canonical empty payload ({}). */
export type EmptyPayload = Record<string, never>;

/** `jarvis:send-chat` payload. `conversationId` is null for a NEW conversation. */
export type ChatSendPayload = {
  message: string;
  conversationId: string | null;
};

/** `jarvis:list-conversations` payload (offset pagination into backend ordering). */
export type ConversationListPayload = {
  limit?: number;
  offset?: number;
};

/** `jarvis:get-conversation` / `jarvis:delete-conversation` payload. */
export type ConversationIdPayload = {
  conversationId: string;
};

/** Payload type shared by every backend metadata listing (approvals, plans). */
export interface BoundedPagePayload {
  limit?: number;
  offset?: number;
}

/** `jarvis:list-approvals` payload (same backend page bounds 1..200). */
export type ApprovalListPayload = BoundedPagePayload;

/** `jarvis:list-plans` payload (same backend page bounds 1..200). */
export type PlanListPayload = BoundedPagePayload;

/** `jarvis:get-approval`, `jarvis:approve-approval`, `jarvis:reject-approval`. */
export type ApprovalIdPayload = {
  approvalId: string;
};

/** `jarvis:get-plan` payload. */
export type PlanIdPayload = {
  planId: string;
};

/** `jarvis:request-plan-step` / `jarvis:resolve-plan-recovery` payload. */
export type PlanStepPayload = {
  planId: string;
  stepId: string;
};

/**
 * Voice channel payloads (Phase 6.4-a contracts; the engine that honors
 * them arrives in 6.4-b). All voice command channels take the canonical
 * empty payload except `jarvis:tts-speak`.
 */
export type VoiceCommandPayload = EmptyPayload;

/** `jarvis:tts-speak` payload — the ONLY voice command carrying text. */
export type TtsSpeakPayload = {
  text: string;
  /** Renderer-owned identity used to reject stale synthesis events. */
  requestId: number;
};

/** Bound for TTS text, transcripts (interim + final), and failure notes. */
export const VOICE_TEXT_MAX_LENGTH = 2000;

/** Bounded failure copy carried by voice events. */
export const VOICE_FAILURE_MESSAGE_MAX_LENGTH = 512;

/** Fixed capture (STT input) sample rate: 16 kHz mono PCM16 frames. */
export const VOICE_PCM_SAMPLE_RATE = 16_000;

/**
 * One streamed TTS chunk is bounded independently of its model-reported
 * sample rate; at 24 kHz * 2 bytes, the ceiling admits ~1.3 s per chunk
 * while keeping any single IPC message bounded.
 */
export const VOICE_PCM_CHUNK_MAX_BYTES = 65_536;

/** Upper bound on streamed TTS chunk sequence numbers. */
export const VOICE_SEQUENCE_MAX = 1_000_000;

/** Upper bound on a renderer-owned TTS playback request identity. */
export const VOICE_PLAYBACK_REQUEST_ID_MAX = VOICE_SEQUENCE_MAX;

/** TTS transport format: mono signed PCM16 little-endian. */
export const VOICE_TTS_CHANNELS = 1 as const;
export const VOICE_TTS_ENCODING = "pcm16le" as const;

/** Bounded full-buffer playback ceiling: 180 seconds at the model's 24 kHz rate. */
export const VOICE_TTS_MAX_SAMPLES = 24_000 * 180;

/** Validate `jarvis:tts-speak` (exact keys, trimmed text, bounded identity). */
export function validateTtsSpeakPayload(
  value: unknown,
): PayloadValidation<TtsSpeakPayload> {
  if (!isPlainObject(value)) {
    return { ok: false, reason: "TTS payload must be an object." };
  }
  const keys = Object.keys(value).sort();
  if (keys.length !== 2 || keys[0] !== "requestId" || keys[1] !== "text") {
    return { ok: false, reason: "TTS payload accepts exactly requestId and text." };
  }
  const text = (value as { text: unknown }).text;
  const requestId = (value as { requestId: unknown }).requestId;
  if (typeof text !== "string") {
    return { ok: false, reason: "TTS text must be a string." };
  }
  const trimmed = text.trim();
  if (trimmed.length === 0) {
    return { ok: false, reason: "TTS text must not be empty." };
  }
  if (text.length > VOICE_TEXT_MAX_LENGTH) {
    return {
      ok: false,
      reason: `TTS text exceeds the ${VOICE_TEXT_MAX_LENGTH} character limit.`,
    };
  }
  if (
    typeof requestId !== "number" ||
    !Number.isInteger(requestId) ||
    requestId < 0 ||
    requestId > VOICE_PLAYBACK_REQUEST_ID_MAX
  ) {
    return { ok: false, reason: "TTS requestId is out of bounds." };
  }
  return { ok: true, value: { text: trimmed, requestId } };
}

/* ------------------------- validation contract --------------------- */

/** Backend-bound message size: mirrors ChatRequest.message.maxLength. */
export const CHAT_MESSAGE_MAX_LENGTH = 4000;

/**
 * Conversation identifiers originate from the backend (`str(uuid4())`).
 * The strict pattern keeps arbitrary strings out of request URL paths.
 */
export const CONVERSATION_ID_PATTERN =
  /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

/** Backend policy: page size 1..200 (DEFAULT_LIST_LIMIT 50, MAX 200) —
 *  identical bounds in every backend metadata listing endpoint. */
export const CONVERSATION_LIST_LIMITS = { default: 50, min: 1, max: 200 } as const;

/** Approval identifiers are also `str(uuid4())` (backend/app/services/approval_store.py). */
export const APPROVAL_ID_PATTERN = CONVERSATION_ID_PATTERN;

/** Backend PlanDetail.plan_id pattern (`^[a-zA-Z0-9_-]{1,64}$`). */
export const PLAN_ID_PATTERN = /^[a-zA-Z0-9_-]{1,64}$/;

/** Backend PlanStep.step_id pattern (`^[a-zA-Z0-9_]{1,32}$`). */
export const PLAN_STEP_ID_PATTERN = /^[a-zA-Z0-9_]{1,32}$/;

export type PayloadValidation<IpcPayloadT> =
  | { ok: true; value: IpcPayloadT }
  | { ok: false; reason: string };

/** True for `undefined` or a plain, empty, non-array object. */
export function isEmptyPayload(value: unknown): value is EmptyPayload {
  if (value === undefined) return true;
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  return Object.keys(value).length === 0;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value);
}

/** Validate `jarvis:send-chat`. Message is trimmed here (wire contract). */
export function validateChatSendPayload(
  value: unknown,
): PayloadValidation<ChatSendPayload> {
  if (!isPlainObject(value)) return { ok: false, reason: "Chat payload must be an object." };
  const keys = Object.keys(value).sort();
  if (keys.length !== 2 || keys[0] !== "conversationId" || keys[1] !== "message") {
    return { ok: false, reason: "Chat payload accepts exactly message and conversationId." };
  }
  if (typeof value["message"] !== "string") {
    return { ok: false, reason: "Chat message must be a string." };
  }
  const message = value["message"].trim();
  if (message.length === 0) {
    return { ok: false, reason: "Chat message must not be empty." };
  }
  if (message.length > CHAT_MESSAGE_MAX_LENGTH) {
    return {
      ok: false,
      reason: `Chat message exceeds the ${CHAT_MESSAGE_MAX_LENGTH} character limit.`,
    };
  }
  const conversationId = value["conversationId"];
  if (conversationId !== null) {
    if (typeof conversationId !== "string" || !CONVERSATION_ID_PATTERN.test(conversationId)) {
      return { ok: false, reason: "conversationId must be a backend conversation uuid or null." };
    }
  }
  return { ok: true, value: { message, conversationId } };
}

/** Shared page validator for every backend metadata listing (1..200). */
function validateBoundedPage(
  value: unknown,
): PayloadValidation<{ limit?: number; offset?: number }> {
  if (value === undefined || isEmptyPayload(value)) return { ok: true, value: {} };
  if (!isPlainObject(value)) {
    return { ok: false, reason: "Listing payload must be an object." };
  }
  const { limit, offset, ...extra } = value;
  if (Object.keys(extra).length > 0) {
    return { ok: false, reason: "Listing payload accepts limit and offset only." };
  }
  if (limit !== undefined) {
    if (
      !isInteger(limit) ||
      limit < CONVERSATION_LIST_LIMITS.min ||
      limit > CONVERSATION_LIST_LIMITS.max
    ) {
      return {
        ok: false,
        reason: `limit must be an integer between ${CONVERSATION_LIST_LIMITS.min} and ${CONVERSATION_LIST_LIMITS.max}.`,
      };
    }
  }
  if (offset !== undefined) {
    if (!isInteger(offset) || offset < 0 || offset > Number.MAX_SAFE_INTEGER) {
      return { ok: false, reason: "offset must be a non-negative integer." };
    }
  }
  return { ok: true, value: { limit, offset } };
}

/** Validate `jarvis:list-conversations` (optional bounded pagination). */
export function validateConversationListPayload(
  value: unknown,
): PayloadValidation<ConversationListPayload> {
  return validateBoundedPage(value);
}

/** Validate `jarvis:list-approvals` (same backend page bounds). */
export function validateApprovalListPayload(
  value: unknown,
): PayloadValidation<ApprovalListPayload> {
  return validateBoundedPage(value);
}

/** Validate `jarvis:list-plans` (same backend page bounds). */
export function validatePlanListPayload(
  value: unknown,
): PayloadValidation<PlanListPayload> {
  return validateBoundedPage(value);
}

/** Validate `jarvis:get-conversation` / `jarvis:delete-conversation`. */
export function validateConversationIdPayload(
  value: unknown,
): PayloadValidation<ConversationIdPayload> {
  if (!isPlainObject(value)) {
    return { ok: false, reason: "Conversation payload must be an object." };
  }
  const keys = Object.keys(value);
  if (keys.length !== 1 || keys[0] !== "conversationId") {
    return { ok: false, reason: "Conversation payload accepts exactly conversationId." };
  }
  const conversationId = value["conversationId"];
  if (typeof conversationId !== "string" || !CONVERSATION_ID_PATTERN.test(conversationId)) {
    return { ok: false, reason: "conversationId must be a backend conversation uuid." };
  }
  return { ok: true, value: { conversationId } };
}

/** Validate `jarvis:get-approval` / `jarvis:approve-approval` / `jarvis:reject-approval`. */
export function validateApprovalIdPayload(
  value: unknown,
): PayloadValidation<ApprovalIdPayload> {
  if (!isPlainObject(value)) {
    return { ok: false, reason: "Approval payload must be an object." };
  }
  const keys = Object.keys(value);
  if (keys.length !== 1 || keys[0] !== "approvalId") {
    return { ok: false, reason: "Approval payload accepts exactly approvalId." };
  }
  const approvalId = value["approvalId"];
  if (typeof approvalId !== "string" || !APPROVAL_ID_PATTERN.test(approvalId)) {
    return { ok: false, reason: "approvalId must be a backend approval uuid." };
  }
  return { ok: true, value: { approvalId } };
}

/** Validate `jarvis:get-plan`. */
export function validatePlanIdPayload(
  value: unknown,
): PayloadValidation<PlanIdPayload> {
  if (!isPlainObject(value)) {
    return { ok: false, reason: "Plan payload must be an object." };
  }
  const keys = Object.keys(value);
  if (keys.length !== 1 || keys[0] !== "planId") {
    return { ok: false, reason: "Plan payload accepts exactly planId." };
  }
  const planId = value["planId"];
  if (typeof planId !== "string" || !PLAN_ID_PATTERN.test(planId)) {
    return { ok: false, reason: "planId must match the backend plan identifier pattern." };
  }
  return { ok: true, value: { planId } };
}

/** Validate `jarvis:request-plan-step` / `jarvis:resolve-plan-recovery`. */
export function validatePlanStepPayload(
  value: unknown,
): PayloadValidation<PlanStepPayload> {
  if (!isPlainObject(value)) {
    return { ok: false, reason: "Plan step payload must be an object." };
  }
  const keys = Object.keys(value).sort();
  if (keys.length !== 2 || keys[0] !== "planId" || keys[1] !== "stepId") {
    return { ok: false, reason: "Plan step payload accepts exactly planId and stepId." };
  }
  const planId = value["planId"];
  if (typeof planId !== "string" || !PLAN_ID_PATTERN.test(planId)) {
    return { ok: false, reason: "planId must match the backend plan identifier pattern." };
  }
  const stepId = value["stepId"];
  if (typeof stepId !== "string" || !PLAN_STEP_ID_PATTERN.test(stepId)) {
    return { ok: false, reason: "stepId must match the backend step identifier pattern." };
  }
  return { ok: true, value: { planId, stepId } };
}

/** Per-channel payload validators; the registry consumes this table. */
export type IpcPayloadMap = {
  "jarvis:backend-status": EmptyPayload;
  "jarvis:check-health": EmptyPayload;
  "jarvis:get-system-info": EmptyPayload;
  "jarvis:send-chat": ChatSendPayload;
  "jarvis:list-conversations": ConversationListPayload;
  "jarvis:get-conversation": ConversationIdPayload;
  "jarvis:delete-conversation": ConversationIdPayload;
  "jarvis:list-approvals": ApprovalListPayload;
  "jarvis:get-approval": ApprovalIdPayload;
  "jarvis:approve-approval": ApprovalIdPayload;
  "jarvis:reject-approval": ApprovalIdPayload;
  "jarvis:list-plans": PlanListPayload;
  "jarvis:get-plan": PlanIdPayload;
  "jarvis:request-plan-step": PlanStepPayload;
  "jarvis:resolve-plan-recovery": PlanStepPayload;
  "jarvis:voice-status": VoiceCommandPayload;
  "jarvis:voice-prepare-models": VoiceCommandPayload;
  "jarvis:voice-arm": VoiceCommandPayload;
  "jarvis:voice-begin": VoiceCommandPayload;
  "jarvis:voice-end": VoiceCommandPayload;
  "jarvis:voice-cancel": VoiceCommandPayload;
  "jarvis:voice-feed": VoiceFeedPayload;
  "jarvis:tts-speak": TtsSpeakPayload;
  "jarvis:tts-stop": VoiceCommandPayload;
};

export type PayloadValidator<K extends keyof IpcPayloadMap> = (
  value: unknown,
) => PayloadValidation<IpcPayloadMap[K]>;

function validateEmptyPayload(value: unknown): PayloadValidation<EmptyPayload> {
  return isEmptyPayload(value)
    ? { ok: true, value: {} }
    : { ok: false, reason: "This channel accepts an empty payload only." };
}

/**
 * `jarvis:voice-feed` payload (Phase 6.4-c). One bounded chunk of
 * microphone audio: mono PCM16, little-endian, at the fixed
 * VOICE_PCM_SAMPLE_RATE (16 kHz). The chunk itself never carries a
 * sample-rate field — the rate is a shared constant, so a lying rate is
 * impossible by construction. `sequence` strictly increases across the
 * capture session starting at 0; duplicates/replays are dropped by the
 * main-process gate (never fed to the recognizer twice).
 */
export type VoiceFeedPayload = {
  sequence: number;
  pcm: ArrayBuffer;
};

/**
 * Renderer capture framing: 3 200 PCM16 samples = 200 ms of 16 kHz
 * speech per IPC message (6 400 bytes) — batching well above the
 * per-frame drizzle while keeping end-of-utterance flush latency low.
 */
export const VOICE_MIC_CHUNK_SAMPLES = 3_200;

/** Validate `jarvis:voice-feed` (exact key set, bounded PCM16). */
export function validateVoiceFeedPayload(
  value: unknown,
): PayloadValidation<VoiceFeedPayload> {
  if (!isPlainObject(value)) {
    return { ok: false, reason: "Voice feed payload must be an object." };
  }
  const keys = Object.keys(value).sort();
  if (keys.length !== 2 || keys[0] !== "pcm" || keys[1] !== "sequence") {
    return { ok: false, reason: "Voice feed payload accepts exactly pcm and sequence." };
  }
  const sequence = (value as { sequence: unknown }).sequence;
  if (
    typeof sequence !== "number" ||
    !Number.isInteger(sequence) ||
    sequence < 0 ||
    sequence > VOICE_SEQUENCE_MAX
  ) {
    return { ok: false, reason: "Voice feed sequence is out of bounds." };
  }
  const pcm = (value as { pcm: unknown }).pcm;
  if (!(pcm instanceof ArrayBuffer)) {
    return { ok: false, reason: "Voice feed pcm must be an ArrayBuffer of PCM16 bytes." };
  }
  if (
    pcm.byteLength === 0 ||
    pcm.byteLength % 2 !== 0 ||
    pcm.byteLength > VOICE_PCM_CHUNK_MAX_BYTES
  ) {
    return {
      ok: false,
      reason: `Voice feed pcm must be 2…${VOICE_PCM_CHUNK_MAX_BYTES} bytes (even count).`,
    };
  }
  return { ok: true, value: { sequence, pcm } };
}

export const PAYLOAD_VALIDATORS: {
  [K in keyof IpcPayloadMap]: PayloadValidator<K>;
} = {
  "jarvis:backend-status": validateEmptyPayload,
  "jarvis:check-health": validateEmptyPayload,
  "jarvis:get-system-info": validateEmptyPayload,
  "jarvis:send-chat": validateChatSendPayload,
  "jarvis:list-conversations": validateConversationListPayload,
  "jarvis:get-conversation": validateConversationIdPayload,
  "jarvis:delete-conversation": validateConversationIdPayload,
  "jarvis:list-approvals": validateApprovalListPayload,
  "jarvis:get-approval": validateApprovalIdPayload,
  "jarvis:approve-approval": validateApprovalIdPayload,
  "jarvis:reject-approval": validateApprovalIdPayload,
  "jarvis:list-plans": validatePlanListPayload,
  "jarvis:get-plan": validatePlanIdPayload,
  "jarvis:request-plan-step": validatePlanStepPayload,
  "jarvis:resolve-plan-recovery": validatePlanStepPayload,
  "jarvis:voice-status": validateEmptyPayload,
  "jarvis:voice-prepare-models": validateEmptyPayload,
  "jarvis:voice-arm": validateEmptyPayload,
  "jarvis:voice-begin": validateEmptyPayload,
  "jarvis:voice-end": validateEmptyPayload,
  "jarvis:voice-cancel": validateEmptyPayload,
  "jarvis:voice-feed": validateVoiceFeedPayload,
  "jarvis:tts-speak": validateTtsSpeakPayload,
  "jarvis:tts-stop": validateEmptyPayload,
};

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
 * electron/main/api/schema.gen.ts; the renderer only ever sees this
 * narrowed, UI-facing projection.
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
/* Chat + conversation projections (backend/app/schemas/chat.py /      */
/* conversation.py, camel-Cased for the renderer)                      */
/* ------------------------------------------------------------------ */

export type ChatExchangeStatus = "success" | "action_proposed" | "plan_proposed";

export type PlanStepStatusView =
  | "PENDING"
  | "AWAITING_APPROVAL"
  | "RECOVERY_REQUIRED"
  | "COMPLETED"
  | "EXECUTED"
  | "FAILED";

export type RiskLevelView = "READ_ONLY" | "LOW_RISK" | "HIGH_RISK";

export type ApprovalStatusView =
  | "PENDING"
  | "APPROVED"
  | "REJECTED"
  | "EXPIRED"
  | "EXECUTED";

/** Backend ProposedAction, narrowed. Arguments are capped at 32 entries. */
export type ProposedActionView = {
  approvalId: string;
  toolName: string;
  /** Bounded arguments map (≤32 entries) exactly as the backend bound them. */
  arguments: Record<string, unknown>;
  description: string;
  riskLevel: RiskLevelView;
  approvalStatus: ApprovalStatusView;
  expiresAt: string;
};

/** Backend ExecutedAction (tool_result), narrowed. */
export type ExecutedActionView = {
  toolName: string;
  status: "success" | "already_exists" | "failure";
  success: boolean;
  message: string;
};

export type PlanStepView = {
  stepId: string;
  description: string;
  toolName: string;
  status: PlanStepStatusView;
  dependsOn: string[];
  approvalId: string | null;
};

export type PlanProposalView = {
  planId: string;
  goal: string;
  steps: PlanStepView[];
};

/**
 * POST /api/chat result for the UI. `reply` is the assistant text;
 * `proposal*` carry the structured payloads for action/plan proposals.
 */
export type ChatExchange = {
  conversationId: string;
  status: ChatExchangeStatus;
  reply: string;
  /** Backend timestamp, or the main process clock when the backend omits it. */
  occurredAt: string;
  action: ProposedActionView | null;
  toolResult: ExecutedActionView | null;
  plan: PlanProposalView | null;
};

/** Backend ConversationTurn, narrowed. */
export type ConversationTurnView = {
  role: "user" | "assistant";
  content: string;
  timestamp: string;
};

/** GET /api/conversations/{id} for the UI (windowed, still chronological). */
export type ConversationHistoryView = {
  conversationId: string;
  turns: ConversationTurnView[];
  turnCount: number;
  totalTurnCount: number;
};

/** Backend ConversationSummary (metadata only, never message content). */
export type ConversationSummaryView = {
  conversationId: string;
  createdAt: string;
  updatedAt: string;
  turnCount: number;
};

/** GET /api/conversations for the UI. */
export type ConversationListView = {
  conversations: ConversationSummaryView[];
  total: number;
};

/** DELETE /api/conversations/{id} for the UI. */
export type ConversationDeletedView = {
  conversationId: string;
};

/* ------------------------------------------------------------------ */
/* Approvals (backend/app/schemas/approval.py + tool.py ToolResult)     */
/* ------------------------------------------------------------------ */

/** Full ToolResult projection for the workspace (extends the chat-side ExecutedActionView). */
export type ToolExecutionView = {
  toolName: string;
  status: "success" | "already_exists" | "failure";
  success: boolean;
  message: string;
  /** Bounded string map (≤32 items) exactly as the backend returned it. */
  data: Record<string, string> | null;
  /** Stable, non-sensitive error classification or null. */
  errorCode: string | null;
};

/** GET /api/approvals row (metadata only — never includes arguments). */
export type ApprovalSummaryView = {
  approvalId: string;
  toolName: string;
  description: string;
  riskLevel: RiskLevelView;
  status: ApprovalStatusView;
  createdAt: string;
  expiresAt: string;
  resolvedAt: string | null;
};

/** GET /api/approvals/{id} — authoritative read including exact arguments. */
export type ApprovalRequestView = ApprovalSummaryView & {
  /** Exact validated arguments this approval is bound to (≤32 string entries). */
  arguments: Record<string, string>;
};

/** GET /api/approvals for the UI. */
export type ApprovalListView = {
  approvals: ApprovalSummaryView[];
  total: number;
};

/* ------------------------------------------------------------------ */
/* Plans (backend/app/schemas/plan.py)                                  */
/* ------------------------------------------------------------------ */

export type PlanOverallStatusView =
  | "recovery_required"
  | "awaiting_approval"
  | "actionable"
  | "failed"
  | "in_progress"
  | "completed";

/** GET /api/plans row — metadata with attention flags (Phase 5.2-a). */
export type PlanSummaryView = {
  planId: string;
  goal: string;
  createdAt: string;
  updatedAt: string;
  stepCount: number;
  /** Recorded step count per status key (bounded by the 8-step ceiling). */
  statusCounts: Record<string, number>;
  recoveryRequired: string[];
  awaitingApproval: string[];
  /** Backend derivation: true while any step is RECOVERY_REQUIRED or AWAITING_APPROVAL. */
  needsAttention: boolean;
};

/** One authored plan step inside the authoritative PlanDetail. */
export type PlanDetailStepView = {
  stepId: string;
  description: string;
  toolName: string;
  arguments: Record<string, string>;
  dependsOn: string[];
  status: PlanStepStatusView;
  /** The exact single-use approval bound to this step, if one was created. */
  approvalId: string | null;
  /** Recorded execution result, once the step ran (or FAILED resolved). */
  result: ToolExecutionView | null;
};

/** GET /api/plans/{id} — authoritative read with backend-derived status. */
export type PlanDetailView = {
  planId: string;
  goal: string;
  conversationId: string | null;
  createdAt: string;
  overallStatus: PlanOverallStatusView;
  steps: PlanDetailStepView[];
  /** Backend derivation — PENDING steps whose dependencies have recorded results. */
  readyStepIds: string[];
  recoveryRequired: string[];
  awaitingApproval: string[];
  needsAttention: boolean;
};

/** GET /api/plans for the UI. */
export type PlanListView = {
  plans: PlanSummaryView[];
  total: number;
};

/**
 * `jarvis:voice-status` result (Phase 6.4). Everything is a bounded,
 * boolean-ish availability probe — never paths or model internals.
 */
export type VoiceStatusView = {
  /** Speech runtime loaded and responsive. */
  engineAvailable: boolean;
  /** Speech-to-text model files present and verified. */
  sttModelPresent: boolean;
  /** Text-to-speech model files present and verified. */
  ttsModelPresent: boolean;
  /** Voice-activity-detection model present (optional tier). */
  vadModelPresent: boolean;
  /** Bounded human note (e.g. which phase/tooling is missing) — no paths. */
  message: string;
};

/** POST .../steps/{step}/request outcome, discriminated exactly like the API (200|202). */
export type PlanStepRequestOutcomeView =
  | { kind: "executed"; execution: ToolExecutionView }
  /** 202: a mutating step created a pending single-use approval. Nothing ran. */
  | { kind: "approval_required"; approval: ApprovalRequestView };

/** POST .../steps/{step}/resolve-recovery — fresh TaskPlan after reconciliation. */
export type PlanRecoveryResolvedView = {
  planId: string;
  goal: string;
  conversationId: string | null;
  createdAt: string;
  steps: PlanDetailStepView[];
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
  { method: "sendChat", channel: IPC_CHANNELS.sendChat },
  { method: "listConversations", channel: IPC_CHANNELS.listConversations },
  { method: "getConversation", channel: IPC_CHANNELS.getConversation },
  { method: "deleteConversation", channel: IPC_CHANNELS.deleteConversation },
  { method: "listApprovals", channel: IPC_CHANNELS.listApprovals },
  { method: "getApproval", channel: IPC_CHANNELS.getApproval },
  { method: "approveApproval", channel: IPC_CHANNELS.approveApproval },
  { method: "rejectApproval", channel: IPC_CHANNELS.rejectApproval },
  { method: "listPlans", channel: IPC_CHANNELS.listPlans },
  { method: "getPlan", channel: IPC_CHANNELS.getPlan },
  { method: "requestPlanStep", channel: IPC_CHANNELS.requestPlanStep },
  { method: "resolvePlanRecovery", channel: IPC_CHANNELS.resolvePlanRecovery },
  { method: "voiceStatus", channel: IPC_CHANNELS.voiceStatus },
  { method: "voicePrepareModels", channel: IPC_CHANNELS.voicePrepareModels },
  { method: "voiceArm", channel: IPC_CHANNELS.voiceArm },
  { method: "voiceBegin", channel: IPC_CHANNELS.voiceBegin },
  { method: "voiceEnd", channel: IPC_CHANNELS.voiceEnd },
  { method: "voiceCancel", channel: IPC_CHANNELS.voiceCancel },
  { method: "voiceFeed", channel: IPC_CHANNELS.voiceFeed },
  { method: "ttsSpeak", channel: IPC_CHANNELS.ttsSpeak },
  { method: "ttsStop", channel: IPC_CHANNELS.ttsStop },
] as const;

export type PreloadMethodName = (typeof PRELOAD_METHODS)[number]["method"];

/** The object exposed on `window.jarvis`. */
export type JarvisApi = {
  backendStatus(): Promise<IpcResult<BackendStatus>>;
  checkHealth(): Promise<IpcResult<HealthCheck>>;
  getSystemInfo(): Promise<IpcResult<SystemInfoSummary>>;
  sendChat(payload: ChatSendPayload): Promise<IpcResult<ChatExchange>>;
  listConversations(payload?: ConversationListPayload): Promise<IpcResult<ConversationListView>>;
  getConversation(payload: ConversationIdPayload): Promise<IpcResult<ConversationHistoryView>>;
  deleteConversation(payload: ConversationIdPayload): Promise<IpcResult<ConversationDeletedView>>;
  listApprovals(payload?: ApprovalListPayload): Promise<IpcResult<ApprovalListView>>;
  getApproval(payload: ApprovalIdPayload): Promise<IpcResult<ApprovalRequestView>>;
  approveApproval(payload: ApprovalIdPayload): Promise<IpcResult<ToolExecutionView>>;
  rejectApproval(payload: ApprovalIdPayload): Promise<IpcResult<ApprovalRequestView>>;
  listPlans(payload?: PlanListPayload): Promise<IpcResult<PlanListView>>;
  getPlan(payload: PlanIdPayload): Promise<IpcResult<PlanDetailView>>;
  requestPlanStep(payload: PlanStepPayload): Promise<IpcResult<PlanStepRequestOutcomeView>>;
  resolvePlanRecovery(payload: PlanStepPayload): Promise<IpcResult<PlanRecoveryResolvedView>>;
  voiceStatus(): Promise<IpcResult<VoiceStatusView>>;
  voicePrepareModels(): Promise<IpcResult<VoiceStatusView>>;
  voiceArm(): Promise<IpcResult<EmptyPayload>>;
  voiceBegin(): Promise<IpcResult<EmptyPayload>>;
  voiceEnd(): Promise<IpcResult<EmptyPayload>>;
  voiceCancel(): Promise<IpcResult<EmptyPayload>>;
  ttsSpeak(payload: TtsSpeakPayload): Promise<IpcResult<EmptyPayload>>;
  ttsStop(): Promise<IpcResult<EmptyPayload>>;
  voiceFeed(payload: VoiceFeedPayload): Promise<IpcResult<EmptyPayload>>;
  /**
   * Subscribe to the single MAIN → RENDERER voice event channel
   * (state / partial / final / tts-chunk / tts-ended / failure).
   * The listener receives engine-validated payloads only; the returned
   * function unsubscribes. Events are never dispatchable renderer→main.
   */
  onVoiceEvent(listener: (event: VoiceEventPayload) => void): () => void;
};

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
  ENGINE_UNAVAILABLE:
    "The voice engine is not available in this build (speech runtime not configured).",
  VOICE_STATE: "That voice command does not match the current voice state.",
  MODEL_MISSING: "The local voice models are not installed yet. Prepare them first.",
  STT_FAILURE: "Local speech recognition failed — no transcript was produced.",
  TTS_FAILURE: "Local speech synthesis failed — no audio was produced.",
  RECORDING_CANCELLED: "Cancelled. Nothing was recorded or spoken.",
  AUDIO_OUTPUT_FAILURE: "Windows audio output could not be opened.",
  MICROPHONE_DENIED: "Microphone access was not granted.",
};

/** Chat-specific consequence copy shown under the generic error line. */
export const CHAT_ERROR_CONSEQUENCES: Partial<Record<IpcErrorCode, string>> = {
  AUTH_NOT_CONFIGURED: "Your message was not sent — it is restored in the composer.",
  BACKEND_UNREACHABLE: "Your message was not sent — it is restored in the composer.",
  TIMEOUT: "Your message was not sent — it is restored in the composer.",
  UNEXPECTED: "Your message may not have been sent — it is restored in the composer.",
};

/** Workspace-specific consequence copy (same failure-UI pattern as chat). */
export const APPROVAL_ERROR_CONSEQUENCES: Partial<Record<IpcErrorCode, string>> = {
  AUTH_NOT_CONFIGURED: "Your decision was not sent — the approval stays in its current state.",
  BACKEND_UNREACHABLE: "Your decision was not sent — the approval stays in its current state.",
  TIMEOUT: "Your decision was not sent — the approval stays in its current state.",
  UNEXPECTED: "Your decision may not have been sent — the approval stays in its current state.",
};

export const PLAN_ERROR_CONSEQUENCES: Partial<Record<IpcErrorCode, string>> = {
  AUTH_NOT_CONFIGURED: "Your request was not sent — the plan is unchanged.",
  BACKEND_UNREACHABLE: "Your request was not sent — the plan is unchanged.",
  TIMEOUT: "Your request was not sent — the plan is unchanged.",
  UNEXPECTED: "Your request may not have been sent — the plan is unchanged.",
};

/**
 * Understanding-oriented copy for HTTP statuses the workspaces can surface
 * (extends the chat context). Backend policy text arrives as the structured
 * `error.message`; this adds what the status MEANS.
 */
export const HTTP_STATUS_HINTS: Record<number, string> = {
  403: "denied by backend policy — nothing was executed.",
  404: "not found — it may have been resolved or deleted since you loaded it.",
  409: "conflict — the state changed since you loaded it; nothing happened, refresh and check.",
  422: "rejected by validation — the request was not actionable.",
  500: "internal backend error — nothing was executed.",
  502: "the language model provider failed — nothing was executed.",
  503: "the backend is at capacity or not fully configured — try again shortly.",
  504: "the request timed out server-side — treat the outcome as uncertain.",
};
