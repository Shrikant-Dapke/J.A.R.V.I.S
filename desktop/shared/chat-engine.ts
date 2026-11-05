/**
 * Pure chat-session core — NO React, NO Electron, NO network imports.
 *
 * This module is compiled into the renderer bundle AND into the plain-Node
 * contract tests, so the rules that matter are verifiable without a DOM:
 *
 *  - optimistic user messages (`kind: "pending"` → `"sent"` / `"failed"`),
 *  - a genuine in-flight state (`sending`) that gates duplicate submits,
 *  - serialized chat requests (one at a time, second submit refused),
 *  - no automatic retry anywhere (a failed attempt stays failed until the
 *    user resends; the original text is preserved by the caller),
 *  - conversation switching (loading → ready/failed) as pure transitions.
 *
 * The React hook (useChatSession) only wires these functions to state and
 * the IPC bridge; it adds no chat rules of its own.
 */

import type {
  ChatExchange,
  ChatExchangeStatus,
  ConversationTurnView,
  ExecutedActionView,
  IpcErrorCode,
  PlanProposalView,
  ProposedActionView,
} from "./ipc-contract";

export type ChatMessageKind = "sent" | "pending" | "failed";

/** Structured payload attached to an assistant turn (proposal/result). */
export type ChatProposal = {
  status: ChatExchangeStatus;
  action: ProposedActionView | null;
  toolResult: ExecutedActionView | null;
  plan: PlanProposalView | null;
};

export type ChatFailure = {
  code: IpcErrorCode;
  message: string;
  httpStatus?: number;
};

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  /** ISO timestamp — backend timestamp for history, local clock for live turns. */
  createdAt: string;
  kind: ChatMessageKind;
  /** Present on assistant turns that returned a proposal or executed result. */
  proposal: ChatProposal | null;
  /** Present only when kind === "failed" (user turn not delivered). */
  failure: ChatFailure | null;
};

export type ChatSessionState = {
  /** null = a new, not-yet-persisted conversation (welcome/empty state). */
  conversationId: string | null;
  /** true while history is being loaded (switch or retry). */
  loading: boolean;
  /** History-load failure for the active conversation (empty transcript). */
  loadError: ChatFailure | null;
  messages: ChatMessage[];
  /** true while a chat request is in flight. */
  sending: boolean;
  /** Backend history window did not include every stored turn. */
  truncated: boolean;
};

/** Welcome state: new chat, nothing typed, no history. */
export const EMPTY_CHAT_SESSION: ChatSessionState = {
  conversationId: null,
  loading: false,
  loadError: null,
  messages: [],
  sending: false,
  truncated: false,
};

/* ------------------------------------------------------------------ */
/* Message ids (generated OUTSIDE the reducer to keep it deterministic) */
/* ------------------------------------------------------------------ */

let fallbackCounter = 0;

export function createChatMessageId(): string {
  const holder = globalThis as {
    crypto?: { randomUUID?: () => string };
  };
  const randomUUID = holder.crypto?.randomUUID;
  if (typeof randomUUID === "function") {
    // Keep the receiver: webcrypto methods validate their `this`.
    return randomUUID.call(holder.crypto);
  }
  fallbackCounter += 1;
  return `msg-${Date.now().toString(36)}-${fallbackCounter}-${Math.random().toString(36).slice(2, 10)}`;
}

/** Map backend history turns into settled chat messages. */
export function historyTurnsToMessages(
  turns: readonly ConversationTurnView[],
  makeId: () => string = createChatMessageId,
): ChatMessage[] {
  return turns.map((turn) => ({
    id: makeId(),
    role: turn.role,
    content: turn.content,
    createdAt: turn.timestamp,
    kind: "sent",
    proposal: null,
    failure: null,
  }));
}

/* ------------------------------------------------------------------ */
/* Reducer                                                             */
/* ------------------------------------------------------------------ */

