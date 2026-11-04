/**
 * Application shell (Phase 6.1): dark HUD framework with sidebar
 * navigation, scrollable workspace, and a live right-side context
 * panel. Every control on screen performs a real action.
 */

import { useState } from "react";
import { NavButton, Button } from "./components/controls";
import {
  ActivityIcon,
  ChatIcon,
  ShieldIcon,
  PlansIcon,
  MenuIcon,
  PanelRightIcon,
  CloseIcon,
  RefreshIcon,
} from "./components/icons";
import { Badge, KeyValue, Panel, SectionTitle } from "./components/primitives";
import { ChatView } from "./views/ChatView";
import { ApprovalsView } from "./views/ApprovalsView";
import { PlansView } from "./views/PlansView";
import { SystemStatusView } from "./views/SystemStatusView";
import { useBackendMonitor, type BackendMonitor } from "./hooks/useBackendMonitor";
import { cn } from "./lib/cn";
import type { BackendStatus, HealthCheck, IpcResult } from "../../shared/ipc-contract";

type ViewId = "system" | "chat" | "approvals" | "plans";

const NAV: ReadonlyArray<{
  id: ViewId;
  label: string;
  icon: React.ReactNode;
}> = [
  { id: "system", label: "System", icon: <ActivityIcon className="h-4 w-4" /> },
  { id: "chat", label: "Console", icon: <ChatIcon className="h-4 w-4" /> },
  { id: "approvals", label: "Approvals", icon: <ShieldIcon className="h-4 w-4" /> },
  { id: "plans", label: "Plans", icon: <PlansIcon className="h-4 w-4" /> },
];

function BrandMark() {
  return (
    <div className="flex items-center gap-3 px-3">
      <span
        aria-hidden
        className="relative flex h-9 w-9 items-center justify-center rounded-lg border border-accent/30 bg-accent/5 shadow-glow-sm"
      >
        <span className="h-2 w-2 rounded-full bg-accent animate-pulse-soft" />
      </span>
      <span>
        <span className="block font-mono text-[13px] tracking-[0.3em] text-slate-100">JARVIS</span>
        <span className="block font-mono text-[9px] uppercase tracking-[0.24em] text-slate-500">
          Phase 6.1 · Foundation
        </span>
      </span>
    </div>
  );
}

function SidebarNav({
  view,
  onSelect,
  className,
}: {
  view: ViewId;
  onSelect: (id: ViewId) => void;
  className?: string;
}) {
  return (
    <nav aria-label="Primary" className={cn("flex w-full flex-col gap-1", className)}>
      {NAV.map((item) => (
        <NavButton
          key={item.id}
          label={item.label}
          icon={item.icon}
          active={view === item.id}
          onClick={() => onSelect(item.id)}
        />
      ))}
    </nav>
  );
}

function ContextPanel({ monitor }: { monitor: BackendMonitor }) {
  return (
    <div className="flex flex-col gap-4">
      <Panel>
        <div className="mb-3 flex items-center justify-between">
          <SectionTitle title="Live status" suffix="30s poll" />
          <Button
            variant="ghost"
            small
            onClick={monitor.refresh}
            disabled={monitor.pending}
          >
            <RefreshIcon className="h-3 w-3" />
            Sync
          </Button>
        </div>
        <MonitorPillGroup status={monitor.status} health={monitor.health} />
      </Panel>

      <Panel>
        <SectionTitle title="Session" suffix="per-process" />
        <StatusKeys status={monitor.status} />
      </Panel>

      <Panel>
        <SectionTitle title="Contract reference" suffix="informational" />
        <ul className="space-y-1.5 font-mono text-[11px] leading-relaxed text-slate-500">
          <li>· message ≤ 4,000 chars</li>
          <li>· replay ≤ 24 turns / 12,000 chars</li>
          <li>· plans ≤ 8 steps · args ≤ 32 items</li>
          <li>· approvals single-use · TTL-bound</li>
        </ul>
      </Panel>
    </div>
  );
}

/** Live pills driven by the shared monitor (no duplicate polling). */
function MonitorPillGroup({
  status,
  health,
}: {
  status: IpcResult<BackendStatus> | null;
  health: IpcResult<HealthCheck> | null;
}) {
  const pills: Array<{ tone: "ok" | "warn" | "danger" | "idle"; label: string }> = [];
  if (health === null) pills.push({ tone: "idle", label: "checking" });
  else if (health.ok) {
    pills.push({ tone: "ok", label: "backend up" });
    pills.push({ tone: "idle", label: `${health.data.latencyMs} ms` });
  } else pills.push({ tone: "danger", label: "backend down" });
  if (status?.ok === true && !status.data.tokenConfigured) {
    pills.push({ tone: "warn", label: "token unconfigured" });
  }
  return (
    <div className="flex flex-wrap gap-2">
      {pills.map((pill) => (
        <Badge key={pill.label} tone={pill.tone}>
          {pill.label}
        </Badge>
      ))}
    </div>
  );
}

