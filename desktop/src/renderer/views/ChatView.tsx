/** Assistant console placeholder — real scope arrives in Phase 6.2. */

import { EmptyState, PhaseNote } from "../components/states";
import { Panel, SectionTitle } from "../components/primitives";
import type { HealthCheck, IpcResult } from "../../../shared/ipc-contract";

const CHAT_CONTRACT = ['POST /api/chat → { status: success | action_proposed | plan_proposed }'];

export function ChatView({ health }: { health: IpcResult<HealthCheck> | null }) {
  const connected = health?.ok === true;

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-5">
      <EmptyState title="Assistant console · Phase 6.2">
        The chat turn pipeline is not part of this foundation build. The full backend contract is
        already typed in <code className="font-mono text-accent-soft">schema.gen.ts</code>; when
        turns arrive they will render here with the same policy and approval boundaries you see on
        the backend.
      </EmptyState>

      <Panel>
        <SectionTitle title="Connection" suffix="live" />
        <p className="text-[13px] text-slate-400">
          {connected
            ? "Backend reachable — chat can be wired as soon as Phase 6.2 lands."
            : health === null
              ? "Checking backend reachability…"
              : "Backend unreachable — start the backend, then check System."}
        </p>
        <div className="mt-3">
          <PhaseNote phase="Phase 6.2">{CHAT_CONTRACT[0]}</PhaseNote>
        </div>
      </Panel>
    </div>
  );
}
