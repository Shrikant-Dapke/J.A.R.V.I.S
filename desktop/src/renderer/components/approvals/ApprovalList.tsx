/**
 * Approval inventory list (metadata rows) + empty/loading/error states.
 * Rows carry NO arguments — the backend listing structurally drops them;
 * the authoritative detail fetch is the only place they appear.
 */

import type { ApprovalSummaryView } from "../../../../shared/ipc-contract";
import { cn } from "../../lib/cn";
import { Badge, ErrorNote, SectionTitle } from "../primitives";
import { Button } from "../controls";
import { RefreshIcon } from "../icons";
import { approvalStatusTone, riskTone } from "../workspaces/meta";
import { SkeletonRows } from "../workspaces/skeleton";
import { workspaceDetail as failureDetail, workspaceHeadline } from "../workspaces/failures";
import { APPROVAL_ERROR_CONSEQUENCES } from "../../../../shared/ipc-contract";
import type { WorkspaceFailure } from "../../hooks/useApprovals";

function formatWhen(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  const now = new Date();
  const sameDay = date.toDateString() === now.toDateString();
  return sameDay
    ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : date.toLocaleDateString([], { month: "short", day: "numeric" });
}

export function ApprovalList({
  items,
  total,
  loading,
  loaded,
  failure,
  selectedId,
  locked,
  onRefresh,
  onSelect,
}: {
  items: ApprovalSummaryView[];
  total: number;
  loading: boolean;
  loaded: boolean;
  failure: WorkspaceFailure | null;
  selectedId: string | null;
  /** true while a decisive action is in flight (selection locked). */
  locked: boolean;
  onRefresh: () => void;
  onSelect: (approvalId: string) => void;
}) {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="mb-3 flex items-center justify-between">
        <SectionTitle title="Approvals" suffix={`${total}`} />
        <Button variant="ghost" small onClick={onRefresh} disabled={loading || locked}>
          <RefreshIcon className={cn("h-3 w-3", loading && "animate-pulse-soft")} />
          {locked ? "Waiting…" : "Sync"}
        </Button>
      </div>

      {failure ? (
        <div className="mb-1 animate-fade-in-up">
          <ErrorNote detail={workspaceHeadline(failure)} />
          <p className="mt-1 text-[11px] leading-snug text-slate-500">
            {failureDetail(failure, APPROVAL_ERROR_CONSEQUENCES)}
          </p>
        </div>
      ) : null}

      <div className="scroll-hud mt-1 min-h-0 flex-1">
        {loading && !loaded ? (
          <SkeletonRows rows={4} />
        ) : loaded && items.length === 0 ? (
          <p className="px-1 py-3 text-[12.5px] leading-relaxed text-slate-500">
            No approvals on record. Pending actions appear here when a plan step or chat
            proposal needs your decision.
          </p>
        ) : (
          <ul className="flex flex-col gap-1.5">
            {items.map((item) => {
              const active = item.approvalId === selectedId;
              return (
                <li key={item.approvalId} className="animate-fade-in-up">
                  <div
                    role="button"
                    tabIndex={locked ? -1 : 0}
                    aria-current={active ? "true" : undefined}
                    onClick={() => {
                      if (!locked) onSelect(item.approvalId);
                    }}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" && !locked) onSelect(item.approvalId);
                    }}
                    className={cn(
                      "cursor-pointer rounded-lg border px-3 py-2.5 transition-colors focus-ring",
                      active
                        ? "border-accent/40 bg-accent/10"
                        : "border-transparent hover:border-line hover:bg-white/[0.03]",
                    )}
                  >
                    <div className="flex items-center gap-2">
                      <Badge tone={approvalStatusTone(item.status)}>{item.status}</Badge>
                      <span className="font-mono text-[11px] text-slate-200">
                        {item.toolName}
                      </span>
                      <span className="ml-auto font-mono text-[10px] text-slate-500">
                        {formatWhen(item.createdAt)}
                      </span>
                    </div>
                    <p className="mt-1 line-clamp-2 text-[12px] leading-snug text-slate-400">
                      {item.description}
                    </p>
                    <div className="mt-1.5 flex items-center gap-2">
                      <Badge tone={riskTone(item.riskLevel)}>{item.riskLevel}</Badge>
                      {item.resolvedAt ? (
                        <span className="font-mono text-[9.5px] text-slate-600">
                          resolved {formatWhen(item.resolvedAt)}
                        </span>
                      ) : null}
                    </div>
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
