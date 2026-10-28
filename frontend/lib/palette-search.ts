// Ranking for the Ctrl+K palette (components/shell/command-palette.tsx). Pure, so it is unit-tested on its own.

type Searchable = { label: string; hint?: string; keywords?: string; group: string };

/** null: no match. Lower is better: the label starts with the query, contains it, contains every word, or only the
 *  hint and keywords do. So "knowledge" ranks the Knowledge view above an action that merely mentions knowledge. */
export function score(c: Searchable, query: string): number | null {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return 0;
  const label = c.label.toLowerCase();
  const hay = `${label} ${c.hint ?? ""} ${c.keywords ?? ""} ${c.group}`.toLowerCase();
  if (!words.every((w) => hay.includes(w))) return null;
  const phrase = words.join(" ");
  if (label.startsWith(phrase)) return 0;
  if (label.includes(phrase)) return 1;
  if (words.every((w) => label.includes(w))) return 2;
  return 3;
}
