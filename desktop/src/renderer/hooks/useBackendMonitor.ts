/** Shared backend monitor: status + health with honest pending state. */

import { useCallback, useEffect, useRef, useState } from "react";
import type { BackendStatus, HealthCheck, IpcResult } from "../../../shared/ipc-contract";
import { fetchBackendStatus, fetchHealthCheck } from "../services/bridge";

export type BackendMonitor = {
  status: IpcResult<BackendStatus> | null;
  health: IpcResult<HealthCheck> | null;
  pending: boolean;
  /** Manually refresh both status and health (button entry point). */
  refresh: () => void;
};

const POLL_INTERVAL_MS = 30_000;

export function useBackendMonitor(pollMs: number = POLL_INTERVAL_MS): BackendMonitor {
  const [status, setStatus] = useState<IpcResult<BackendStatus> | null>(null);
  const [health, setHealth] = useState<IpcResult<HealthCheck> | null>(null);
  const [pending, setPending] = useState(false);
  const inFlight = useRef(false);

  const refresh = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setPending(true);
    try {
      // Status first (cheap, local), then the wire check.
      const nextStatus = await fetchBackendStatus();
      const nextHealth = await fetchHealthCheck();
      setStatus(nextStatus);
      setHealth(nextHealth);
    } catch (error) {
      // Bridge failures are fatal precondition errors; surface as a
      // failed health so the UI shows the connection state honestly.
      setHealth({
        ok: false,
        code: "UNEXPECTED",
        message: error instanceof Error ? error.message : String(error),
      });
    } finally {
      inFlight.current = false;
      setPending(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), pollMs);
    return () => window.clearInterval(timer);
  }, [refresh, pollMs]);

  return { status, health, pending, refresh: () => void refresh() };
}
