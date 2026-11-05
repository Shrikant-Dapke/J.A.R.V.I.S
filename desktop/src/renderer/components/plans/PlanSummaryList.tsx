/**
 * Plan inventory list (metadata rows + backend attention flags) with
 * empty/loading/error states. Rows are summaries only — readiness and
 * statuses are authoritative on the detail endpoint.
 */

import type { PlanSummaryView } from "../../../../shared/ipc-contract";
import { cn } from "../../lib/cn";
import { Badge, ErrorNote, SectionTitle } from "../primitives";
import { Button } from "../controls";
import { RefreshIcon } from "../icons";
import { SkeletonRows } from "../workspaces/skeleton";
import { workspaceDetail, workspaceHeadline } from "../workspaces/failures";
import { PLAN_ERROR_CONSEQUENCES } from "../../../../shared/ipc-contract";
import type { WorkspaceFailure } from "../../hooks/useApprovals";

function AdaptedFailure({ failure }: { failure: WorkspaceFailure }) {
  return (
    <div className="mb-1 animate-fade-in-up">
      <ErrorNote detail={workspaceHeadline(failure)} />
      <p className="mt-1 text-[11px] leading-snug text-slate-500">
        {workspaceDetail(failure, PLAN_ERROR_CONSEQUENCES)}
      </p>
    </div>
  );
}

export function PlanSummaryList({
  plans,
  total,
  loading,
  loaded,
  failure,
  selectedId,
  locked,
  onRefresh,
  onSelect,
}: {
  plans: PlanSummaryView[];
  total: number;
  loading: boolean;
  loaded: boolean;
  failure: WorkspaceFailure | null;
  selectedId: string | null;
  /** true while a step request / recovery action is in flight. */
  locked: boolean;
  onRefresh: () => void;
  onSelect: (planId: string) => void;
}) {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="mb-3 flex items-center justify-between">
        <SectionTitle title="Plans" suffix={`${total}`} />
        <Button variant="ghost" small onClick={onRefresh} disabled={loading || locked}>
          <RefreshIcon className={cn("h-3 w-3", loading && "animate-pulse-soft")} />
          {locked ? "Waiting…" : "Sync"}
        </Button>
      </div>

      {failure ? <AdaptedFailure failure={failure} /> : null}

      <div className="scroll-hud mt-1 min-h-0 flex-1">
        {loading && !loaded ? (
          <SkeletonRows rows={4} />
        ) : loaded && plans.length === 0 ? (
          <p className="px-1 py-3 text-[12.5px] leading-relaxed text-slate-500">
            No plans on record. Plans appear here when the orchestrator captures one from a
            conversation.
          </p>
        ) : (
          <ul className="flex flex-col gap-1.5">
            {plans.map((plan) => {
              const active = plan.planId === selectedId;
              return (
                <li key={plan.planId} className="animate-fade-in-up">
                  <div
                    role="button"
                    tabIndex={locked ? -1 : 0}
                    aria-current={active ? "true" : undefined}
                    onClick={() => {
                      if (!locked) onSelect(plan.planId);
                    }}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" && !locked) onSelect(plan.planId);
                    }}
                    className={cn(
                      "cursor-pointer rounded-lg border px-3 py-2.5 transition-colors focus-ring",
                      active
                        ? "border-accent/40 bg-accent/10"
                        : "border-transparent hover:border-line hover:bg-white/[0.03]",
                    )}
                  >
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-[10px] text-slate-500">
                        {plan.stepCount} step{plan.stepCount === 1 ? "" : "s"}
                      </span>
                      {plan.needsAttention ? (
                        <Badge tone="danger">attention</Badge>
                      ) : null}
                      <span className="ml-auto font-mono text-[10px] text-slate-500">
                        {new Date(plan.updatedAt).toLocaleDateString([], {
                          month: "short",
                          day: "numeric",
                        })}
                      </span>
                    </div>
                    <p className="mt-1 line-clamp-2 text-[12px] leading-snug text-slate-300">
                      {plan.goal}
                    </p>
                    {(plan.recoveryRequired.length > 0 || plan.awaitingApproval.length > 0) ? (
                      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                        {plan.recoveryRequired.length > 0 ? (
                          <Badge tone="danger">
                            recovery ×{plan.recoveryRequired.length}
                          </Badge>
                        ) : null}
                        {plan.awaitingApproval.length > 0 ? (
                          <Badge tone="warn">approval ×{plan.awaitingApproval.length}</Badge>
                        ) : null}
                      </div>
                    ) : null}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
