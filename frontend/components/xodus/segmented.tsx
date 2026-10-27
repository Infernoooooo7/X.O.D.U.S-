"use client";

import { cn } from "@/lib/utils";

/** One choice among a few options, shown as a segmented control. Native radio buttons, so the semantics and keyboard
 *  behaviour (arrow keys move the choice) come from the browser. */
export function Segmented<T extends string>({ legend, name, value, options, onChange, disabled }: {
  legend: string; name: string; value: T; options: { value: T; label: string }[]; onChange: (v: T) => void; disabled?: boolean;
}) {
  return (
    <fieldset disabled={disabled}>
      <legend className="sr-only">{legend}</legend>
      <div className="inline-flex flex-wrap gap-0.5 rounded-full border border-border bg-background/60 p-1">
        {options.map((o) => {
          const on = value === o.value;
          return (
            <label key={o.value}
              className={cn("cursor-pointer rounded-full px-3.5 py-1 text-sm font-medium transition-[background-color,color,box-shadow] duration-200",
                "has-[:focus-visible]:outline has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-1 has-[:focus-visible]:outline-ring",
                "has-[:disabled]:cursor-not-allowed has-[:disabled]:opacity-60",
                on ? "bg-primary text-primary-foreground shadow-[0_0_16px_-4px_rgb(var(--glow)/0.8)]" : "text-foreground hover:bg-muted")}>
              <input type="radio" className="sr-only" name={name} value={o.value} checked={on} onChange={() => onChange(o.value)} />
              {o.label}
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}
