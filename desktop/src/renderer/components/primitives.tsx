/** Small presentational primitives shared by all views. */

import type { ReactNode } from "react";
import { cn } from "../lib/cn";

export function Panel({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return <section className={cn("panel p-5", className)}>{children}</section>;
}

export function SectionTitle({
  title,
  suffix,
}: {
  title: string;
  suffix?: string;
}) {
  return (
    <h3 className="mb-3 font-mono text-[11px] uppercase tracking-[0.2em] text-slate-400">
      {title}
      {suffix ? <span className="ml-2 text-slate-600">{suffix}</span> : null}
    </h3>
  );
}

export function KeyValue({ k, v }: { k: string; v: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-1.5">
      <span className="kv-key">{k}</span>
      <span className="kv-value text-right">{v}</span>
    </div>
  );
}

export function SparkDot({ tone }: { tone: "ok" | "warn" | "danger" | "idle" }) {
  const color =
    tone === "ok"
      ? "bg-ok"
      : tone === "warn"
        ? "bg-warn"
        : tone === "danger"
          ? "bg-danger"
          : "bg-slate-600";
  return <span className={cn("inline-block h-2 w-2 rounded-full", color)} />;
}

export function Badge({
  tone,
  children,
}: {
  tone: "ok" | "warn" | "danger" | "idle";
  children: ReactNode;
}) {
  const classes =
    tone === "ok"
      ? "border-ok/40 text-ok"
      : tone === "warn"
        ? "border-warn/40 text-warn"
        : tone === "danger"
          ? "border-danger/40 text-danger"
          : "border-slate-600/60 text-slate-400";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded border px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.12em]",
        classes,
      )}
    >
      {children}
    </span>
  );
}

export function ErrorNote({ detail }: { detail: string }) {
  return (
    <div className="rounded-lg border border-danger/35 bg-danger/5 px-3 py-2.5">
      <p className="text-[13px] leading-relaxed text-danger">{detail}</p>
    </div>
  );
}
