/** Shared tone/format helpers for the approval + plan workspaces. */

import type {
  ApprovalStatusView,
  PlanOverallStatusView,
  PlanStepStatusView,
  RiskLevelView,
} from "../../../../shared/ipc-contract";

export type Tone = "ok" | "warn" | "danger" | "idle";

/** Human one-liners for each risk level (honest, no fear-mongering). */
export const RISK_EXPLANATION: Record<RiskLevelView, string> = {
  READ_ONLY: "No mutation — this tool only reads or queries.",
  LOW_RISK: "Mutating but immediately local — side effects are small and near the workspace.",
  HIGH_RISK: "Mutating with persistent impact — think before you approve.",
};

export function riskTone(risk: RiskLevelView | string): Tone {
  if (risk === "READ_ONLY") return "idle";
  if (risk === "LOW_RISK") return "warn";
  return "danger";
}

export function approvalStatusTone(status: ApprovalStatusView | string): Tone {
  if (status === "APPROVED" || status === "EXECUTED") return "ok";
  if (status === "PENDING") return "warn";
  if (status === "EXPIRED") return "danger";
  return "idle"; // REJECTED
}

export function stepStatusTone(status: PlanStepStatusView | string): Tone {
  if (status === "COMPLETED" || status === "EXECUTED") return "ok";
  if (status === "PENDING") return "idle";
  if (status === "AWAITING_APPROVAL") return "warn";
  return "danger"; // RECOVERY_REQUIRED, FAILED
}

export function overallStatusTone(status: PlanOverallStatusView | string): Tone {
  if (status === "actionable" || status === "completed") return "ok";
  if (status === "in_progress" || status === "awaiting_approval") return "warn";
  if (status === "recovery_required" || status === "failed") return "danger";
  return "idle";
}

export const OVERALL_STATUS_COPY: Record<PlanOverallStatusView, string> = {
  recovery_required: "Recovery required — an uncertain step needs your acknowledgement",
  awaiting_approval: "Awaiting approval — a step is waiting for your decision",
  actionable: "Actionable — ready steps can be requested now",
  failed: "Failed — a terminal step recorded a failure",
  in_progress: "In progress — steps are moving through policy and approval",
  completed: "Completed — every step resolved",
};

export function formatTimestamp(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString([], {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** TTL countdown text, recalculated by the caller-passed now tick. */
export function expiryText(expiresAt: string, now: number): string {
  const expiry = new Date(expiresAt).getTime();
  if (Number.isNaN(expiry)) return "expires unknown";
  const deltaMs = expiry - now;
  if (deltaMs <= 0) return "expired";
  const minutes = Math.floor(deltaMs / 60_000);
  const seconds = Math.floor((deltaMs % 60_000) / 1000);
  if (minutes > 0) return `expires in ${minutes}m ${String(seconds).padStart(2, "0")}s`;
  return `expires in ${seconds}s`;
}

/** Bounded stringification for argument values. */
export function stringifyValue(value: unknown, limit = 160): string {
  let text: string;
  if (typeof value === "string") text = value;
  else {
    try {
      text = JSON.stringify(value) ?? "…";
    } catch {
      text = "…";
    }
  }
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}
