/**
 * Conversation inventory consumer (list + delete) over the typed IPC
 * bridge. Metadata-only by backend design; every refresh replaces the
 * whole book (bounded page of ≤200 entries, newest activity first).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type {
  ConversationSummaryView,
  IpcErrorCode,
} from "../../../shared/ipc-contract";
import { deleteConversationById, fetchConversations } from "../services/bridge";

export type FailureScope = "load" | "delete";

export type ConversationListFailure = {
  scope: FailureScope;
  code: IpcErrorCode;
  message: string;
  httpStatus?: number;
};

export type ConversationBook = {
  items: ConversationSummaryView[];
  total: number;
  /** true while the page (re)loads. */
  loading: boolean;
  /** true once the first successful load happened (items may be empty). */
  loaded: boolean;
  failure: ConversationListFailure | null;
  /** conversation id currently being deleted (for per-row pending state). */
  deletingId: string | null;
  /** (Re)load the first metadata page (newest activity first). */
  load: () => void;
  /**
   * Delete one conversation; resolves true when deleted. The caller
   * decides whether the active session must reset.
   */
  remove: (conversationId: string) => Promise<boolean>;
};

export function useConversations(): ConversationBook {
  const [items, setItems] = useState<ConversationSummaryView[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [failure, setFailure] = useState<ConversationListFailure | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const loadingRef = useRef(false);

  const load = useCallback(async () => {
    if (loadingRef.current) return;
    loadingRef.current = true;
    setLoading(true);
    try {
      const result = await fetchConversations({ limit: 50 });
      if (result.ok) {
        setItems(result.data.conversations);
        setTotal(result.data.total);
        setLoaded(true);
        setFailure(null);
      } else {
        setFailure({
          scope: "load",
          code: result.code,
          message: result.message,
          httpStatus: result.httpStatus,
        });
      }
    } finally {
      loadingRef.current = false;
      setLoading(false);
    }
  }, []);

  const remove = useCallback(async (conversationId: string): Promise<boolean> => {
    setDeletingId(conversationId);
    try {
      const result = await deleteConversationById({ conversationId });
      if (result.ok) {
        // Optimistic book update: one row gone, one fewer total.
        setItems((current) => current.filter((item) => item.conversationId !== conversationId));
        setTotal((current) => Math.max(0, current - 1));
        setFailure(null);
        return true;
      }
      setFailure({
        scope: "delete",
        code: result.code,
        message: result.message,
        httpStatus: result.httpStatus,
      });
      return false;
    } finally {
      setDeletingId(null);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return { items, total, loading, loaded, failure, deletingId, load: () => void load(), remove };
}