function StatusKeys({ status }: { status: IpcResult<BackendStatus> | null }) {
  if (status?.ok !== true) return <p className="font-mono text-[12px] text-slate-500">loading…</p>;
  return (
    <div>
      <KeyValue k="Origin" v={status.data.apiOrigin} />
      <KeyValue k="Session" v={status.data.sessionIdMasked} />
      <KeyValue
        k="Token"
        v={status.data.tokenConfigured ? "configured" : "missing"}
      />
    </div>
  );
}

export function App() {
  const [view, setView] = useState<ViewId>("system");
  const [contextOpen, setContextOpen] = useState(true);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const monitor = useBackendMonitor();

  const select = (id: ViewId) => {
    setView(id);
    setDrawerOpen(false);
  };

  return (
    <div className="relative flex h-full min-h-0 w-full overflow-hidden">
      <div aria-hidden className="hud-backdrop" />

      {/* Sidebar (hidden below md; drawer overlay when toggled) */}
      <aside
        className={cn(
          "hidden h-full w-[248px] shrink-0 flex-col border-r border-line/70 bg-abyss/60 p-4 md:flex",
        )}
      >
        <BrandMark />
        <div className="mt-6">
          <SidebarNav view={view} onSelect={select} />
        </div>
        <div className="mt-auto px-3 font-mono text-[10px] uppercase tracking-[0.18em] text-slate-600">
          local API · loopback only
        </div>
      </aside>

      {drawerOpen ? (
        <div className="fixed inset-0 z-40 flex md:hidden">
          <button
            type="button"
            aria-label="Close navigation"
            onClick={() => setDrawerOpen(false)}
            className="h-full w-full bg-void/70 backdrop-blur-sm"
          />
          <aside className="h-full w-64 shrink-0 border-r border-line bg-abyss p-4">
            <div className="flex items-center justify-between">
              <BrandMark />
              <button
                type="button"
                aria-label="Close navigation"
                onClick={() => setDrawerOpen(false)}
                className="focus-ring rounded p-1 text-slate-400 hover:text-accent-soft"
              >
                <CloseIcon className="h-4 w-4" />
              </button>
            </div>
            <div className="mt-6">
              <SidebarNav view={view} onSelect={select} />
            </div>
          </aside>
        </div>
      ) : null}

      {/* Workspace column */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-3 border-b border-line/70 px-4 py-3.5">
          <button
            type="button"
            aria-label="Open navigation"
            aria-expanded={drawerOpen}
            onClick={() => setDrawerOpen(true)}
            className={cn(
              "focus-ring rounded-lg border border-line p-2 text-slate-300 hover:border-accent/40 hover:text-accent-soft md:hidden",
            )}
          >
            <MenuIcon className="h-4 w-4" />
          </button>
          <h1 className="font-mono text-[11px] uppercase tracking-[0.3em] text-slate-400">
            {NAV.find((item) => item.id === view)?.label}
          </h1>
          <div className="ml-auto">
            <button
              type="button"
              aria-label="Toggle context panel"
              aria-pressed={contextOpen}
              onClick={() => setContextOpen((open) => !open)}
              className={cn(
                "focus-ring rounded-lg border p-2 transition-colors",
                contextOpen
                  ? "border-accent/40 text-accent-soft"
                  : "border-line text-slate-400 hover:border-accent/40 hover:text-accent-soft",
              )}
            >
              <PanelRightIcon className="h-4 w-4" />
            </button>
          </div>
        </header>

        <main className="scroll-hud min-h-0 flex-1 p-6">
          {view === "system" ? (
            <SystemStatusView monitor={monitor} />
          ) : view === "chat" ? (
            <ChatView health={monitor.health} />
          ) : view === "approvals" ? (
            <ApprovalsView health={monitor.health} />
          ) : (
            <PlansView health={monitor.health} />
          )}
        </main>
      </div>

      {/* Right context panel (hidden below xl; toggle always available) */}
      {contextOpen ? (
        <aside className="hidden h-full w-[320px] shrink-0 flex-col overflow-y-auto border-l border-line/70 bg-abyss/40 p-4 xl:flex">
          <ContextPanel monitor={monitor} />
        </aside>
      ) : null}
    </div>
  );
}
