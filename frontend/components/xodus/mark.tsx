import { useId } from "react";
import { cn } from "@/lib/utils";

/** The XODUS mark: a lit core inside two orbits. Inline SVG, decorative (the name is always written next to it). */
export function Mark({ className }: { className?: string }) {
  const id = useId();
  return (
    <svg viewBox="0 0 32 32" fill="none" aria-hidden="true" className={cn("text-primary", className)}>
      <defs>
        <radialGradient id={`${id}-core`} cx="50%" cy="45%" r="55%">
          <stop offset="0" stopColor="currentColor" stopOpacity="1" />
          <stop offset="0.6" stopColor="currentColor" stopOpacity="0.35" />
          <stop offset="1" stopColor="currentColor" stopOpacity="0" />
        </radialGradient>
      </defs>
      <circle cx="16" cy="16" r="7.5" fill={`url(#${id}-core)`} />
      <ellipse cx="16" cy="16" rx="13.5" ry="5.2" stroke="currentColor" strokeOpacity="0.75" strokeWidth="1.2" transform="rotate(-30 16 16)" />
      <ellipse cx="16" cy="16" rx="13.5" ry="5.2" stroke="currentColor" strokeOpacity="0.4" strokeWidth="1" transform="rotate(34 16 16)" />
      <circle cx="26.6" cy="10.4" r="1.5" fill="currentColor" />
    </svg>
  );
}
