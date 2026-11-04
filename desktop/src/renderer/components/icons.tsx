/** Hand-authored stroke icons (no icon dependency). */

import type { ReactElement, SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement>;

function Base({ children, ...props }: IconProps): ReactElement {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      {children}
    </svg>
  );
}

export function ActivityIcon(props: IconProps) {
  return (
    <Base {...props}>
      <path d="M3 12h4l2.5-7 5 14L17 12h4" />
    </Base>
  );
}

export function ChatIcon(props: IconProps) {
  return (
    <Base {...props}>
      <path d="M21 12a8 8 0 0 1-8 8H4l2-3.2A8 8 0 1 1 21 12Z" />
      <path d="M9 11h6M9 14h3" />
    </Base>
  );
}

export function ShieldIcon(props: IconProps) {
  return (
    <Base {...props}>
      <path d="M12 3l7 3v5c0 4.4-2.9 8.2-7 10-4.1-1.8-7-5.6-7-10V6l7-3Z" />
      <path d="m9 12 2 2 4-4" />
    </Base>
  );
}

export function PlansIcon(props: IconProps) {
  return (
    <Base {...props}>
      <path d="M4 5.5 5.4 7 8 4.5" />
      <path d="M4 12.5 5.4 14 8 11.5" />
      <path d="M4 19.5 5.4 21 8 18.5" />
      <path d="M11 6h9M11 13h9M11 20h9" />
    </Base>
  );
}

export function RefreshIcon(props: IconProps) {
  return (
    <Base {...props}>
      <path d="M20 11a8 8 0 1 0-2.3 6.3" />
      <path d="M20 5v6h-6" />
    </Base>
  );
}

export function PanelRightIcon(props: IconProps) {
  return (
    <Base {...props}>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="M15 4v16" />
      <path d="M17.5 12.5h1M17.5 15.5h1" />
    </Base>
  );
}

export function MenuIcon(props: IconProps) {
  return (
    <Base {...props}>
      <path d="M4 6h16M4 12h16M4 18h16" />
    </Base>
  );
}

export function CloseIcon(props: IconProps) {
  return (
    <Base {...props}>
      <path d="m6 6 12 12M18 6 6 18" />
    </Base>
  );
}
