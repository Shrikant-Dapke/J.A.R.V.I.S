/** Loading skeletons: indeterminate, labeled blocks (never fake data). */

import { cn } from "../../lib/cn";

function Shimmer({ className }: { className?: string }) {
  return (
    <div
      aria-hidden
      className={cn("rounded-md bg-line/40 animate-pulse-soft", className)}
    />
  );
}

export function SkeletonLines({ lines = 3 }: { lines?: number }) {
  const widths = ["w-full", "w-11/12", "w-4/5", "w-2/3", "w-5/6"];
  return (
    <div className="flex flex-col gap-2" role="status" aria-label="Loading">
      {Array.from({ length: lines }, (_, index) => (
        <Shimmer key={index} className={cn("h-3.5", widths[index % widths.length])} />
      ))}
    </div>
  );
}

export function SkeletonRows({ rows = 3 }: { rows?: number }) {
  return (
    <div className="flex flex-col gap-2" role="status" aria-label="Loading">
      {Array.from({ length: rows }, (_, index) => (
        <Shimmer key={index} className="h-14 w-full" />
      ))}
    </div>
  );
}

export function SkeletonBlock({ className }: { className?: string }) {
  return <Shimmer className={cn("h-24 w-full", className)} />;
}
