// Small building blocks of the XODUS design: page header, numbered card, ticket strip, tags, status, notices.

import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import type { ApiError } from "@/lib/api";
import type { Tone } from "@/lib/status";

export function PageHeader({ number, title, lead, children }: { number: string; title: string; lead: string; children?: ReactNode }) {
  return (
    <header className="mb-6 flex flex-col gap-3 border-b border-border pb-5 sm:flex-row sm:items-end sm:justify-between">
      <div className="min-w-0">
        <p className="card-number" aria-hidden="true">{number}</p>
        <h1 className="text-[1.75rem] font-semibold tracking-[-0.01em] sm:text-[2.2rem]">{title}</h1>
        <p className="mt-1 max-w-[62ch] text-muted-foreground">{lead}</p>
      </div>
      {children && <div className="flex shrink-0 flex-wrap items-center gap-2">{children}</div>}
    </header>
  );
}

/** Card anatomy from the brief: number, monospace eyebrow, title, one-line subtitle, body, tags, one action. */
export function NumberedCard({
  number, eyebrow, title, subtitle, children, tags, action, className, headingLevel = 2, ...rest
}: {
  number?: string; eyebrow?: ReactNode; title: ReactNode; subtitle?: ReactNode; children?: ReactNode; tags?: ReactNode;
  action?: ReactNode; className?: string; headingLevel?: 2 | 3;
} & React.HTMLAttributes<HTMLElement>) {
  const H = headingLevel === 2 ? "h2" : "h3";
  return (
    <article className={cn("rounded-md border border-border bg-card p-4 text-card-foreground sm:p-5", className)} {...rest}>
      <div className="flex items-baseline gap-3">
        {number && <span className="card-number" aria-hidden="true">{number}</span>}
        {eyebrow && <span className="eyebrow min-w-0 truncate">{eyebrow}</span>}
      </div>
      <H className="mt-1 text-lg font-semibold leading-snug">{title}</H>
      {subtitle && <p className="mt-0.5 text-sm text-muted-foreground">{subtitle}</p>}
      {children && <div className="mt-3 text-sm">{children}</div>}
      {(tags || action) && (
        <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap gap-1.5">{tags}</div>
          {action && <div className="shrink-0">{action}</div>}
        </div>
      )}
    </article>
  );
}

/** Ticket strip: label/value pairs in monospace under a dashed rule. */
export function Ticket({ fields, className }: { fields: [string, ReactNode][]; className?: string }) {
  const shown = fields.filter(([, v]) => v !== null && v !== undefined && v !== "");
  if (!shown.length) return null;
  return (
    <p className={cn("ticket mt-3", className)}>
      {shown.map(([label, value]) => (
        <span key={label}>{label} <b>{value}</b></span>
      ))}
    </p>
  );
}

export function Tag({ children, title }: { children: ReactNode; title?: string }) {
  return <span className="tag" title={title}>{children}</span>;
}

const TONE: Record<Tone, string> = {
  neutral: "border-border bg-muted text-foreground",
  active: "border-primary/40 bg-accent text-accent-foreground",
  wait: "border-warn/50 bg-warn-soft text-warn",
  good: "border-ok/40 bg-card text-ok",
  bad: "border-destructive/40 bg-destructive-soft text-destructive",
};

export function Status({ tone, children, title }: { tone: Tone; children: ReactNode; title?: string }) {
  return (
    <span title={title} className={cn("inline-flex items-center gap-1.5 rounded-sm border px-2 py-0.5 text-xs font-medium", TONE[tone])}>
      <span aria-hidden="true" className={cn("size-1.5 rounded-full bg-current", tone === "active" && "animate-pulse")} />
      {children}
    </span>
  );
}

/** An error the user can act on: what happened, the code, and (optionally) what to do. */
export function ErrorNote({ error, title, hint }: { error: ApiError | { code: string; message: string } | null; title?: string; hint?: ReactNode }) {
  if (!error) return null;
  return (
    <div role="alert" className="mt-3 rounded-md border border-destructive/40 bg-destructive-soft px-3 py-2 text-sm text-destructive">
      {title && <p className="font-semibold">{title}</p>}
      <p className="text-foreground">{error.message}</p>
      <p className="mt-1 font-mono text-xs">code {error.code}</p>
      {hint && <p className="mt-1 text-foreground">{hint}</p>}
    </div>
  );
}

export function Note({ children, tone = "neutral", role }: { children: ReactNode; tone?: "neutral" | "wait"; role?: string }) {
  return (
    <div role={role} className={cn("rounded-md border px-3 py-2 text-sm",
      tone === "wait" ? "border-warn/50 bg-warn-soft text-foreground" : "border-border bg-card text-muted-foreground")}>
      {children}
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="rounded-md border border-dashed border-rail px-4 py-6 text-sm">
      <p className="font-medium">{title}</p>
      {children && <div className="mt-1 text-muted-foreground">{children}</div>}
    </div>
  );
}

export function Section({ title, description, children, actions, id }: {
  title: string; description?: ReactNode; children: ReactNode; actions?: ReactNode; id?: string;
}) {
  return (
    <section className="mt-8 first:mt-0" aria-labelledby={id}>
      <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
        <div>
          <h2 id={id} className="text-xl font-semibold">{title}</h2>
          {description && <p className="text-sm text-muted-foreground">{description}</p>}
        </div>
        {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
      </div>
      {children}
    </section>
  );
}

export function JsonView({ value, label }: { value: unknown; label?: string }) {
  return (
    // tabIndex: a scrollable box must be reachable by keyboard so it can be scrolled without a mouse
    <pre aria-label={label} tabIndex={0} className="scroll-x max-h-80 rounded-sm border border-border bg-background p-3 font-mono text-xs leading-relaxed">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

/** Label + value rows that wrap on narrow screens (used instead of wide tables). */
export function Facts({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-1 gap-x-4 gap-y-1 text-sm sm:grid-cols-[max-content_1fr]">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="font-mono text-xs text-muted-foreground sm:pt-0.5">{k}</dt>
          <dd className="mb-1 min-w-0 overflow-wrap-anywhere sm:mb-0 [overflow-wrap:anywhere]">{v}</dd>
        </div>
      ))}
    </dl>
  );
}
