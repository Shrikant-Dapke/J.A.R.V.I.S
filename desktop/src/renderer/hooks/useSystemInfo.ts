/** Auth-gated GET /api/system-info consumer (real IPC; no mock data). */

import { useCallback, useEffect, useState } from "react";
import type { IpcResult, SystemInfoSummary } from "../../../shared/ipc-contract";
import { fetchSystemInfo } from "../services/bridge";

export type SystemInfoState = {
  result: IpcResult<SystemInfoSummary> | null;
  pending: boolean;
  loaded: boolean;
  load: () => void;
};

export function useSystemInfo(enabled: boolean): SystemInfoState {
  const [result, setResult] = useState<IpcResult<SystemInfoSummary> | null>(null);
  const [pending, setPending] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    if (!enabled) return;
    setPending(true);
    try {
      const next = await fetchSystemInfo();
      setResult(next);
      setLoaded(true);
    } finally {
      setPending(false);
    }
  }, [enabled]);

  useEffect(() => {
    if (enabled) void load();
  }, [enabled, load]);

  return { result, pending, loaded, load: () => void load() };
}
