"""Data layer: loads a zhengyin-dict SQLite database into memory.

The dictionary lives in the separate zhengyin-dict repository; this CLI
only reads it. Field access is deliberately tolerant: records are plain
dicts and optional fields (e.g. tongjia ``sense``) are read with ``.get``
so newer/older dictionary builds never crash the CLI.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def _load_variants() -> dict[str, str]:
    p = Path(__file__).parent / "data" / "variants.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except OSError:
        return {}


class Dict:
    """In-memory view of one zhengyin.db."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            self.meta = {
                r["key"]: r["value"] for r in con.execute("SELECT key, value FROM meta")
            }
            self.chars: dict[str, dict] = {}
            for r in con.execute("SELECT * FROM chars"):
                d = dict(r)
                d["readings"] = json.loads(d.get("readings") or "[]")
                d["default"] = d.get("default_reading") or (
                    d["readings"][0] if d["readings"] else None
                )
                try:
                    _cr = d.get("common_readings")
                    _common = json.loads(_cr) if _cr else []
                    d["common"] = _common if isinstance(_common, list) else []
                except (ValueError, TypeError):
                    d["common"] = []
                self.chars[d["char"]] = d

            self.tongjia: dict[str, list[dict]] = {}
            for r in con.execute("SELECT * FROM tongjia"):
                d = dict(r)
                for k in ("sense", "example", "char_simp", "benzi_simp", "src_ref"):
                    d.setdefault(k, "")
                    if d[k] is None:
                        d[k] = ""
                self.tongjia.setdefault(self.canonical(d["char"]), []).append(d)

            self.podu: dict[str, list[dict]] = {}
            for r in con.execute("SELECT * FROM podu"):
                d = dict(r)
                for k in ("context", "sense", "basis", "confidence", "verified"):
                    d.setdefault(k, "")
                    if d[k] is None:
                        d[k] = ""
                d.setdefault("verified", "unverified")
                self.podu.setdefault(self.canonical(d["char"]), []).append(d)

            self.phrases: dict[str, list[tuple[str, list[str]]]] = {}
            for r in con.execute("SELECT * FROM phrases"):
                d = dict(r)
                phrase = d["phrase"]
                pinyin = json.loads(d.get("pinyin") or "[]")
                if len(phrase) != len(pinyin):
                    continue
                canon = "".join(self.canonical(c) for c in phrase)
                self.phrases.setdefault(canon[0], []).append((canon, pinyin))
            for lst in self.phrases.values():
                lst.sort(key=lambda t: -len(t[0]))

            self.heteronyms: list[dict] = []
            for r in con.execute("SELECT * FROM heteronyms"):
                d = dict(r)
                d["readings"] = json.loads(d.get("readings") or "[]")
                d.setdefault("note", "")
                self.heteronyms.append(d)

            self.substitutes: dict[str, list[str]] = {}
            for r in con.execute("SELECT * FROM substitutes"):
                d = dict(r)
                self.substitutes[d["reading"]] = json.loads(d.get("candidates") or "[]")
        finally:
            con.close()

    # -- variant handling -------------------------------------------------
    _variants: dict[str, str] | None = None

    @classmethod
    def _variant_map(cls) -> dict[str, str]:
        if cls._variants is None:
            cls._variants = _load_variants()
        return cls._variants

    def canonical(self, ch: str) -> str:
        """Canonical glyph form (simplified representative) of a character."""
        return self._variant_map().get(ch, ch)

    def variant_forms(self, ch: str) -> list[str]:
        """All glyph forms equivalent to ``ch`` (including itself)."""
        canon = self.canonical(ch)
        forms = {ch, canon}
        for k, v in self._variant_map().items():
            if v == canon:
                forms.add(k)
        return sorted(forms)

    # -- character info ---------------------------------------------------
    def char_info(self, ch: str) -> dict | None:
        """chars row for the exact glyph, else for any equivalent form."""
        if ch in self.chars:
            return self.chars[ch]
        for form in self.variant_forms(ch):
            if form in self.chars:
                return self.chars[form]
        return None

    def readings_of(self, ch: str) -> list[str]:
        info = self.char_info(ch)
        return list(info["readings"]) if info else []

    def default_of(self, ch: str) -> str | None:
        info = self.char_info(ch)
        return info["default"] if info else None

    # -- lookups ------------------------------------------------------------
    def tongjia_entries(self, ch: str) -> list[dict]:
        return list(self.tongjia.get(self.canonical(ch), []))

    def podu_entries(self, ch: str) -> list[dict]:
        return list(self.podu.get(self.canonical(ch), []))

    def best_substitute(self, reading: str) -> str | None:
        """Safest same-sound replacement for ``reading``.

        A candidate qualifies only if it is single-reading and its default
        reading is exactly the target (so any default-reading TTS says it
        right); ranked by commonness (normative-table membership, then
        frequency rank).
        """
        best: tuple | None = None
        best_char: str | None = None
        for cand in self.substitutes.get(reading, []):
            info = self.chars.get(cand) or self.char_info(cand)
            if not info:
                continue
            if len(info["readings"]) != 1 or info["default"] != reading:
                continue
            if not info.get("in_8105") and info.get("freq_rank") is None:
                continue
            rank = info.get("freq_rank")
            key = (0 if info.get("in_8105") else 1, 0 if rank is not None else 1,
                   rank if rank is not None else 1 << 30)
            if best is None or key < best:
                best, best_char = key, cand
        return best_char

    def benzi_valid(self, benzi: str, reading: str) -> bool:
        """True if ``benzi``'s default reading is exactly ``reading``."""
        return self.default_of(benzi) == reading
