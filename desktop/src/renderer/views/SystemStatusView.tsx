/** System status: live backend connection + local identity + system info tool. */

import { ConnectionCard } from "../components/ConnectionCard";
import { SystemInfoCard } from "../components/SystemInfoCard";
import { Badge, KeyValue, Panel, SectionTitle } from "../components/primitives";
import type { BackendMonitor } from "../hooks/useBackendMonitor";
import { useSystemInfo } from "../hooks/useSystemInfo";

export function SystemStatusView({ monitor }: { monitor: BackendMonitor }) {
  const { status, health, pending, refresh } = monitor;
  const tokenConfigured = status?.ok === true ? status.data.tokenConfigured : false;

  const systemInfo = useSystemInfo(tokenConfigured);

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-5">
      <ConnectionCard health={health} pending={pending} onRefresh={refresh} />

      <Panel>
        <SectionTitle title="Local identity" suffix="main process only" />
        {status === null ? (
          <p className="px-1 py-3 font-mono text-[12px] text-slate-500">Loading configuration…</p>
        ) : status.ok ? (
          <div className="animate-fade-in-up">
            <div className="mb-3 flex flex-wrap items-center gap-2">
              {status.data.tokenConfigured ? (
                <Badge tone="ok">Token configured</Badge>
              ) : (
                <Badge tone="warn">Token unconfigured</Badge>
              )}
              <Badge tone="idle">{status.data.runtime.electron} · Electron</Badge>
              <Badge tone="idle">Node {status.data.runtime.node}</Badge>
            </div>
            <KeyValue k="Backend origin" v={status.data.apiOrigin} />
            <KeyValue
              k="API token"
              v={
                status.data.tokenConfigured
                  ? `configured (${status.data.tokenSource ?? "unknown"} source)`
                  : "not configured — protected routes are unavailable"
              }
            />
            <KeyValue k="Session id" v={status.data.sessionIdMasked} />
            <KeyValue k="Desktop shell" v={`v${status.data.appVersion}`} />
          </div>
        ) : (
          <p className="px-1 py-3 font-mono text-[12px] text-danger">
            Identity configuration failed to load.
          </p>
        )}
      </Panel>

      {tokenConfigured ? (
        <SystemInfoCard
          result={systemInfo.result}
          pending={systemInfo.pending}
          loaded={systemInfo.loaded}
          onLoad={systemInfo.load}
        />
      ) : (
        <Panel>
          <SectionTitle title="System information" suffix="auth-gated" />
          <p className="text-[13px] leading-relaxed text-slate-400">
            The <code className="font-mono text-accent-soft">get_system_info</code> tool requires a
            configured backend token. Set <code className="font-mono text-accent-soft">JARVIS_API_TOKEN</code>
            (≥ 32 chars, same source the backend reads) and restart the app to enable this card.
          </p>
        </Panel>
      )}
    </div>
  );
}
