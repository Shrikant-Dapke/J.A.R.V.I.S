/** Approval workspace placeholder — the approval flow arrives Phase 6.2. */

import { EmptyState, PhaseNote } from "../components/states";
import { Panel, SectionTitle } from "../components/primitives";
import type { HealthCheck, IpcResult } from "../../../shared/ipc-contract";

const APPROVAL_CONTRACT = [
  "GET /api/approvals — inventory (metadata only)",
  "GET /api/approvals/{id} — authoritative state, TTL expiry",
  "POST /api/approvals/{id}/approve — execute exactly once",
  "POST /api/approvals/{id}/reject — reject pending",
];

export function ApprovalsView({ health }: { health: IpcResult<HealthCheck> | null }) {
  const connected = health?.ok === true;

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-5">
      <EmptyState title="Pending approvals · Phase 6.2">
        Approval decisions stay backend-enforced: any proposed action binds exact arguments, expires
        on its own, and executes once. This workspace will surface pending approvals from the
        approval inventory as soon as the flow is wired.
      </EmptyState>

      <Panel>
        <SectionTitle title="Connection" suffix="live" />
        <p className="text-[13px] text-slate-400">
          {connected
            ? "Backend reachable — approval flow lands with Phase 6.2."
            : health === null
              ? "Checking backend reachability…"
              : "Backend unreachable — approval actions cannot run without the local API."}
        </p>
        <div className="mt-3 space-y-2">
          {APPROVAL_CONTRACT.map((line) => (
            <PhaseNote key={line} phase="Contract">
              {line}
            </PhaseNote>
          ))}
        </div>
      </Panel>
    </div>
  );
}
