/**
 * Authoritative plan detail card. Everything rendered comes verbatim from
 * GET /api/plans/{id}: `overall_status`, `ready_step_ids`, attention
 * lists and recorded results. The client NEVER decides which step is
 * requestable — the backend's ready list is the single source of truth.
 *
 * Step requests run through an explicit confirm ("Request… → Confirm");
 * recovery reconciliation requires acknowledging the uncertainty: the
 * backend resolves the step FAILED with execution_uncertain and cannot
 * prove whether the side effect occurred.
 */

import { useState } from "react";
import {
  PLAN_ERROR_CONSEQUENCES,
  type ApprovalRequestView,
  type PlanDetailStepView,
  type PlanDetailView,
  type PlanStepRequestOutcomeView,
} from "../../../../shared/ipc-contract";
import type { WorkspaceFailure } from "../../hooks/useApprovals";
import {
  formatTimestamp,
  overallStatusTone,
  OVERALL_STATUS_COPY,
  stepStatusTone,
  stringifyValue,
} from "../workspaces/meta";
import { SkeletonLines } from "../workspaces/skeleton";
import { workspaceDetail, workspaceHeadline } from "../workspaces/failures";
import { Badge, ErrorNote, SectionTitle } from "../primitives";
import { Button } from "../controls";
import { RefreshIcon } from "../icons";
import { cn } from "../../lib/cn";

function FailureNote({ failure }: { failure: WorkspaceFailure }) {
  return (
    <div className="mb-3 animate-fade-in-up">
      <ErrorNote detail={workspaceHeadline(failure)} />
      <p className="mt-1 text-[11px] leading-snug text-slate-500">
        {workspaceDetail(failure, PLAN_ERROR_CONSEQUENCES)}
      </p>
    </div>
  );
}

