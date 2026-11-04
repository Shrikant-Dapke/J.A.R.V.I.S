/**
 * Buttons: every rendered button is wired by construction — `onClick`
 * and `disabled` are required input for interactivity, so a
 * non-functional button cannot be added without a lint-visible call
 * passing undefined handlers.
 */

import type { ReactNode } from "react";
import { cn } from "../lib/cn";

export type ButtonVariant = "primary" | "ghost" | "danger";

export function Button({
  children,
  onClick,
  disabled,
  variant = "ghost",
  small = false,
}: {
  children: ReactNode;
  /** Required: buttons without an action simply do not exist here. */
  onClick: () => void;
  /** Required: forces each call site to state its enablement. */
  disabled: boolean;
  variant?: ButtonVariant;
  small?: boolean;
}) {
  const base = cn(
    "focus-ring inline-flex items-center justify-center gap-2 rounded-md border font-mono uppercase tracking-[0.14em] transition-all",
    small ? "px-2.5 py-1 text-[10px]" : "px-3.5 py-1.5 text-[11px]",
    variant === "primary" &&
      "border-accent/50 bg-accent/10 text-accent-soft hover:bg-accent/20 hover:shadow-glow-sm",
    variant === "ghost" &&
      "border-line text-slate-300 hover:border-accent/40 hover:text-accent-soft",
    variant === "danger" &&
      "border-danger/45 bg-danger/10 text-danger hover:bg-danger/20",
    disabled && "cursor-not-allowed opacity-40",
  );
  return (
    <button type="button" className={base} onClick={onClick} disabled={disabled}>
      {children}
    </button>
  );
}

/** Navigation entry — a real <button> wired to the view switcher. */
export function NavButton({
  label,
  active,
  icon,
  onClick,
}: {
  label: string;
  active: boolean;
  icon: ReactNode;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? "page" : undefined}
      className={cn(
        "focus-ring flex w-full items-center gap-3 rounded-lg px-3 py-2.5 text-left text-[13px] transition-colors",
        active
          ? "border border-accent/40 bg-accent/10 text-accent-soft shadow-glow-sm"
          : "border border-transparent text-slate-400 hover:bg-white/[0.04] hover:text-slate-200",
      )}
    >
      <span className={cn("h-4 w-4", active ? "text-accent" : "text-slate-500")}>{icon}</span>
      <span className="flex-1">{label}</span>
      {active ? <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-accent animate-pulse-soft" /> : null}
    </button>
  );
}
