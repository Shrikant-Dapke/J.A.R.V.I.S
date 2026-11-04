/** Plans workspace placeholder — step execution arrives Phase 6.2. */

import { EmptyState, PhaseNote } from "../components/states";
import { Panel, SectionTitle } from "../components/primitives";
import type { HealthCheck, IpcResult } from "../../../shared/ipc-contract";

const PLAN_CONTRACT = [
  "GET /api/plans — advisory inventory, attention flags",
  "GET /api/plans/{id} — authoritative read, ready steps",
  "POST /api/plans/{id}/steps/{step}/request — one step per call",
  "POST /api/plans/{id}/steps/{step}/resolve-recovery — human decision",
];

export function PlansView({ health }: { health: IpcResult<HealthCheck> | null }) {
  const connected = health?.ok === true;

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-5">
      <EmptyState title="Task plans · Phase 6.2">
        Plans are captured by the orchestrator and execute one explicit step at a time through
        policy and approval. This workspace will list plans, highlight recovery-required and
        awaiting-approval steps, and expose the manual step request controls.
      </EmptyState>

      <Panel>
        <SectionTitle title="Connection" suffix="live" />
        <p className="text-[13px] text-slate-400">
          {connected
            ? "Backend reachable — plans workspace lands with Phase 6.2."
            : health === null
              ? "Checking backend reachability…"
              : "Backend unreachable — start the backend to inspect plans."}
        </p>
        <div className="mt-3 space-y-2">
          {PLAN_CONTRACT.map((line) => (
            <PhaseNote key={line} phase="Contract">
              {line}
            </PhaseNote>
          ))}
        </div>
      </Panel>
    </div>
  );
}
