/** Bounded data-map table for tool results (GET /api/system-info). */

import { Badge, KeyValue, Panel, SectionTitle, ErrorNote } from "./primitives";
import { RefreshIcon } from "./icons";
import { Button } from "./controls";
import { errorCopy } from "../services/bridge";
import type { IpcResult, SystemInfoSummary } from "../../../shared/ipc-contract";

export function SystemInfoCard({
  result,
  pending,
  loaded,
  onLoad,
}: {
  result: IpcResult<SystemInfoSummary> | null;
  pending: boolean;
  loaded: boolean;
  onLoad: () => void;
}) {
  return (
    <Panel>
      <div className="mb-4 flex items-center justify-between">
        <SectionTitle title="System information" suffix="get_system_info · read-only tool" />
        <Button variant="ghost" small disabled={pending} onClick={onLoad}>
          <RefreshIcon className="h-3 w-3" />
          {loaded ? "Refresh" : "Load"}
        </Button>
      </div>

      {result === null ? (
        <p className="px-1 py-4 font-mono text-[12px] text-slate-500">
          {pending ? "Querying tool boundary…" : "Press Load to query the tool boundary."}
        </p>
      ) : result.ok ? (
        <div className="animate-fade-in-up">
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <Badge tone={result.data.success ? "ok" : "danger"}>{result.data.status}</Badge>
            <Badge tone="idle">{result.data.toolName}</Badge>
          </div>
          <p className="mb-3 text-[13px] text-slate-300">{result.data.message}</p>
          {result.data.data && Object.keys(result.data.data).length > 0 ? (
            <div className="panel-inner px-4 py-2">
              {Object.entries(result.data.data).map(([k, v]) => (
                <KeyValue key={k} k={k} v={v} />
              ))}
            </div>
          ) : null}
        </div>
      ) : (
        <div className="animate-fade-in-up">
          <ErrorNote detail={errorCopy(result.code, "System info request failed.")} />
        </div>
      )}
    </Panel>
  );
}
