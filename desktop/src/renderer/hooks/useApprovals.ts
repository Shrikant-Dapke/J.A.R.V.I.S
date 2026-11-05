/**
 * Approval inventory consumer (list + detail + decisions) over the typed
 * IPC bridge. Backend policy stays authoritative everywhere:
 *  - the listing is metadata-only (advisory statuses),
 *  - decisive actions ALWAYS re-read the authoritative detail first,
 *  - exactly ONE backend attempt per decision — no automatic retries,
 *  - duplicate submissions are refused by an in-flight lock and by
 *    terminal state (backend answers 409 for already-resolved requests).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  APPROVAL_ID_PATTERN,
  type ApprovalRequestView,
  type ApprovalSummaryView,
  type IpcErrorCode,
  type ToolExecutionView,
} from "../../../shared/ipc-contract";
import {
  approveApprovalById,
  fetchApproval,
  fetchApprovals,
  rejectApprovalById,
} from "../services/bridge";

export type DecisionKind = "approve" | "reject";

export type WorkspaceFailure = {
  scope: string;
  code: IpcErrorCode;
  message: string;
  httpStatus?: number;
};

export type ApprovalBook = {
  items: ApprovalSummaryView[];
  total: number;
  loading: boolean;
  loaded: boolean;
  failure: WorkspaceFailure | null;
  load: () => void;
};

export type ApprovalDetail = {
  /** Authoritative request state (arguments included) or null. */
  view: ApprovalRequestView | null;
  loading: boolean;
  failure: WorkspaceFailure | null;
  refresh: () => void;
};

export type ApprovalAction = {
  /** Decision in flight or null; blocks every further decision button. */
  acting: DecisionKind | null;
  /** Confirmed single-attempt outcome of an "approve" action (ToolResult). */
  execution: ToolExecutionView | null;
  /** Failure of the last decision attempt (cleared by refresh/switch). */
  failure: WorkspaceFailure | null;
  act: (kind: DecisionKind, approvalId: string) => void;
  clearFailure: () => void;
  clearExecution: () => void;
};

export function useApprovalList(): ApprovalBook {
  const [items, setItems] = useState<ApprovalSummaryView[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [failure, setFailure] = useState<WorkspaceFailure | null>(null);
  const loadingRef = useRef(false);

  const load = useCallback(async () => {
    if (loadingRef.current) return;
    loadingRef.current = true;
    setLoading(true);
    try {
      // Newest first by backend policy (created_at DESC, approval_id DESC).
      const result = await fetchApprovals({ limit: 50 });
      if (result.ok) {
        setItems(result.data.approvals);
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

  useEffect(() => {
    void load();
  }, [load]);

  return { items, total, loading, loaded, failure, load: () => void load() };
}

/**
 * Authoritative detail reader tied to the selected approval. Every refresh
 * re-requests GET /api/approvals/{id} — the endpoint that also applies
 * lazy expiration, so what the user sees is the backend's current truth.
 */
export function useApprovalDetail(approvalId: string | null): ApprovalDetail {
  const [view, setView] = useState<ApprovalRequestView | null>(null);
  const [loading, setLoading] = useState(false);
  const [failure, setFailure] = useState<WorkspaceFailure | null>(null);
  const inFlight = useRef(false);
  const generationRef = useRef(0);
  const requestedRef = useRef<string | null>(null);

  const fetch = useCallback(async (): Promise<boolean> => {
    if (approvalId === null || inFlight.current) return false;
    inFlight.current = true;
    setLoading(true);
    const generation = ++generationRef.current;
    requestedRef.current = approvalId;
    try {
      const result = await fetchApproval({ approvalId });
      if (generation !== generationRef.current) return false; // stale switch
      if (result.ok) {
        setView(result.data);
        setFailure(null);
        return true;
      }
      setFailure({
        scope: "detail",
        code: result.code,
        message: result.message,
        httpStatus: result.httpStatus,
      });
      return false;
    } finally {
      inFlight.current = false;
      setLoading(false);
    }
  }, [approvalId]);

  useEffect(() => {
    if (approvalId === null) {
      generationRef.current += 1; // invalidate in-flight reads on clear
      setView(null);
      setFailure(null);
      setLoading(false);
      return;
    }
    if (requestedRef.current !== approvalId) {
      requestedRef.current = approvalId;
      setView(null);
      setFailure(null);
      void fetch();
    }
  }, [approvalId, fetch]);

  return {
    view,
    loading,
    failure,
    refresh: () => void fetch(),
  };
}

/**
 * Decision executor. The decisive-action contract lives here:
 *
 *  1. serialize: one decision at a time (`inFlight` guard), second clicks
 *     are ignored — the buttons are also disabled;
 *  2. authoritativeness: the caller passes a check callback verified
 *     against a FRESH authoritative read BEFORE the decisive POST;
 *  3. single attempt: the backend is called once per confirmed click and
 *     the result reflects exactly that attempt (404/409 handled honestly,
 *     never retried);
 *  4. the detail is re-read and the list refreshed afterwards so the UI
 *     converges to the backend's persisted state.
 */
export function useApprovalDecision(
  onPreActionCheck: (approvalId: string) => Promise<boolean>,
  onActionOutcome: () => void,
): ApprovalAction {
  const [acting, setActing] = useState<DecisionKind | null>(null);
  const [execution, setExecution] = useState<ToolExecutionView | null>(null);
  const [failure, setFailure] = useState<WorkspaceFailure | null>(null);
  const inFlight = useRef(false);
  const generationRef = useRef(0);

  const clearFailure = useCallback(() => setFailure(null), []);
  const clearExecution = useCallback(() => setExecution(null), []);

  const fail = useCallback((kind: DecisionKind, result: { code: IpcErrorCode; message: string; httpStatus?: number }) => {
    setFailure({
      scope: `decision:${kind}`,
      code: result.code,
      message: result.message,
      httpStatus: result.httpStatus,
    });
  }, []);

  const act = useCallback(
    (kind: DecisionKind, approvalId: string) => {
      void (async () => {
        if (inFlight.current) return; // duplicate-click prevention
        if (!approvalId || !APPROVAL_ID_PATTERN.test(approvalId)) return;
        inFlight.current = true;
        const generation = ++generationRef.current;
        setActing(kind);
        setFailure(null);
        setExecution(null);
        try {
          // Decisive actions must rest on a fresh authoritative read.
          const authoritative = await onPreActionCheck(approvalId);
          if (generation !== generationRef.current) return;
          if (!authoritative) {
            // The detail hook recorded the failure; surface a scope hint.
            setFailure({
              scope: `decision:${kind}`,
              code: "UNEXPECTED",
              message: "The approval's current state could not be confirmed.",
            });
            return;
          }
          if (kind === "approve") {
            const result = await approveApprovalById({ approvalId });
            if (generation !== generationRef.current) return;
            if (result.ok) {
              setExecution(result.data);
              onActionOutcome();
            } else {
              fail(kind, result);
              onActionOutcome();
            }
          } else {
            const result = await rejectApprovalById({ approvalId });
            if (generation !== generationRef.current) return;
            if (result.ok) {
              setExecution(null);
              onActionOutcome();
            } else {
              fail(kind, result);
              onActionOutcome();
            }
          }
        } finally {
          inFlight.current = false;
          if (generation === generationRef.current) setActing(null);
        }
      })();
    },
    [fail, onActionOutcome, onPreActionCheck],
  );

  return { acting, execution, failure, act, clearFailure, clearExecution };
}
