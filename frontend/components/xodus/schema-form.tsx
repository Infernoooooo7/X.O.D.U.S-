"use client";

import { useMemo, useState } from "react";
import type { JsonSchema } from "@/lib/api";
import { coerce, fieldsFor } from "@/lib/schema-form";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Checkbox } from "@/components/ui/checkbox";

/** A form generated from a capability's input schema. `idPrefix` keeps field ids unique on the page. */
export function SchemaForm({ schema, idPrefix, submitLabel, busy, onSubmit }: {
  schema: JsonSchema; idPrefix: string; submitLabel: string; busy?: boolean; onSubmit: (args: Record<string, unknown>) => void;
}) {
  const fields = useMemo(() => fieldsFor(schema), [schema]);
  const [values, setValues] = useState<Record<string, string | boolean>>(() =>
    Object.fromEntries(fields.map((f) => [f.name, f.initial])));
  const [errors, setErrors] = useState<Record<string, string>>({});

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const result = coerce(fields, values);
    setErrors(result.errors);
    if (!Object.keys(result.errors).length) onSubmit(result.args);
  }

  const set = (name: string, value: string | boolean) => setValues((v) => ({ ...v, [name]: value }));

  return (
    <form onSubmit={submit} className="space-y-4" noValidate>
      {fields.length === 0 && <p className="text-sm text-muted-foreground">This capability takes no arguments.</p>}
      {fields.map((f) => {
        const id = `${idPrefix}-${f.name}`;
        const describedBy = [f.description || f.pathRoot ? `${id}-help` : "", errors[f.name] ? `${id}-error` : ""].filter(Boolean).join(" ") || undefined;
        const common = { id, "aria-invalid": Boolean(errors[f.name]) || undefined, "aria-describedby": describedBy };
        return (
          <div key={f.name} className="space-y-1.5">
            {f.kind === "boolean" ? (
              <div className="flex items-center gap-2">
                <Checkbox {...common} checked={Boolean(values[f.name])} onCheckedChange={(c) => set(f.name, c === true)} />
                <Label htmlFor={id}>{f.label}</Label>
              </div>
            ) : (
              <Label htmlFor={id}>{f.label}{f.required ? "" : " (optional)"}</Label>
            )}
            {f.kind === "enum" && (
              <select {...common} value={String(values[f.name] ?? "")} onChange={(e) => set(f.name, e.target.value)}
                className="h-8 w-full rounded-md border border-input bg-background px-2 text-sm">
                {!f.required && <option value="">Default</option>}
                {f.options?.map((o) => <option key={o} value={o}>{o}</option>)}
              </select>
            )}
            {(f.kind === "text" || f.kind === "json" || f.kind === "string-list" || f.kind === "number-list") && (
              <Textarea {...common} rows={f.kind === "text" ? 4 : 2} value={String(values[f.name] ?? "")}
                onChange={(e) => set(f.name, e.target.value)} className={f.kind === "json" ? "font-mono text-xs" : undefined}
                placeholder={f.kind === "number-list" ? "1, 2, 3" : f.kind === "string-list" ? "one per line" : undefined} />
            )}
            {(f.kind === "string" || f.kind === "integer" || f.kind === "number") && (
              <Input {...common} inputMode={f.kind === "string" ? undefined : "decimal"} value={String(values[f.name] ?? "")}
                onChange={(e) => set(f.name, e.target.value)} placeholder={f.pathRoot ? "." : undefined} />
            )}
            {(f.description || f.pathRoot) && (
              <p id={`${id}-help`} className="text-xs text-muted-foreground">
                {f.description}
                {f.pathRoot && <> Relative to the <code>{f.pathRoot}</code> folder; XODUS refuses anything outside it.</>}
                {f.min !== undefined || f.max !== undefined ? ` Range ${f.min ?? "…"} to ${f.max ?? "…"}.` : ""}
              </p>
            )}
            {errors[f.name] && <p id={`${id}-error`} className="text-xs font-medium text-destructive">{errors[f.name]}</p>}
          </div>
        );
      })}
      <Button type="submit" disabled={busy}>{busy ? "Running…" : submitLabel}</Button>
    </form>
  );
}
