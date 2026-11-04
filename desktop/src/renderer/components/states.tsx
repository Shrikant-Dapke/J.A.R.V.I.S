/** States for views that intentionally have no data in Phase 6.1. */

import type { ReactNode } from "react";
import { cn } from "../lib/cn";
import { Panel } from "./primitives";

export function EmptyState({
  title,
  children,
  aside,
}: {
  title: string;
  children: ReactNode;
  aside?: ReactNode;
}) {
  return (
    <Panel>
      <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-line/90 bg-hull/30 px-8 py-14 text-center">
        <div
          aria-hidden
          className="mb-5 h-10 w-10 rounded-full border border-accent/25 shadow-glow-sm"
        />
        <h4 className="font-mono text-[12px] uppercase tracking-[0.22em] text-accent-soft">
          {title}
        </h4>
        <p className="mt-3 max-w-md text-[13px] leading-relaxed text-slate-400">{children}</p>
        {aside ? <div className="mt-5">{aside}</div> : null}
      </div>
    </Panel>
  );
}

export function PhaseNote({ phase, children }: { phase: string; children: ReactNode }) {
  return (
    <div
      className={cn(
        "rounded-lg border border-line bg-hull/50 px-4 py-3",
        "font-mono text-[11px] uppercase tracking-[0.16em] text-slate-500",
      )}
    >
      <span className="text-accent/80">{phase}</span> · {children}
    </div>
  );
}
