/**
 * Chat session hook: the ONLY chat policy lives in the pure engine
 * (shared/chat-engine.ts); this hook merely applies it to React state
 * and the IPC bridge.
 *
 * Rules preserved here:
 *  - one submission at a time (gate), duplicates refused with "busy",
 *  - exactly ONE backend attempt per submit — no automatic retry,
 *  - stale conversation loads are dropped (generation counter),
 *  - send paths are ignored while a conversation switch is in progress.
 */

import { useCallback, useReducer, useRef } from "react";
import {
  createChatMessageId,
  createChatGate,
  exchangeToAssistantMessage,
  historyTurnsToMessages,
  reduceChatSession,
  runExclusive,
  EMPTY_CHAT_SESSION,
  type ChatFailure,
  type ChatMessage,
  type ChatSessionState,
} from "../../../shared/chat-engine";
import { CHAT_MESSAGE_MAX_LENGTH } from "../../../shared/ipc-contract";
import { fetchConversation, sendChatMessage } from "../services/bridge";

export type SendOutcome = "sent" | "failed" | "busy";

export type ChatSession = {
  state: ChatSessionState;
  /** Load an existing conversation's history into the workspace. */
  openConversation: (conversationId: string) => void;
  /** Reset to a fresh, unsaved conversation (welcome state). */
  startNewChat: () => void;
  /** Re-run the history load of the active conversation after a failure. */
  retryHistory: () => void;
  /**
   * Submit one chat turn (single attempt, serialized). On "failed" the
   * caller restores the exact text so the user never loses it.
   */
  sendMessage: (content: string) => Promise<SendOutcome>;
};

export function useChatSession(): ChatSession {
  const [state, dispatch] = useReducer(reduceChatSession, EMPTY_CHAT_SESSION);
  const gate = useRef(createChatGate()).current;

  // Stable snapshots for async paths (the ref must never be read during
  // render-setup for decisions the reducer already owns).
  const stateRef = useRef<ChatSessionState>(state);
  stateRef.current = state;
  const generationRef = useRef(0);

  const loadHistory = useCallback(async (conversationId: string, generation: number) => {
    const result = await fetchConversation({ conversationId });
    // Only the newest load may write: switching again invalidates this one.
    if (generationRef.current !== generation || stateRef.current.conversationId !== conversationId) {
      return;
    }
    if (result.ok) {
      dispatch({
        type: "history-ready",
        conversationId,
        messages: historyTurnsToMessages(result.data.turns),
        totalTurnCount: result.data.totalTurnCount,
      });
    } else {
      const failure: ChatFailure = {
        code: result.code,
        message: result.message,
        httpStatus: result.httpStatus,
      };
      dispatch({ type: "history-failed", conversationId, failure });
    }
  }, []);

  const openConversation = useCallback(
    (conversationId: string) => {
      if (stateRef.current.sending) return;
      generationRef.current += 1;
      dispatch({ type: "history-loading", conversationId });
      void loadHistory(conversationId, generationRef.current);
    },
    [loadHistory],
  );

  const startNewChat = useCallback(() => {
    if (stateRef.current.sending) return;
    generationRef.current += 1; // any in-flight history load becomes stale
    dispatch({ type: "new-chat" });
  }, []);

  const retryHistory = useCallback(() => {
    const current = stateRef.current;
    if (current.sending || current.conversationId === null) return;
    generationRef.current += 1;
    dispatch({ type: "history-loading", conversationId: current.conversationId });
    void loadHistory(current.conversationId, generationRef.current);
  }, [loadHistory]);

  const sendMessage = useCallback(
    async (rawContent: string): Promise<SendOutcome> => {
      const content = rawContent.trim();
      // Composer enforces these bounds too; this is the honest backstop.
      if (content.length === 0 || content.length > CHAT_MESSAGE_MAX_LENGTH) {
        return "failed";
      }
      const outcome = await runExclusive(gate, async () => {
        const conversationId = stateRef.current.conversationId;
        const userMessage: ChatMessage = {
          id: createChatMessageId(),
          role: "user",
          content,
          createdAt: new Date().toISOString(),
          kind: "pending",
          proposal: null,
          failure: null,
        };
        dispatch({ type: "send-start", userMessage });
        const result = await sendChatMessage({ message: content, conversationId });
        if (result.ok) {
          dispatch({
            type: "send-success",
            userMessageId: userMessage.id,
            assistantMessage: exchangeToAssistantMessage(result.data),
            conversationId: result.data.conversationId,
          });
          return "sent" as const;
        }
        const failure: ChatFailure = {
          code: result.code,
          message: result.message,
          httpStatus: result.httpStatus,
        };
        dispatch({ type: "send-failed", userMessageId: userMessage.id, failure });
        return "failed" as const;
      });
      return outcome.ran ? outcome.value : "busy";
    },
    [gate],
  );

  return { state, openConversation, startNewChat, retryHistory, sendMessage };
}
