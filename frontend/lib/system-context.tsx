"use client";

import { createContext, useContext, type ReactNode } from "react";
import { api, type ApiError, type Approval, type Health } from "./api";
import { usePoll } from "./hooks";

/** The part of GET /api/resources the shell shows: measured numbers only, never estimates. */
export type ResourceSummary = {
  totals: { uss_mib: number; uss_complete: boolean };
  device_vram: { total: number; used: number; free: number } | null; // MiB, device-wide (NVML)
  thresholds: { level: string; checks: { metric: string; value_gb: number | null; warn_gb: number | null; stop_gb: number; level: string }[] };
};

type System = {
  health: Health | null;
  healthError: ApiError | null;
  /** The backend itself did not answer (as opposed to Ollama being down, which health reports). */
  unreachable: boolean;
  pending: number;
  resources: ResourceSummary | null;
  refreshHealth: () => Promise<void>;
};

const SystemContext = createContext<System | null>(null);

/** One poll per source for the whole page: health every 5 s, approvals every 3 s, resources every 15 s. */
export function SystemProvider({ children }: { children: ReactNode }) {
  // An older backend does not report ollama_environment: treat it as "not verified" rather than breaking the page.
  const health = usePoll(async () => {
    const h = await api<Health>("/api/health");
    return h.ollama_environment ? h : { ...h, ollama_environment: { status: "unknown", pid: null, checks: [], fix: null,
      message: "This backend does not report how Ollama was started; restart it to get the check." } } as Health;
  }, 5000);
  const approvals = usePoll(() => api<Approval[]>("/api/approvals"), 3000);
  const resources = usePoll(() => api<ResourceSummary>("/api/resources"), 15000);
  const value: System = {
    health: health.data,
    healthError: health.error,
    unreachable: health.error?.code === "backend_unreachable",
    pending: approvals.data?.length ?? 0,
    resources: resources.data,
    refreshHealth: health.refresh,
  };
  return <SystemContext.Provider value={value}>{children}</SystemContext.Provider>;
}

export function useSystem(): System {
  const value = useContext(SystemContext);
  if (!value) throw new Error("useSystem() needs <SystemProvider>");
  return value;
}