/** Recorded ToolResult rendered exactly as the backend stored it. */
function StepRecordedResult({ step }: { step: PlanDetailStepView }) {
  if (step.result === null) return null;
  const result = step.result;
  const dataEntries = result.data ? Object.entries(result.data) : [];
  return (
    <div
      className={cn(
        "mt-2 rounded-lg border px-3 py-2",
        result.success ? "border-ok/40 bg-ok/5" : "border-danger/45 bg-danger/5",
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={result.success ? "ok" : "danger"}>{result.status}</Badge>
        <span className="font-mono text-[10px] text-slate-500">recorded result</span>
        {result.errorCode ? <Badge tone="danger">{result.errorCode}</Badge> : null}
      </div>
      <p
        className={cn(
          "mt-1 text-[12px] leading-relaxed",
          result.success ? "text-ok" : "text-danger",
        )}
      >
        {result.message}
      </p>
      {dataEntries.length > 0 ? (
        <div className="mt-1.5 rounded bg-void/40 px-2.5 py-1.5">
          {dataEntries.map(([key, value]) => (
            <p key={key} className="font-mono text-[10px] text-slate-400">
              <span className="text-slate-500">{key}</span>: {stringifyValue(value)}
            </p>
          ))}
        </div>
      ) : null}
    </div>
  );
}

/** The 202 outcome card: a mutating step created a pending approval. */
function ApprovalRequiredCard({
  stepId,
  approval,
}: {
  stepId: string;
  approval: ApprovalRequestView;
}) {
  return (
    <div className="mt-2 rounded-lg border border-warn/45 bg-warn/5 px-3 py-2 animate-fade-in-up">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="warn">approval required</Badge>
        <span className="font-mono text-[10px] text-slate-500">
          step {stepId} · approval {approval.approvalId.slice(0, 8)}…
        </span>
      </div>
      <p className="mt-1 text-[12px] leading-relaxed text-warn">
        This step mutates, so nothing executed here. A single-use approval
        ({approval.riskLevel}) is now pending and expires{" "}
        {formatTimestamp(approval.expiresAt)} — decide it in the Approvals workspace.
      </p>
    </div>
  );
}

type StepConfirmKind = "request" | "resolve-recovery" | null;

function StepCard({
  step,
  isReady,
  busy,
  busyKind,
  confirm,
  onConfirmChange,
  onRequestStep,
  onResolveRecovery,
}: {
  step: PlanDetailStepView;
  /** Backend-derived: the step id is in the authoritative ready list. */
  isReady: boolean;
  busy: boolean;
  busyKind: "request" | "resolve-recovery" | null;
  confirm: StepConfirmKind;
  onConfirmChange: (kind: StepConfirmKind) => void;
  onRequestStep: () => void;
  onResolveRecovery: () => void;
}) {
  const awaitingApproval = step.status === "AWAITING_APPROVAL";
  const inRecovery = step.status === "RECOVERY_REQUIRED";
  const requestable = isReady && step.status === "PENDING";

  const title = step.description;
  return (
    <div className="rounded-lg border border-line bg-hull/40 p-3.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[11px] text-slate-200">{step.stepId}</span>
        <Badge tone={stepStatusTone(step.status)}>{step.status}</Badge>
        {isReady ? <Badge tone="ok">ready</Badge> : null}
        {step.approvalId ? (
          <span className="font-mono text-[10px] text-slate-500">
            approval {step.approvalId.slice(0, 8)}…
          </span>
        ) : null}
      </div>
      <p className="mt-1.5 text-[12.5px] leading-relaxed text-slate-300">{title}</p>

      <div className="mt-2 rounded bg-void/40 px-3 py-2">
        <p className="font-mono text-[10.5px] text-accent-soft">{step.toolName}</p>
        {Object.entries(step.arguments).length > 0 ? (
          <div className="mt-1.5">
            {Object.entries(step.arguments).map(([key, value]) => (
              <p key={key} className="break-all font-mono text-[10.5px] leading-relaxed">
                <span className="text-slate-500">{key}</span>:{" "}
                <span className="text-slate-300">{stringifyValue(value, 180)}</span>
              </p>
            ))}
          </div>
        ) : null}
        {step.dependsOn.length > 0 ? (
          <p className="mt-1.5 font-mono text-[10px] text-slate-500">
            depends on: {step.dependsOn.join(", ")}
          </p>
        ) : null}
      </div>

      <StepRecordedResult step={step} />

      {inRecovery ? (
        <div className="mt-2 rounded-lg border border-danger/45 bg-danger/5 px-3 py-2">
          <p className="text-[12px] leading-relaxed text-danger">
            This step is in recovery: a mutating execution ended uncertainly — the backend
            cannot prove whether the side effect happened. Resolving is a human decision: it
            finalizes the step FAILED (execution_uncertain). Verify any external effect
            manually afterwards; dependent steps stay blocked.
          </p>
        </div>
      ) : null}

      {awaitingApproval ? (
        <p className="mt-2 text-[11.5px] leading-relaxed text-warn">
          A pending single-use approval guards this step. Decide it in the Approvals
          workspace; the plan reconciles itself on the next read.
        </p>
      ) : null}

      <div className="mt-2.5 flex flex-wrap items-center gap-2">
        {requestable && confirm !== "request" ? (
          <Button variant="primary" small disabled={busy} onClick={() => onConfirmChange("request")}>
            Request…
          </Button>
        ) : null}
        {inRecovery && confirm !== "resolve-recovery" ? (
          <Button variant="danger" small disabled={busy} onClick={() => onConfirmChange("resolve-recovery")}>
            Resolve recovery…
          </Button>
        ) : null}
        {busy && busyKind !== null ? (
          <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-slate-500 animate-pulse-soft">
            {busyKind === "request" ? "requesting…" : "reconciling…"}
          </span>
        ) : null}
      </div>

      {confirm !== null ? (
        <div className="mt-2 rounded-lg border border-accent/40 bg-accent/5 px-3 py-2.5 animate-fade-in-up">
          <p className="text-[12px] leading-relaxed text-slate-200">
            {confirm === "request"
              ? "Send this step request? Read-only steps run once; mutating steps instead create a pending approval. One attempt — no automatic retry."
              : "Acknowledge the uncertainty of this step? The backend will finalize it FAILED (execution_uncertain) — this cannot prove or undo any side effect."}
          </p>
          <div className="mt-2 flex flex-wrap gap-2">
            <Button
              variant={confirm === "request" ? "primary" : "danger"}
              small
              disabled={busy}
              onClick={() => {
                if (confirm === "request") onRequestStep();
                else onResolveRecovery();
                onConfirmChange(null);
              }}
            >
              Confirm
            </Button>
            <Button variant="ghost" small disabled={busy} onClick={() => onConfirmChange(null)}>
              Cancel
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

export function PlanDetailCard({
  planId,
  view,
  loading,
  failure,
  actionFailure,
  busyStepId,
  busyKind,
  lastOutcome,
  lastOutcomeStepId,
  onRefresh,
  onRequestStep,
  onResolveRecovery,
}: {
  planId: string;
  view: PlanDetailView | null;
  loading: boolean;
  failure: WorkspaceFailure | null;
  actionFailure: WorkspaceFailure | null;
  busyStepId: string | null;
  busyKind: "request" | "resolve-recovery" | null;
  lastOutcome: PlanStepRequestOutcomeView | null;
  lastOutcomeStepId: string | null;
  onRefresh: () => void;
  onRequestStep: (stepId: string) => void;
  onResolveRecovery: (stepId: string) => void;
}) {
  const [confirmStep, setConfirmStep] = useState<{ stepId: string; kind: StepConfirmKind } | null>(
    null,
  );

  const header = (
    <div className="mb-3 flex items-center justify-between">
      <SectionTitle title="Plan" suffix={planId} />
      <Button variant="ghost" small onClick={onRefresh} disabled={loading || busyStepId !== null}>
        <RefreshIcon className={cn("h-3 w-3", loading && "animate-pulse-soft")} />
        Re-read
      </Button>
    </div>
  );

  if (loading && view === null) {
    return (
      <div>
        {header}
        <SkeletonLines lines={3} />
        <div className="mt-4 flex flex-col gap-3">
          <SkeletonLines lines={4} />
          <SkeletonLines lines={4} />
        </div>
      </div>
    );
  }

  if (failure) {
    return (
      <div>
        {header}
        <FailureNote failure={failure} />
        <div>
          <Button variant="ghost" small onClick={onRefresh} disabled={loading}>
            <RefreshIcon className="h-3 w-3" />
            Retry
          </Button>
        </div>
      </div>
    );
  }

  if (view === null) return <div>{header}</div>;

  const meta: string[] = [];
  if (view.createdAt) meta.push(`created ${formatTimestamp(view.createdAt)}`);
  if (view.awaitingApproval.length > 0) {
    meta.push(`awaiting approval: ${view.awaitingApproval.join(", ")}`);
  }
  if (view.recoveryRequired.length > 0) {
    meta.push(`recovery required: ${view.recoveryRequired.join(", ")}`);
  }

  return (
    <div>
      {header}

      {/* Verbatim overall status banner — backend derivation, not client opinion. */}
      <div
        className={cn(
          "mb-3 flex flex-wrap items-center gap-2 rounded-lg border px-3 py-2",
          overallStatusTone(view.overallStatus) === "ok" && "border-ok/40 bg-ok/5",
          overallStatusTone(view.overallStatus) === "warn" && "border-warn/40 bg-warn/5",
          overallStatusTone(view.overallStatus) === "danger" && "border-danger/45 bg-danger/5",
          overallStatusTone(view.overallStatus) === "idle" && "border-line bg-hull/40",
        )}
      >
        <Badge tone={overallStatusTone(view.overallStatus)}>{view.overallStatus}</Badge>
        <p className="text-[12px] leading-relaxed text-slate-300">
          {OVERALL_STATUS_COPY[view.overallStatus]}
        </p>
        {view.needsAttention ? <Badge tone="danger">needs attention</Badge> : null}
      </div>

      {actionFailure ? <FailureNote failure={actionFailure} /> : null}

      {meta.length > 0 ? (
        <div className="mb-3 flex flex-col gap-0.5">
          {meta.map((line) => (
            <p key={line} className="font-mono text-[10.5px] text-slate-500">
              {line}
            </p>
          ))}
        </div>
      ) : null}

      <div className="flex flex-col gap-3">
        {view.steps.map((step) => {
          const isBusy = busyStepId === step.stepId;
          const confirm =
            confirmStep !== null && confirmStep.stepId === step.stepId
              ? confirmStep.kind
              : null;
          return (
            <StepCard
              key={step.stepId}
              step={step}
              isReady={view.readyStepIds.includes(step.stepId)}
              busy={busyStepId !== null}
              busyKind={isBusy ? busyKind : null}
              confirm={confirm}
              onConfirmChange={(kind) =>
                setConfirmStep(kind === null ? null : { stepId: step.stepId, kind })
              }
              onRequestStep={() => onRequestStep(step.stepId)}
              onResolveRecovery={() => onResolveRecovery(step.stepId)}
            />
          );
        })}
      </div>

      {lastOutcome !== null && lastOutcomeStepId !== null ? (
        lastOutcome.kind === "executed" ? (
          <div className="mt-3 rounded-lg border border-ok/40 bg-ok/5 px-3 py-2.5 animate-fade-in-up">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={lastOutcome.execution.success ? "ok" : "danger"}>
                {lastOutcome.execution.status}
              </Badge>
              <span className="font-mono text-[10px] text-slate-500">
                step {lastOutcomeStepId} · {lastOutcome.execution.toolName}
              </span>
            </div>
            <p className="mt-1 text-[12px] leading-relaxed text-slate-300">
              {lastOutcome.execution.message}
            </p>
            {lastOutcome.execution.data
              ? Object.entries(lastOutcome.execution.data).map(([key, value]) => (
                  <p key={key} className="mt-0.5 font-mono text-[10px] text-slate-400">
                    <span className="text-slate-500">{key}</span>: {stringifyValue(value)}
                  </p>
                ))
              : null}
          </div>
        ) : (
          <ApprovalRequiredCard stepId={lastOutcomeStepId} approval={lastOutcome.approval} />
        )
      ) : null}

      <p className="mt-3 text-[11px] leading-snug text-slate-500">
        Step requests and recovery acknowledgements each make exactly one backend attempt.
        Readiness above comes from the backend's <span className="font-mono">ready_step_ids</span> —
        nothing is calculated here.
      </p>
    </div>
  );
}
