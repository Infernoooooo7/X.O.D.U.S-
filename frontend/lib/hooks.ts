"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api";

/**
 * Load data and keep it fresh. Polls every `intervalMs` while the page is visible (0 = load once). `error` holds the
 * last failure; the previous data stays on screen, marked stale by the caller, instead of disappearing.
 */
export function usePoll<T>(load: () => Promise<T>, intervalMs: number, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const loadRef = useRef(load);
  loadRef.current = load;

  const refresh = useCallback(async () => {
    try {
      const value = await loadRef.current();
      setData(value);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err : new ApiError(0, "client_error", String(err)));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    void refresh();
    if (!intervalMs) return () => { alive = false; };
    const id = setInterval(() => {
      if (alive && document.visibilityState === "visible") void refresh();
    }, intervalMs);
    return () => { alive = false; clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, refresh, ...deps]);

  return { data, error, loading, refresh, setData };
}

/** Run an action, tracking busy state and the last error, so every button can show what happened. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const run = useCallback(async <T,>(fn: () => Promise<T>): Promise<T | undefined> => {
    setBusy(true);
    setError(null);
    try {
      return await fn();
    } catch (err) {
      setError(err instanceof ApiError ? err : new ApiError(0, "client_error", String(err)));
      return undefined;
    } finally {
      setBusy(false);
    }
  }, []);
  return { busy, error, run, setError };
}

/** Re-render every `ms` (for countdowns and "x min ago"). */
export function useNow(ms = 1000): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now() / 1000), ms);
    return () => clearInterval(id);
  }, [ms]);
  return now;
}

/** A query-string parameter, read on the client (static export: no server rendering of search params). */
export function useQueryParam(name: string): [string | null, (value: string | null) => void] {
  const [value, setValue] = useState<string | null>(null);
  useEffect(() => {
    const read = () => setValue(new URLSearchParams(window.location.search).get(name));
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, [name]);
  const update = useCallback((next: string | null) => {
    const url = new URL(window.location.href);
    if (next === null) url.searchParams.delete(name);
    else url.searchParams.set(name, next);
    window.history.replaceState(null, "", url);
    setValue(next);
  }, [name]);
  return [value, update];
}
