#!/usr/bin/env python3
"""Generate src/zhengyin/data/variants.json from Unihan_Variants.txt.

Builds union-find classes over kSimplifiedVariant / kTraditionalVariant /
kZVariant edges (glyph-form equivalence only; semantic variants excluded).
Canonical representative of each class = the simplified form when the class
contains one (a kSimplifiedVariant target), else the smallest codepoint.

Source: Unicode Unihan database (Unicode License v3).
Usage: python3 tools/make_variants.py [path-to-Unihan_Variants.txt]
"""
import json
import sys
from pathlib import Path

DEFAULT_SRC = Path.home() / "workspace/zhengyin-dict/sources_raw/Unihan_Variants.txt"
OUT = Path(__file__).resolve().parent.parent / "src/zhengyin/data/variants.json"


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SRC
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    simp_targets: set[str] = set()
    for line in src.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        cp, field, values = parts[0], parts[1], parts[2]
        if field not in ("kSimplifiedVariant", "kTraditionalVariant", "kZVariant"):
            continue
        try:
            ch = chr(int(cp[2:], 16))
        except ValueError:
            continue
        for v in values.split():
            v = v.split("<")[0].strip()
            if not v.startswith("U+"):
                continue
            try:
                other = chr(int(v[2:], 16))
            except ValueError:
                continue
            union(ch, other)
            if field == "kSimplifiedVariant":
                simp_targets.add(other)
            elif field == "kTraditionalVariant":
                simp_targets.add(ch)

    classes: dict[str, list[str]] = {}
    for ch in list(parent):
        classes.setdefault(find(ch), []).append(ch)

    mapping: dict[str, str] = {}
    for members in classes.values():
        if len(members) < 2:
            continue
        simps = [m for m in members if m in simp_targets]
        canonical = sorted(simps)[0] if simps else sorted(members)[0]
        for m in members:
            if m != canonical:
                mapping[m] = canonical

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(mapping, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    print(f"wrote {OUT}: {len(mapping)} variant chars, {len(classes)} classes")


if __name__ == "__main__":
    main()
