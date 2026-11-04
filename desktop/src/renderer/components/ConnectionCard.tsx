/**
 * The live backend connection card (System status view).
 * Three honest states only: checking / connected / failed.
 */

import {
  Badge,
  KeyValue,
  Panel,
  SectionTitle,
  ErrorNote,
} from "./primitives";
import { RefreshIcon } from "./icons";
import { Button } from "./controls";
import type { HealthCheck, IpcResult } from "../../../shared/ipc-contract";
import { errorCopy } from "../services/bridge";

export function ConnectionCard({
  health,
  pending,
  onRefresh,
}: {
  health: IpcResult<HealthCheck> | null;
  pending: boolean;
  onRefresh: () => void;
}) {
  return (
    <Panel>
      <div className="mb-4 flex items-center justify-between">
        <SectionTitle title="Backend connection" suffix="/api/health" />
        <Button variant="ghost" small disabled={pending} onClick={onRefresh}>
          <RefreshIcon className="h-3 w-3" />
          Check now
        </Button>
      </div>

      {health === null ? (
        <div className="flex items-center gap-3 px-1 py-8">
          <span aria-hidden className="h-2 w-2 rounded-full bg-accent animate-pulse-soft" />
          <span className="font-mono text-[12px] text-slate-500">Establishing connection…</span>
        </div>
      ) : health.ok ? (
        <div className="animate-fade-in-up">
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <Badge tone="ok">Connected</Badge>
            <Badge tone="idle">{health.data.snapshot.status}</Badge>
            <Badge tone="idle">{health.data.latencyMs} ms</Badge>
          </div>
          <KeyValue k="Service" v={health.data.snapshot.name} />
          <KeyValue k="Backend version" v={health.data.snapshot.version} />
          <KeyValue
            k="Last check"
            v={new Date(health.data.checkedAt).toLocaleTimeString()}
          />
        </div>
      ) : (
        <div className="animate-fade-in-up">
          <div className="mb-3">
            <Badge tone="danger">Unreachable</Badge>
            <span className="ml-3 font-mono text-[10px] uppercase tracking-[0.14em] text-slate-500">
              {pending ? "retrying…" : "poll paused"}
            </span>
          </div>
          <ErrorNote
            detail={[
              errorCopy(health.code, "Backend connection failed."),
              health.code === "HTTP_STATUS" ? ` (${health.message})` : "",
            ]
              .filter(Boolean)
              .join(" ")}
          />
        </div>
      )}
    </Panel>
  );
}
