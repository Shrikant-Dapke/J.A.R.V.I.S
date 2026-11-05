/**
 * Plans consumer (overview + authoritative detail + step requests) over
 * the typed IPC bridge. Backend rules stay the single source of truth:
 *  - readiness comes ONLY from `ready_step_ids` on the authoritative
 *    detail read — the client never calculates eligibility itself;
 *  - `overall_status`, attention flags and recorded results are rendered
 *    verbatim from the backend derivation;
 *  - step requests and recovery reconciliation are ONE attempt each
 *    (no automatic retries) and refuse duplicate submissions.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  PLAN_ID_PATTERN,
  PLAN_STEP_ID_PATTERN,
  type IpcErrorCode,
  type PlanDetailView,
  type PlanStepRequestOutcomeView,
  type PlanSummaryView,
  type PlanRecoveryResolvedView,
} from "../../../shared/ipc-contract";
import {
  fetchPlan,
  fetchPlans,
  requestPlanStepById,
  resolvePlanRecoveryStep,
} from "../services/bridge";
import type { WorkspaceFailure } from "./useApprovals";

export type PlanBook = {
  plans: PlanSummaryView[];
  total: number;
  loading: boolean;
  loaded: boolean;
  failure: WorkspaceFailure | null;
  load: () => void;
};

export type PlanDetailState = {
  /** Authoritative PlanDetail or null (no plan selected yet). */
  view: PlanDetailView | null;
  loading: boolean;
  failure: WorkspaceFailure | null;
  refresh: () => void;
};

export type PlanAction = {
  /** Step payload of the decisive action in flight (blocks further ones). */
  busyStepId: string | null;
  busyKind: "request" | "resolve-recovery" | null;
  /** Last single-attempt outcome (rendered next to the step, then cleared). */
  lastOutcome: PlanStepRequestOutcomeView | null;
  lastOutcomeStepId: string | null;
  failure: WorkspaceFailure | null;
  requestStep: (planId: string, stepId: string) => void;
  resolveRecovery: (planId: string, stepId: string) => void;
  clearFailure: () => void;
  clearOutcome: () => void;
};

export function usePlanList(): PlanBook {
  const [plans, setPlans] = useState<PlanSummaryView[]>([]);
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
      // Newest first by backend policy (created_at DESC).
      const result = await fetchPlans({ limit: 50 });
      if (result.ok) {
        setPlans(result.data.plans);
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

  return { plans, total, loading, loaded, failure, load: () => void load() };
}

/** Authoritative plan reader tied to the selected plan id. */
export function usePlanDetail(planId: string | null): PlanDetailState {
  const [view, setView] = useState<PlanDetailView | null>(null);
  const [loading, setLoading] = useState(false);
  const [failure, setFailure] = useState<WorkspaceFailure | null>(null);
  const inFlight = useRef(false);
  const generationRef = useRef(0);
  const requestedRef = useRef<string | null>(null);

  const fetchPlanDetail = useCallback(async (id: string): Promise<boolean> => {
    if (inFlight.current) return false;
    inFlight.current = true;
    setLoading(true);
    const generation = ++generationRef.current;
    try {
      const result = await fetchPlan({ planId: id });
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
  }, []);

  useEffect(() => {
    if (planId === null) {
      generationRef.current += 1; // invalidate in-flight reads on clear
      setView(null);
      setFailure(null);
      setLoading(false);
      return;
    }
    if (!PLAN_ID_PATTERN.test(planId)) return;
    if (requestedRef.current !== planId) {
      requestedRef.current = planId;
      setView(null);
      setFailure(null);
      void fetchPlanDetail(planId);
    }
  }, [planId, fetchPlanDetail]);

  return { view, loading, failure, refresh: () => {
    if (planId !== null && PLAN_ID_PATTERN.test(planId)) void fetchPlanDetail(planId);
  } };
}

/**
 * Step request + recovery reconciliation executor.
 *
 *  - one action at a time (busyStepId lock; the UI disables all step
 *    buttons while one decisive action is in flight);
 *  - exactly ONE backend POST per confirmed click, never retried;
 *  - after any attempt the authoritative detail is re-read so the plan
 *    converges to the backend's persisted state (requested, approved,
 *    executed or failed steps all change stored state);
 *  - recovery reconciliation is a human decision the backend finalizes;
 *    the client only carries it.
 */
export function usePlanActions(
  onOutcome: () => void,
): PlanAction {
  const [busyStepId, setBusyStepId] = useState<string | null>(null);
  const [busyKind, setBusyKind] = useState<"request" | "resolve-recovery" | null>(null);
  const [lastOutcome, setLastOutcome] = useState<PlanStepRequestOutcomeView | null>(null);
  const [lastOutcomeStepId, setLastOutcomeStepId] = useState<string | null>(null);
  const [failure, setFailure] = useState<WorkspaceFailure | null>(null);
  const inFlight = useRef(false);
  const generationRef = useRef(0);

  const clearFailure = useCallback(() => setFailure(null), []);
  const clearOutcome = useCallback(() => {
    setLastOutcome(null);
    setLastOutcomeStepId(null);
  }, []);

  const run = useCallback(
    (
      kind: "request" | "resolve-recovery",
      planId: string,
      stepId: string,
      invoke: () => Promise<{ ok: true; data: PlanStepRequestOutcomeView | PlanRecoveryResolvedView } | { ok: false; code: IpcErrorCode; message: string; httpStatus?: number }>,
    ) => {
      void (async () => {
        if (inFlight.current) return; // duplicate-click prevention
        if (!PLAN_ID_PATTERN.test(planId) || !PLAN_STEP_ID_PATTERN.test(stepId)) return;
        inFlight.current = true;
        const generation = ++generationRef.current;
        setBusyStepId(stepId);
        setBusyKind(kind);
        setFailure(null);
        try {
          const result = await invoke();
          if (generation !== generationRef.current) return;
          if (result.ok) {
            if (kind === "request") {
              setLastOutcome(result.data as PlanStepRequestOutcomeView);
              setLastOutcomeStepId(stepId);
            }
            onOutcome();
          } else {
            setFailure({
              scope: `plan-action:${kind}`,
              code: result.code,
              message: result.message,
              httpStatus: result.httpStatus,
            });
            onOutcome();
          }
        } finally {
          inFlight.current = false;
          if (generation === generationRef.current) {
            setBusyStepId(null);
            setBusyKind(null);
          }
        }
      })();
    },
    [onOutcome],
  );

  const requestStep = useCallback(
    (planId: string, stepId: string) => {
      run("request", planId, stepId, () => requestPlanStepById({ planId, stepId }));
    },
    [run],
  );

  const resolveRecovery = useCallback(
    (planId: string, stepId: string) => {
      run("resolve-recovery", planId, stepId, () =>
        resolvePlanRecoveryStep({ planId, stepId }),
      );
    },
    [run],
  );

  return {
    busyStepId,
    busyKind,
    lastOutcome,
    lastOutcomeStepId,
    failure,
    requestStep,
    resolveRecovery,
    clearFailure,
    clearOutcome,
  };
}
