"""Sample XODUS plugin: text statistics (subprocess, newline-delimited JSON-RPC 2.0 on stdin/stdout).

Standard library only, no files, no network. XODUS has already validated the arguments against manifest.json
before they arrive here, and validates this reply against its output_schema.
"""

import json
import re
import sys
from collections import Counter

WORD = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*", re.UNICODE)
SENTENCE_END = re.compile(r"[.!?]+(?=\s|$)")
STOPWORDS = frozenset(
    "a an and are as at be but by for from has have he her his i if in into is it its of on or our she so that the "
    "their them there they this to was we were what when which who will with you your".split())
WORDS_PER_MINUTE = 200


def stats(text: str, top: int = 5) -> dict:
    words = WORD.findall(text)
    lowered = [w.lower() for w in words]
    frequent = Counter(w for w in lowered if w not in STOPWORDS and len(w) > 1).most_common(top)
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    sentences = len(SENTENCE_END.findall(text)) or (1 if words else 0)
    return {
        "characters": len(text),
        "words": len(words),
        "sentences": sentences,
        "paragraphs": len(paragraphs),
        "average_word_length": round(sum(map(len, words)) / len(words), 2) if words else 0.0,
        "reading_minutes": round(len(words) / WORDS_PER_MINUTE, 2),
        "top_words": [{"word": w, "count": n} for w, n in frequent],
    }


def main() -> None:
    for line in sys.stdin:
        request = json.loads(line)
        try:
            args = request["params"]["args"]
            result = {"jsonrpc": "2.0", "id": request["id"], "result": stats(args["text"], args.get("top", 5))}
        except Exception as exc:  # report, never crash the loop
            result = {"jsonrpc": "2.0", "id": request.get("id"), "error": {"message": f"{type(exc).__name__}: {exc}"}}
        sys.stdout.write(json.dumps(result) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
