/**
 * Fully functional Plans workspace (Phase 6.3 Part B) over the typed IPC
 * bridge. The overview lists plans with backend attention flags; selecting
 * one fetches the authoritative detail. Step requests and recovery
 * reconciliations each make exactly one backend attempt (no automatic
 * retry) and re-read the plan afterwards so the UI converges to the
 * backend's persisted state.
 */

import { useCallback, useState } from "react";
import { PlanDetailCard } from "../components/plans/PlanDetailCard";
import { PlanSummaryList } from "../components/plans/PlanSummaryList";
import { CloseIcon, MenuIcon } from "../components/icons";
import { Badge } from "../components/primitives";
import { usePlanActions, usePlanDetail, usePlanList } from "../hooks/usePlans";
import type { BackendMonitor } from "../hooks/useBackendMonitor";
import { cn } from "../lib/cn";

function ConnectionBadge({ monitor }: { monitor: BackendMonitor }) {
  if (monitor.health === null) return <Badge tone="idle">checking…</Badge>;
  if (monitor.health.ok) {
    return <Badge tone="ok">backend up · {monitor.health.data.latencyMs} ms</Badge>;
  }
  return <Badge tone="danger">backend offline</Badge>;
}

export function PlansView({ monitor }: { monitor: BackendMonitor }) {
  const book = usePlanList();
  const [selectedPlanId, setSelectedPlanId] = useState<string | null>(null);
  const [railOpen, setRailOpen] = useState(false);

  const detail = usePlanDetail(selectedPlanId);
  // After ANY attempt (success or failure) the plan is re-read: both the
  // request and the recovery endpoints mutate persisted state.
  const actions = usePlanActions(useCallback(() => detail.refresh(), [detail]));

  const select = (planId: string) => {
    actions.clearOutcome();
    actions.clearFailure();
    setSelectedPlanId(planId);
    setRailOpen(false);
  };

  const railProps = {
    plans: book.plans,
    total: book.total,
    loading: book.loading,
    loaded: book.loaded,
    failure: book.failure,
    selectedId: selectedPlanId,
    locked: actions.busyStepId !== null,
    onRefresh: book.load,
    onSelect: select,
  };

  return (
    <div className="flex h-full min-h-0 w-full">
      {/* Plans rail — permanent on md+, drawer overlay below. */}
      <aside className="hidden h-full w-[290px] shrink-0 border-r border-line/70 bg-abyss/50 p-4 md:flex md:flex-col md:min-h-0">
        <PlanSummaryList {...railProps} />
      </aside>

      {railOpen ? (
        <div className="fixed inset-0 z-40 flex md:hidden">
          <button
            type="button"
            aria-label="Close plans"
            onClick={() => setRailOpen(false)}
            className="h-full flex-1 bg-void/70 backdrop-blur-sm"
          />
          <aside className="flex h-full w-[300px] shrink-0 flex-col border-r border-line bg-abyss p-4 md:min-h-0">
            <div className="mb-2 flex items-center justify-between">
              <span className="font-mono text-[11px] uppercase tracking-[0.24em] text-slate-300">
                Plans
              </span>
              <button
                type="button"
                aria-label="Close plans"
                onClick={() => setRailOpen(false)}
                className="focus-ring rounded p-1 text-slate-400 hover:text-accent-soft"
              >
                <CloseIcon className="h-4 w-4" />
              </button>
            </div>
            <PlanSummaryList {...railProps} />
          </aside>
        </div>
      ) : null}

      {/* Plan detail column */}
      <section className="flex h-full min-h-0 min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-3 border-b border-line/70 bg-abyss/20 px-4 py-2.5">
          <button
            type="button"
            aria-label="Open plans"
            aria-expanded={railOpen}
            onClick={() => setRailOpen(true)}
            className={cn(
              "focus-ring rounded-lg border border-line p-2 text-slate-300 hover:border-accent/40 hover:text-accent-soft md:hidden",
            )}
          >
            <MenuIcon className="h-4 w-4" />
          </button>
          <h2 className="min-w-0 truncate font-mono text-[11px] uppercase tracking-[0.24em] text-slate-300">
            {detail.view === null
              ? selectedPlanId === null
                ? "Select a plan"
                : "Loading plan…"
              : detail.view.goal}
          </h2>
          <div className="ml-auto hidden sm:block">
            <ConnectionBadge monitor={monitor} />
          </div>
        </header>

        <div className="scroll-hud min-h-0 flex-1 overflow-y-auto p-4 md:p-6">
          <div className="mx-auto max-w-3xl">
            {selectedPlanId === null ? (
              <div className="rounded-lg border border-dashed border-line/90 bg-hull/30 px-8 py-14 text-center">
                <h4 className="font-mono text-[12px] uppercase tracking-[0.22em] text-accent-soft">
                  Task plans
                </h4>
                <p className="mx-auto mt-3 max-w-md text-[13px] leading-relaxed text-slate-400">
                  Plans captured by the orchestrator execute one explicit step at a time through
                  policy and approval. Readiness, status and attention flags come straight from
                  the backend — this workspace never decides eligibility on its own.
                </p>
              </div>
            ) : (
              <PlanDetailCard
                planId={selectedPlanId}
                view={detail.view}
                loading={detail.loading}
                failure={detail.failure}
                actionFailure={actions.failure}
                busyStepId={actions.busyStepId}
                busyKind={actions.busyKind}
                lastOutcome={actions.lastOutcome}
                lastOutcomeStepId={actions.lastOutcomeStepId}
                onRefresh={detail.refresh}
                onRequestStep={(stepId) => actions.requestStep(selectedPlanId, stepId)}
                onResolveRecovery={(stepId) => actions.resolveRecovery(selectedPlanId, stepId)}
              />
            )}
          </div>
        </div>
      </section>
    </div>
  );
}