export type ChatAction =
  | { type: "new-chat" }
  | { type: "history-loading"; conversationId: string }
  | { type: "history-ready"; conversationId: string; messages: ChatMessage[]; totalTurnCount: number }
  | { type: "history-failed"; conversationId: string; failure: ChatFailure }
  | { type: "send-start"; userMessage: ChatMessage }
  | { type: "send-success"; userMessageId: string; assistantMessage: ChatMessage; conversationId: string }
  | { type: "send-failed"; userMessageId: string; failure: ChatFailure };

function failMessage(message: ChatMessage, failure: ChatFailure): ChatMessage {
  return { ...message, kind: "failed", failure };
}

export function reduceChatSession(
  state: ChatSessionState,
  action: ChatAction,
): ChatSessionState {
  switch (action.type) {
    case "new-chat":
      // An in-flight reply cannot be abandoned; the UI blocks this path too.
      if (state.sending) return state;
      return { ...EMPTY_CHAT_SESSION };

    case "history-loading":
      if (state.sending) return state;
      return {
        ...EMPTY_CHAT_SESSION,
        conversationId: action.conversationId,
        loading: true,
      };

    case "history-ready":
      if (state.sending) return state;
      return {
        conversationId: action.conversationId,
        loading: false,
        loadError: null,
        messages: action.messages,
        sending: false,
        truncated: action.totalTurnCount > action.messages.length,
      };

    case "history-failed":
      if (state.sending) return state;
      return {
        ...EMPTY_CHAT_SESSION,
        conversationId: action.conversationId,
        loadError: action.failure,
      };

    case "send-start":
      // Serialization invariant: a second submit is refused while in flight.
      if (state.sending) return state;
      return {
        ...state,
        loading: false,
        loadError: null,
        messages: [...state.messages, action.userMessage],
        sending: true,
      };

    case "send-success":
      return {
        ...state,
        conversationId: action.conversationId,
        messages: [
          ...state.messages.map((message) =>
            message.id === action.userMessageId
              ? { ...message, kind: "sent" as const, failure: null }
              : message,
          ),
          action.assistantMessage,
        ],
        sending: false,
      };

    case "send-failed":
      return {
        ...state,
        messages: state.messages.map((message) =>
          message.id === action.userMessageId
            ? failMessage(message, action.failure)
            : message,
        ),
        sending: false,
      };

    default: {
      // Exhaustiveness check: a new action type must be handled above.
      const exhaustive: never = action;
      return exhaustive;
    }
  }
}

/* ------------------------------------------------------------------ */
/* Submission gate: one chat request at a time, no retries             */
/* ------------------------------------------------------------------ */

export type ChatGate = {
  readonly busy: boolean;
  /** Claim the single in-flight slot; false when a submission is active. */
  tryBegin(): boolean;
  /** Release the slot (always called from `finally`). */
  end(): void;
};

export function createChatGate(): ChatGate {
  let active = false;
  return {
    get busy() {
      return active;
    },
    tryBegin() {
      if (active) return false;
      active = true;
      return true;
    },
    end() {
      active = false;
    },
  };
}

export type ExclusiveRun<T> =
  | { ran: true; value: T }
  | { ran: false; reason: "busy" };

/**
 * Run `task` exactly once if the gate is free. The gate is ALWAYS released,
 * including on throw — and the task is never re-invoked here, so callers
 * cannot accidentally introduce retries (and no retry policy exists).
 */
export async function runExclusive<T>(
  gate: ChatGate,
  task: () => Promise<T>,
): Promise<ExclusiveRun<T>> {
  if (!gate.tryBegin()) return { ran: false, reason: "busy" };
  try {
    return { ran: true, value: await task() };
  } finally {
    gate.end();
  }
}

/** Build the assistant message for a live chat exchange. */
export function exchangeToAssistantMessage(
  exchange: ChatExchange,
  makeId: () => string = createChatMessageId,
): ChatMessage {
  return {
    id: makeId(),
    role: "assistant",
    content: exchange.reply,
    createdAt: exchange.occurredAt,
    kind: "sent",
    proposal:
      exchange.action || exchange.toolResult || exchange.plan
        ? {
            status: exchange.status,
            action: exchange.action,
            toolResult: exchange.toolResult,
            plan: exchange.plan,
          }
        : null,
    failure: null,
  };
}
