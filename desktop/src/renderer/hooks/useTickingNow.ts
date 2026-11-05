/** Shared ticking clock for TTL countdowns (approvals, plan statuses). */

import { useEffect, useState } from "react";

export function useTickingNow(intervalMs = 1_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);
  return now;
}
