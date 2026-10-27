// Capability input forms rendered from JSON Schema (master prompt section 9). Supports the subset capability
// manifests use: strings (with enum, length), integers and numbers (with bounds), booleans, and arrays of strings or
// numbers. Anything else falls back to a JSON text field. The backend validates again: this only helps the user.

import type { JsonSchema } from "./api";

export type FieldKind = "string" | "text" | "enum" | "integer" | "number" | "boolean" | "string-list" | "number-list" | "json";

export type Field = {
  name: string;
  kind: FieldKind;
  label: string;
  required: boolean;
  description?: string;
  options?: string[];
  min?: number;
  max?: number;
  maxLength?: number;
  initial: string | boolean;
  pathRoot?: string; // x-xodus-path: the configured folder this argument is confined to
};

function typeOf(s: JsonSchema): string | undefined {
  return Array.isArray(s.type) ? s.type.find((t) => t !== "null") : s.type;
}

function labelFor(name: string): string {
  const words = name.replace(/[_-]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function fieldsFor(schema: JsonSchema): Field[] {
  const required = new Set(schema.required ?? []);
  return Object.entries(schema.properties ?? {}).map(([name, s]) => {
    const base = { name, label: labelFor(name), required: required.has(name), description: s.description };
    const pathPermission = typeof s["x-xodus-path"] === "string" ? (s["x-xodus-path"] as string) : undefined;
    const pathRoot = pathPermission?.split(":")[1];
    const t = typeOf(s);
    if (Array.isArray(s.enum) && s.enum.every((v) => typeof v === "string")) {
      return { ...base, kind: "enum", options: s.enum as string[], initial: (s.default as string) ?? "" };
    }
    if (t === "string") {
      const long = (s.maxLength ?? 0) > 200 || (s.maxLength === undefined && !pathRoot && name !== "path");
      return { ...base, kind: long ? "text" : "string", maxLength: s.maxLength, initial: (s.default as string) ?? "", pathRoot };
    }
    if (t === "integer" || t === "number") {
      return { ...base, kind: t, min: s.minimum, max: s.maximum, initial: s.default !== undefined ? String(s.default) : "" };
    }
    if (t === "boolean") return { ...base, kind: "boolean", initial: Boolean(s.default) };
    if (t === "array" && s.items) {
      const it = typeOf(s.items);
      if (it === "string") return { ...base, kind: "string-list", initial: "" };
      if (it === "number" || it === "integer") return { ...base, kind: "number-list", initial: "" };
    }
    return { ...base, kind: "json", initial: s.default !== undefined ? JSON.stringify(s.default) : "" };
  });
}

export type Coerced = { args: Record<string, unknown>; errors: Record<string, string> };

/** Turn form values into arguments. Empty optional fields are left out, so the capability's defaults apply. */
export function coerce(fields: Field[], values: Record<string, string | boolean>): Coerced {
  const args: Record<string, unknown> = {};
  const errors: Record<string, string> = {};
  for (const f of fields) {
    const raw = values[f.name];
    if (f.kind === "boolean") {
      if (raw !== undefined) args[f.name] = Boolean(raw);
      continue;
    }
    const text = typeof raw === "string" ? raw : "";
    if (text.trim() === "") {
      if (f.required) errors[f.name] = "Required.";
      continue;
    }
    switch (f.kind) {
      case "integer":
      case "number": {
        const n = Number(text);
        if (!Number.isFinite(n) || (f.kind === "integer" && !Number.isInteger(n))) {
          errors[f.name] = f.kind === "integer" ? "Enter a whole number." : "Enter a number.";
        } else if (f.min !== undefined && n < f.min) errors[f.name] = `At least ${f.min}.`;
        else if (f.max !== undefined && n > f.max) errors[f.name] = `At most ${f.max}.`;
        else args[f.name] = n;
        break;
      }
      case "string-list":
        args[f.name] = text.split(/\n|,/).map((s) => s.trim()).filter(Boolean);
        break;
      case "number-list": {
        const items = text.split(/[\s,;]+/).filter(Boolean).map(Number);
        if (items.some((n) => !Number.isFinite(n))) errors[f.name] = "Enter numbers separated by commas or spaces.";
        else args[f.name] = items;
        break;
      }
      case "json":
        try {
          args[f.name] = JSON.parse(text);
        } catch {
          errors[f.name] = "Not valid JSON.";
        }
        break;
      default:
        if (f.maxLength !== undefined && text.length > f.maxLength) errors[f.name] = `At most ${f.maxLength} characters.`;
        else args[f.name] = text;
    }
  }
  return { args, errors };
}
