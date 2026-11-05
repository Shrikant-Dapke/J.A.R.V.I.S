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
  listApprovals: "jarvis:list-approvals",
  getApproval: "jarvis:get-approval",
  approveApproval: "jarvis:approve-approval",
  rejectApproval: "jarvis:reject-approval",
  listPlans: "jarvis:list-plans",
  getPlan: "jarvis:get-plan",
  requestPlanStep: "jarvis:request-plan-step",
  resolvePlanRecovery: "jarvis:resolve-plan-recovery",
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
  "jarvis:list-approvals": validateApprovalListPayload,
  "jarvis:get-approval": validateApprovalIdPayload,
  "jarvis:approve-approval": validateApprovalIdPayload,
  "jarvis:reject-approval": validateApprovalIdPayload,
  "jarvis:list-plans": validatePlanListPayload,
  "jarvis:get-plan": validatePlanIdPayload,
  "jarvis:request-plan-step": validatePlanStepPayload,
  "jarvis:resolve-plan-recovery": validatePlanStepPayload,
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
