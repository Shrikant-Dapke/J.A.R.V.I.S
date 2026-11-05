/**
 * Fully functional Approvals workspace (Phase 6.3 Part A) over the typed
 * IPC bridge. The listing is metadata-only; selecting an approval fetches
 * the authoritative detail (exact arguments, risk, TTL). Decisive actions
 * confirm against a FRESH authoritative read before one single backend
 * attempt — never retried, never duplicated, with honest 404/409/422
 * handling.
 */

import { useCallback, useEffect, useState } from "react";
import { ApprovalDetailCard } from "../components/approvals/ApprovalDetailCard";
import { ApprovalList } from "../components/approvals/ApprovalList";
import { CloseIcon, MenuIcon } from "../components/icons";
import { Badge } from "../components/primitives";
import {
  useApprovalDecision,
  useApprovalDetail,
  useApprovalList,
} from "../hooks/useApprovals";
import { fetchApproval } from "../services/bridge";
import type { BackendMonitor } from "../hooks/useBackendMonitor";
import { cn } from "../lib/cn";

function ConnectionBadge({ monitor }: { monitor: BackendMonitor }) {
  if (monitor.health === null) return <Badge tone="idle">checking…</Badge>;
  if (monitor.health.ok) {
    return <Badge tone="ok">backend up · {monitor.health.data.latencyMs} ms</Badge>;
  }
  return <Badge tone="danger">backend offline</Badge>;
}

export function ApprovalsView({ monitor }: { monitor: BackendMonitor }) {
  const book = useApprovalList();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [railOpen, setRailOpen] = useState(false);

  const detail = useApprovalDetail(selectedId);

  /**
   * Decisive actions rest on a fresh authoritative read: the POST goes
   * out only if this very read returns ok (it also records the failure
   * in the detail hook for display).
   */
  const decision = useApprovalDecision(
    useCallback(
      async (approvalId: string) => {
        const result = await fetchApproval({ approvalId });
        return result.ok;
      },
      [],
    ),
    // After every attempt both surfaces converge to the backend's state.
    useCallback(() => {
      book.load();
      detail.refresh();
    }, [book, detail]),
  );

  useEffect(() => {
    if (decision.execution !== null) {
      book.load();
      detail.refresh();
    }
  }, [decision.execution, book, detail]);

  const select = (approvalId: string) => {
    decision.clearFailure();
    decision.clearExecution();
    setSelectedId(approvalId);
    setRailOpen(false);
  };

  const railProps = {
    items: book.items,
    total: book.total,
    loading: book.loading,
    loaded: book.loaded,
    failure: book.failure,
    selectedId,
    locked: decision.acting !== null,
    onRefresh: book.load,
    onSelect: select,
  };

  return (
    <div className="flex h-full min-h-0 w-full">
      {/* Approval rail — permanent on md+, drawer overlay below. */}
      <aside className="hidden h-full w-[290px] shrink-0 border-r border-line/70 bg-abyss/50 p-4 md:flex md:flex-col md:min-h-0">
        <ApprovalList {...railProps} />
      </aside>

      {railOpen ? (
        <div className="fixed inset-0 z-40 flex md:hidden">
          <button
            type="button"
            aria-label="Close approvals"
            onClick={() => setRailOpen(false)}
            className="h-full flex-1 bg-void/70 backdrop-blur-sm"
          />
          <aside className="flex h-full w-[300px] shrink-0 flex-col border-r border-line bg-abyss p-4 md:min-h-0"          >
            <div className="mb-2 flex items-center justify-between">
              <span className="font-mono text-[11px] uppercase tracking-[0.24em] text-slate-300">
                Approvals
              </span>
              <button
                type="button"
                aria-label="Close approvals"
                onClick={() => setRailOpen(false)}
                className="focus-ring rounded p-1 text-slate-400 hover:text-accent-soft"
              >
                <CloseIcon className="h-4 w-4" />
              </button>
            </div>
            <ApprovalList {...railProps} />
          </aside>
        </div>
      ) : null}

      {/* Decision column */}
      <section className="flex h-full min-h-0 min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-3 border-b border-line/70 bg-abyss/20 px-4 py-2.5">
          <button
            type="button"
            aria-label="Open approvals"
            aria-expanded={railOpen}
            onClick={() => setRailOpen(true)}
            className={cn(
              "focus-ring rounded-lg border border-line p-2 text-slate-300 hover:border-accent/40 hover:text-accent-soft md:hidden",
            )}
          >
            <MenuIcon className="h-4 w-4" />
          </button>
          <h2 className="min-w-0 truncate font-mono text-[11px] uppercase tracking-[0.24em] text-slate-300">
            {selectedId === null ? "Select an approval" : `Approval · ${selectedId.slice(0, 8)}…`}
          </h2>
          <div className="ml-auto hidden sm:block">
            <ConnectionBadge monitor={monitor} />
          </div>
        </header>

        <div className="scroll-hud min-h-0 flex-1 overflow-y-auto p-4 md:p-6">
          <div className="mx-auto max-w-3xl">
            {selectedId === null ? (
              <div className="rounded-lg border border-dashed border-line/90 bg-hull/30 px-8 py-14 text-center">
                <h4 className="font-mono text-[12px] uppercase tracking-[0.22em] text-accent-soft">
                  Pending approvals
                </h4>
                <p className="mx-auto mt-3 max-w-md text-[13px] leading-relaxed text-slate-400">
                  Every decisive action here is backend-enforced: exact arguments, a hard expiry,
                  and exactly one execution. Pick an approval from the list to read its
                  authoritative state.
                </p>
              </div>
            ) : (
              <ApprovalDetailCard
                approvalId={selectedId}
                view={detail.view}
                loading={detail.loading}
                failure={detail.failure}
                acting={decision.acting}
                execution={decision.execution}
                actionFailure={decision.failure}
                onRefresh={detail.refresh}
                onConfirm={(kind) => decision.act(kind, selectedId)}
                /* fresh = the displayed snapshot IS the latest authoritative read
                   (no un-refreshed decision state left around). */
                isPending={
                  detail.view?.status === "PENDING" && decision.failure === null && decision.acting === null
                }
              />
            )}
          </div>
        </div>
      </section>
    </div>
  );
}
