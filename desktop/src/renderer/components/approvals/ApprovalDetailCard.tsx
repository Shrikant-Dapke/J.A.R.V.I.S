/**
 * Authoritative approval detail card: exact arguments bound to the
 * approval, risk visualization, TTL countdown, and the decisive
 * approve/reject flow with explicit confirmation. The backend stays
 * authoritative: the client renders what the detail endpoint returned
 * (including lazy-expired states) and never fabricates lifecycle states.
 */

import { useState } from "react";
import {
  APPROVAL_ERROR_CONSEQUENCES,
  type ApprovalRequestView,
  type ToolExecutionView,
} from "../../../../shared/ipc-contract";
import type { DecisionKind, WorkspaceFailure } from "../../hooks/useApprovals";
import {
  approvalStatusTone,
  expiryText,
  formatTimestamp,
  RISK_EXPLANATION,
  riskTone,
  stringifyValue,
} from "../workspaces/meta";
import { SkeletonLines } from "../workspaces/skeleton";
import { workspaceDetail as failureDetail, workspaceHeadline } from "../workspaces/failures";
import { Badge, ErrorNote, KeyValue, SectionTitle } from "../primitives";
import { Button } from "../controls";
import { RefreshIcon } from "../icons";
import { useTickingNow } from "../../hooks/useTickingNow";
import { cn } from "../../lib/cn";

function ExecutionResult({ execution }: { execution: ToolExecutionView }) {
  const dataEntries = execution.data ? Object.entries(execution.data) : [];
  return (
    <div
      className={cn(
        "mt-3 rounded-lg border px-3 py-3",
        execution.success ? "border-ok/40 bg-ok/5" : "border-danger/45 bg-danger/5",
      )}
    >
      <div className="flex items-center gap-2">
        <Badge tone={execution.success ? "ok" : "danger"}>{execution.status}</Badge>
        <span className="font-mono text-[11px] text-slate-200">{execution.toolName}</span>
        {execution.errorCode ? (
          <Badge tone="danger">{execution.errorCode}</Badge>
        ) : null}
      </div>
      <p
        className={cn(
          "mt-1.5 text-[12.5px] leading-relaxed",
          execution.success ? "text-ok" : "text-danger",
        )}
      >
        {execution.message}
      </p>
      {dataEntries.length > 0 ? (
        <div className="mt-2 rounded bg-void/40 px-2.5 py-1.5">
          {dataEntries.map(([key, value]) => (
            <p key={key} className="font-mono text-[10.5px] text-slate-400">
              <span className="text-slate-500">{key}</span>: {stringifyValue(value)}
            </p>
          ))}
        </div>
      ) : null}
      <p className="mt-2 font-mono text-[10px] uppercase tracking-[0.14em] text-slate-500">
        single-use approval consumed · this result came from exactly one execution
      </p>
    </div>
  );
}

const DECISION_CONFLICT_CODES = new Set([404, 409]);

