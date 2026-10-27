// Split an answer into text and citation markers, matching the backend's validator (retrieval/citations.py):
// "[chunk:12]", "[chunk:12, chunk:13]", "[chunk:12, 13]"; ASCII digits only. "[citation removed]" stays text.

export type AnswerPart = { kind: "text"; text: string } | { kind: "cite"; ids: number[]; raw: string };

const GROUP = /\[\s*chunk\s*:\s*([0-9]+(?:\s*,\s*(?:chunk\s*:\s*)?[0-9]+)*)\s*\]/gi;

export function splitCitations(text: string): AnswerPart[] {
  const parts: AnswerPart[] = [];
  let last = 0;
  for (const m of text.matchAll(GROUP)) {
    const start = m.index ?? 0;
    if (start > last) parts.push({ kind: "text", text: text.slice(last, start) });
    const ids = [...m[1].matchAll(/[0-9]+/g)].map((d) => Number(d[0]));
    parts.push({ kind: "cite", ids, raw: m[0] });
    last = start + m[0].length;
  }
  if (last < text.length) parts.push({ kind: "text", text: text.slice(last) });
  return parts;
}
