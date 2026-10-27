// Formatting helpers shared by the views. Pure functions (tested in lib/__tests__).

/** T+ offset from the start of a task: T+0.4s, T+12.0s, T+2:05.3 */
export function tplus(ms: number): string {
  const total = Math.max(0, ms) / 1000;
  if (total < 60) return `T+${total.toFixed(1)}s`;
  const minutes = Math.floor(total / 60);
  const seconds = total - minutes * 60;
  return `T+${minutes}:${seconds.toFixed(1).padStart(4, "0")}`;
}

export function mib(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "n/a";
  return `${value.toLocaleString("en-GB", { maximumFractionDigits: digits, minimumFractionDigits: digits })} MiB`;
}

export function bytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return "n/a";
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KiB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MiB`;
  return `${(value / 1024 ** 3).toFixed(2)} GiB`;
}

/** "just now", "5 min ago", "3 h ago", or a date. `seconds` is a Unix timestamp. */
export function ago(seconds: number, now: number = Date.now() / 1000): string {
  const d = now - seconds;
  if (d < 45) return "just now";
  if (d < 3600) return `${Math.round(d / 60)} min ago`;
  if (d < 86400) return `${Math.round(d / 3600)} h ago`;
  return new Date(seconds * 1000).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

export function clock(seconds: number): string {
  return new Date(seconds * 1000).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function shortHash(hash: string, n = 12): string {
  return hash.length > n ? `${hash.slice(0, n)}…` : hash;
}

export function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/** The first sentence: ends at . ! or ? followed by a capital letter or the end (so "e.g. (17.5" does not end it). */
export function firstSentence(text: string): string {
  const m = text.match(/^.*?[.!?](?=\s+[A-Z]|\s*$)/s);
  return (m ? m[0] : text).trim();
}

/** Seconds left until `expires` as m:ss, or "expired". */
export function countdown(expires: number, now: number = Date.now() / 1000): string {
  const left = Math.floor(expires - now);
  if (left <= 0) return "expired";
  return `${Math.floor(left / 60)}:${pad2(left % 60)}`;
}
