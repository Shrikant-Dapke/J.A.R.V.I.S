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

/** Explicit, exhaustive channel registry. Add new channels here only. */
export const IPC_CHANNELS = {
  backendStatus: "jarvis:backend-status",
  checkHealth: "jarvis:check-health",
  getSystemInfo: "jarvis:get-system-info",
  sendChat: "jarvis:send-chat",
  listConversations: "jarvis:list-conversations",
  getConversation: "jarvis:get-conversation",
  deleteConversation: "jarvis:delete-conversation",
} as const;

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
  | "UNEXPECTED";

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

/* ------------------------- validation contract --------------------- */

/** Backend-bound message size: mirrors ChatRequest.message.maxLength. */
export const CHAT_MESSAGE_MAX_LENGTH = 4000;

/**
 * Conversation identifiers originate from the backend (`str(uuid4())`).
 * The strict pattern keeps arbitrary strings out of request URL paths.
 */
export const CONVERSATION_ID_PATTERN =
  /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

/** Backend policy: page size 1..200 (DEFAULT_LIST_LIMIT 50, MAX 200). */
export const CONVERSATION_LIST_LIMITS = { default: 50, min: 1, max: 200 } as const;

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

/** Validate `jarvis:list-conversations` (optional bounded pagination). */
export function validateConversationListPayload(
  value: unknown,
): PayloadValidation<ConversationListPayload> {
  if (value === undefined || isEmptyPayload(value)) return { ok: true, value: {} };
  if (!isPlainObject(value)) {
    return { ok: false, reason: "Conversation list payload must be an object." };
  }
  const { limit, offset, ...extra } = value;
  if (Object.keys(extra).length > 0) {
    return { ok: false, reason: "Conversation list payload accepts limit and offset only." };
  }
  if (limit !== undefined) {
    if (!isInteger(limit) || limit < CONVERSATION_LIST_LIMITS.min || limit > CONVERSATION_LIST_LIMITS.max) {
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

/** Per-channel payload validators; the registry consumes this table. */
export type IpcPayloadMap = {
  "jarvis:backend-status": EmptyPayload;
  "jarvis:check-health": EmptyPayload;
  "jarvis:get-system-info": EmptyPayload;
  "jarvis:send-chat": ChatSendPayload;
  "jarvis:list-conversations": ConversationListPayload;
  "jarvis:get-conversation": ConversationIdPayload;
  "jarvis:delete-conversation": ConversationIdPayload;
};

export type PayloadValidator<K extends keyof IpcPayloadMap> = (
  value: unknown,
) => PayloadValidation<IpcPayloadMap[K]>;

function validateEmptyPayload(value: unknown): PayloadValidation<EmptyPayload> {
  return isEmptyPayload(value)
    ? { ok: true, value: {} }
    : { ok: false, reason: "This channel accepts an empty payload only." };
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
};

/** Chat-specific consequence copy shown under the generic error line. */
export const CHAT_ERROR_CONSEQUENCES: Partial<Record<IpcErrorCode, string>> = {
  AUTH_NOT_CONFIGURED: "Your message was not sent — it is restored in the composer.",
  BACKEND_UNREACHABLE: "Your message was not sent — it is restored in the composer.",
  TIMEOUT: "Your message was not sent — it is restored in the composer.",
  UNEXPECTED: "Your message may not have been sent — it is restored in the composer.",
};

/**
 * Understanding-oriented copy for HTTP statuses the chat can surface
 * (beyond IPC_ERROR_COPY). Backend policy text arrives as the structured
 * `error.message`; this adds what the status MEANS.
 */
export const HTTP_STATUS_HINTS: Record<number, string> = {
  403: "denied by backend policy — nothing was executed.",
  404: "not found — it may have been deleted in the meantime.",
  422: "rejected by validation — adjust the message and resend.",
  500: "internal backend error — nothing was executed.",
  502: "the language model provider failed — nothing was executed.",
  503: "the backend is at capacity or not fully configured — try again shortly.",
  504: "the language model timed out — nothing was executed.",
};
