import { Activity, BookOpen, Brain, Cpu, FlaskConical, Gauge, History, Puzzle, Settings, ShieldCheck, Sparkles, type LucideIcon } from "lucide-react";

export type View = { href: string; label: string; hint: string; icon: LucideIcon; group: "workspace" | "your data" | "agency" | "system" };

/** Every view, in navigation order. The palette (Ctrl+K) offers the same list. */
export const VIEWS: View[] = [
  { href: "/", label: "Workspace", hint: "Chat, ask your documents, run tasks", icon: Sparkles, group: "workspace" },
  { href: "/history/", label: "History", hint: "Saved conversations", icon: History, group: "your data" },
  { href: "/knowledge/", label: "Knowledge", hint: "Collections, files, ingestion", icon: BookOpen, group: "your data" },
  { href: "/memory/", label: "Memory", hint: "Notes, facts, preferences", icon: Brain, group: "your data" },
  { href: "/capabilities/", label: "Capabilities", hint: "What XODUS can do, and with which rights", icon: Puzzle, group: "agency" },
  { href: "/approvals/", label: "Approvals", hint: "Actions waiting for you", icon: ShieldCheck, group: "agency" },
  { href: "/activity/", label: "Activity", hint: "Tasks and the audit log", icon: Activity, group: "agency" },
  { href: "/models/", label: "Models", hint: "Profiles, footprint, switching", icon: Cpu, group: "system" },
  { href: "/resources/", label: "Resources", hint: "Processes, memory, VRAM", icon: Gauge, group: "system" },
  { href: "/experiments/", label: "Experiments", hint: "Measurement results", icon: FlaskConical, group: "system" },
  { href: "/settings/", label: "Settings", hint: "Configuration and theme", icon: Settings, group: "system" },
];

export function isActive(path: string, href: string): boolean {
  return href === "/" ? path === "/" : path.startsWith(href.replace(/\/$/, ""));
}