export function ApprovalDetailCard({
  approvalId,
  view,
  loading,
  failure,
  acting,
  execution,
  actionFailure,
  onRefresh,
  onConfirm,
  isPending,
}: {
  approvalId: string;
  view: ApprovalRequestView | null;
  loading: boolean;
  failure: WorkspaceFailure | null;
  acting: DecisionKind | null;
  /** Result of an approved execution this session (single-use receipt). */
  execution: ToolExecutionView | null;
  actionFailure: WorkspaceFailure | null;
  onRefresh: () => void;
  /** Called with the decision kind after the human confirms in the panel. */
  onConfirm: (kind: DecisionKind) => void;
  /** fresh, pre-decision pending state of this approval. */
  isPending: boolean;
}) {
  const [confirmKind, setConfirmKind] = useState<DecisionKind | null>(null);
  const now = useTickingNow();

  const header = (
    <div className="mb-3 flex items-center justify-between">
      <SectionTitle title="Decision" suffix={approvalId.slice(0, 8)} />
      <Button variant="ghost" small onClick={onRefresh} disabled={loading || acting !== null}>
        <RefreshIcon className={cn("h-3 w-3", loading && "animate-pulse-soft")} />
        Re-read
      </Button>
    </div>
  );

  if (loading && view === null) {
    return (
      <div>
        {header}
        <SkeletonLines lines={4} />
        <div className="mt-4">
          <SkeletonLines lines={2} />
        </div>
      </div>
    );
  }

  if (failure) {
    return (
      <div>
        {header}
        <ErrorNote detail={workspaceHeadline(failure)} />
        <p className="mt-1 text-[11px] leading-snug text-slate-500">
          {failureDetail(failure, APPROVAL_ERROR_CONSEQUENCES)}
        </p>
        <div className="mt-3">
          <Button variant="ghost" small onClick={onRefresh} disabled={loading}>
            <RefreshIcon className="h-3 w-3" />
            Retry
          </Button>
        </div>
      </div>
    );
  }

  if (view === null) return <div>{header}</div>;

  const pending = isPending && view.status === "PENDING";
  const argumentEntries = Object.entries(view.arguments);
  const conflict = actionFailure !== null && DECISION_CONFLICT_CODES.has(actionFailure.httpStatus ?? -1);

  return (
    <div>
      {header}

      {view.status === "PENDING" && !isPending ? (
        <div className="mb-3 rounded-lg border border-warn/40 bg-warn/5 px-3 py-2">
          <p className="text-[12px] leading-relaxed text-warn">
            The snapshot below may be stale — press <span className="font-mono">Re-read</span> to
            fetch the current authoritative state before deciding.
          </p>
        </div>
      ) : null}

      {conflict ? (
        <div className="mb-3 animate-fade-in-up">
          <ErrorNote detail={workspaceHeadline(actionFailure)} />
          <p className="mt-1 text-[11px] leading-snug text-slate-500">
            {failureDetail(actionFailure, APPROVAL_ERROR_CONSEQUENCES)}
          </p>
          <p className="mt-1 text-[11px] leading-snug text-slate-500">
            The state below refreshes automatically; re-check before acting again.
          </p>
        </div>
      ) : actionFailure && !conflict ? (
        <div className="mb-3">
          <ErrorNote detail={workspaceHeadline(actionFailure)} />
          <p className="mt-1 text-[11px] leading-snug text-slate-500">
            {failureDetail(actionFailure, APPROVAL_ERROR_CONSEQUENCES)}
          </p>
        </div>
      ) : null}

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-[1fr_auto]">
        <div className="rounded-lg border border-line bg-hull/40 p-4">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={approvalStatusTone(view.status)}>{view.status}</Badge>
            <Badge tone={riskTone(view.riskLevel)}>{view.riskLevel}</Badge>
            {view.resolvedAt ? (
              <span className="font-mono text-[10px] text-slate-500">
                resolved {formatTimestamp(view.resolvedAt)}
              </span>
            ) : (
              <span className="font-mono text-[10px] text-slate-400">
                {expiryText(view.expiresAt, now)}
              </span>
            )}
          </div>
          <p className="mt-3 text-[13px] leading-relaxed text-slate-200">{view.description}</p>
          <p className="mt-1 text-[12px] leading-relaxed text-slate-400">
            {RISK_EXPLANATION[view.riskLevel]}
          </p>

          <div className="mt-3 rounded bg-void/40 px-3 py-2">
            <p className="mb-1 font-mono text-[10px] uppercase tracking-[0.16em] text-slate-500">
              tool
            </p>
            <p className="font-mono text-[12px] text-accent-soft">{view.toolName}</p>
            <div className="mt-2 flex items-center justify-between gap-2 border-t border-line/60 pt-2">
              <KeyValue k="created" v={<span className="font-mono text-[11px]">{formatTimestamp(view.createdAt)}</span>} />
            </div>
            <KeyValue k="expires" v={<span className="font-mono text-[11px]">{formatTimestamp(view.expiresAt)}</span>} />
          </div>

          <div className="mt-3">
            <p className="mb-1.5 font-mono text-[10px] uppercase tracking-[0.16em] text-slate-500">
              exact bound arguments (read-only view · backend-enforced)
            </p>
            {argumentEntries.length === 0 ? (
              <p className="font-mono text-[11px] text-slate-500">none</p>
            ) : (
              <div className="rounded bg-void/40 px-3 py-2">
                {argumentEntries.map(([key, value]) => (
                  <p key={key} className="break-all font-mono text-[11px] leading-relaxed">
                    <span className="text-slate-500">{key}</span>:{" "}
                    <span className="text-slate-200">{stringifyValue(value, 220)}</span>
                  </p>
                ))}
              </div>
            )}
          </div>
        </div>

        <div className="flex w-full flex-col gap-2 lg:w-56">
          {pending ? (
            confirmKind === null ? (
              <div className="rounded-lg border border-line bg-hull/40 p-3">
                <p className="font-mono text-[10px] uppercase tracking-[0.14em] text-slate-500">
                  decisive action
                </p>
                <div className="mt-2 flex flex-col gap-2">
                  <Button
                    variant="primary"
                    disabled={acting !== null}
                    onClick={() => setConfirmKind("approve")}
                  >
                    Approve…
                  </Button>
                  <Button
                    variant="danger"
                    disabled={acting !== null}
                    onClick={() => setConfirmKind("reject")}
                  >
                    Reject…
                  </Button>
                </div>
                <p className="mt-2 text-[11px] leading-snug text-slate-500">
                  Approve executes the bound arguments exactly once. Reject closes the request.
                </p>
              </div>
            ) : (
              <div className="rounded-lg border border-accent/40 bg-accent/5 p-3 animate-fade-in-up">
                <p className="text-[12px] leading-relaxed text-slate-200">
                  {confirmKind === "approve"
                    ? "Approve and execute this exact request once?"
                    : "Reject this request? Nothing will be executed."}
                </p>
                <p className="mt-1 font-mono text-[10px] leading-snug text-slate-500">
                  {view.toolName} · {argumentEntries.length} argument(s)
                </p>
                <div className="mt-3 flex flex-wrap gap-2">
                  <Button
                    variant={confirmKind === "approve" ? "primary" : "danger"}
                    small
                    disabled={acting !== null}
                    onClick={() => {
                      const kind = confirmKind;
                      setConfirmKind(null);
                      if (kind) onConfirm(kind);
                    }}
                  >
                    {acting === confirmKind ? "Working…" : "Confirm"}
                  </Button>
                  <Button
                    variant="ghost"
                    small
                    disabled={acting !== null}
                    onClick={() => setConfirmKind(null)}
                  >
                    Cancel
                  </Button>
                </div>
              </div>
            )
          ) : (
            <div className="rounded-lg border border-line bg-hull/40 p-3">
              <p className="font-mono text-[10px] uppercase tracking-[0.14em] text-slate-500">
                terminal state
              </p>
              <p className="mt-1.5 text-[12px] leading-relaxed text-slate-400">
                {view.status === "EXECUTED"
                  ? "This approval ran exactly once. Its execution result is recorded."
                  : view.status === "APPROVED"
                    ? "Approved — the backend has consumed this single-use request."
                    : view.status === "REJECTED"
                      ? "Rejected — no execution happened."
                      : view.status === "EXPIRED"
                        ? "Expired — the request aged out unused, nothing ran."
                        : "This approval is no longer actionable."}
              </p>
            </div>
          )}

          {execution ? (
            <ExecutionResult execution={execution} />
          ) : null}
        </div>
      </div>
    </div>
  );
}
