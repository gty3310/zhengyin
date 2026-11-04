"""The learned layer: mappings a user confirmed through the Q&A loop.

Stored as JSONL at ``~/.zhengyin/learned.jsonl``, separate from the base
dictionary so dictionary upgrades never wipe what a user has taught the
tool. Entries can be exported in zhengyin-dict tongjia format for
upstreaming via pull request.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path


def default_learned_path() -> Path:
    return Path.home() / ".zhengyin" / "learned.jsonl"


class LearnStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_learned_path()
        self.entries: list[dict] = []
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        self.entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

    def match(self, char: str, sentence: str, window: str) -> dict | None:
        """A learned mapping applies to the same char in the same context.

        Context identity = the exact sentence, or the exact ±2-char
        window, that the user answered in.
        """
        for e in self.entries:
            if e.get("char") != char:
                continue
            if e.get("context") == sentence or e.get("context") == window:
                return e
        return None

    def add(self, char: str, context: str, reading: str, substitute: str,
            sense: str = "") -> dict:
        entry = {
            "char": char,
            "context": context,
            "reading": reading,
            "substitute": substitute,
            "sense": sense,
            "src": "user-answer",
            "verified": "user_confirmed",
            "date": date.today().isoformat(),
        }
        # replace any prior entry for the same char+context
        self.entries = [
            e for e in self.entries
            if not (e.get("char") == char and e.get("context") == context)
        ]
        self.entries.append(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def export_dict_format(self) -> list[dict]:
        """Render entries as zhengyin-dict tongjia-format records."""
        out = []
        for e in self.entries:
            out.append({
                "char": e.get("char", ""),
                "benzi": e.get("substitute", ""),
                "reading": e.get("reading", ""),
                "sense": e.get("sense", ""),
                "example": e.get("context", ""),
                "char_simp": "",
                "benzi_simp": "",
                "src": "zhengyin:learned",
                "src_ref": f"user-answer via zhengyin CLI, {e.get('date', '')}",
            })
        return out
