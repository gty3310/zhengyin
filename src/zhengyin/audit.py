"""Final audit （终检）: per-character verification of a converted product.

Every Han character of the product text is classified against its target
reading, using the dictionary's *modern* reading fields (dict v0.3.1
schema: ``modern_readings`` / ``is_polyphonic`` / ``unique_reading``):

* A 铁证 — not polyphonic in modern Chinese and its unique modern
  reading is exactly the target;
* B 默认音 — polyphonic, target == default reading, not pinned: a
  default-reading TTS will *usually* say it right, but the engine may
  still drift with context — the position is not locked;
* C 钉音 — the position is pinned by an SSML hint, so the reading is
  forced regardless of the glyph's own readings;
* D 不合格 — polyphonic with target != default and neither pinned nor
  compliantly substituted; or the product glyph is not in the
  dictionary; or a non-polyphonic glyph's unique reading is not the
  target; or the target reading itself is undetermined.

The final state that exit code 0 certifies: no unlocked character —
every position is A, C, or a validated non-polyphonic substitute that
itself lands on A. B/D positions are *not* shown to the user: the
engine closes them itself (auto-pin, or a semantic-equivalent mapping
whose substitute passes the strict A validation), and this module only
detects and reports. Detection here is pure; resolution lives in the
engine, which re-runs this audit after resolving.
"""
from __future__ import annotations

from .pinyinutil import normalize_syllable, resolve_target, split_reading

CATEGORIES = ("A", "B", "C", "D")


def norm_sandhi(canon_char: str, reading: str | None) -> str | None:
    """Tone-sandhi normalisation for 一/不 (mirrors the engine's rule):
    yi2/yi4 are judged as yi1, bu2 as bu4 — sandhi is prosody, handled
    by the TTS itself, and must not fail the audit."""
    if reading is None:
        return None
    if canon_char == "一" and reading in ("yi2", "yi4"):
        return "yi1"
    if canon_char == "不" and reading == "bu2":
        return "bu4"
    return reading


def classify_one(d, out_char: str, target_raw: str | None, pinned: bool):
    """Classify one product position. Returns (category, basis, target_norm)."""
    norm = norm_sandhi(d.canonical(out_char), target_raw)
    if target_raw is None:
        return "D", "目标读音未定，无法通过终检", None
    if pinned:
        return "C", f"已钉音（SSML pin），目标音 {target_raw} 由标注强制", norm
    info = d.char_info(out_char)
    if info is None:
        return "D", f"成品字「{out_char}」不在字典字表（未知字），无法核验其读音", norm
    is_poly = info.get("is_polyphonic")
    if is_poly is None:
        is_poly = len(info.get("readings") or []) > 1
    unique = info.get("unique_reading")
    default = info.get("default")
    if not is_poly:
        if unique is not None and unique == norm:
            return "A", f"非多音字，唯一读音 {unique}＝目标音", norm
        return "D", f"成品字「{out_char}」唯一读音 {unique}≠目标音 {norm}", norm
    if norm == default:
        return ("B",
                f"成品字「{out_char}」为多音字，目标音 {norm}＝默认音，未钉音",
                norm)
    return ("D",
            f"成品字「{out_char}」为多音字，目标音 {norm}≠默认音 {default}，"
            "未钉音、未合规替换", norm)


def build_audit(d, positions: list[dict]) -> dict:
    """Audit a list of per-position traces (detection only).

    Each position: {han_index, index, char (original), out_char, target,
    pinned, via, sentence, prev, next}. A position is ``blocking`` when
    it is B or D, i.e. not locked.
    """
    details = []
    counts = {c: 0 for c in CATEGORIES}
    for pos in positions:
        cat, basis, norm = classify_one(
            d, pos["out_char"], pos.get("target"), bool(pos.get("pinned")))
        counts[cat] += 1
        details.append({
            "han_index": pos["han_index"],
            "index": pos["index"],
            "char": pos["char"],
            "out_char": pos["out_char"],
            "target": pos.get("target"),
            "target_norm": norm,
            "category": cat,
            "basis": basis,
            "action": ("pin" if pos.get("pinned")
                       else "substitute" if pos["out_char"] != pos["char"]
                       else "keep"),
            "via": pos.get("via", ""),
            "blocking": cat in ("B", "D"),
            "sentence": pos.get("sentence", ""),
            "prev": pos.get("prev", ""),
            "next": pos.get("next", ""),
        })
    return {
        "counts": {**counts, "total": len(details)},
        "details": details,
        "passed": not any(det["blocking"] for det in details),
    }


def audit_text(d, text: str, syllables: list[str],
               pinned_han: set | None = None) -> dict:
    """Standalone audit of an existing product text against a pinyin file.

    ``syllables`` aligns 1:1 with the Han characters of ``text`` (same
    alignment rule as convert's --pinyin). Detection only: a standalone
    product passes iff every character is already A or C (C requires
    the caller to supply the pin positions via ``pinned_han``).
    """
    from .engine import is_han  # local import: engine imports this module lazily

    han_pos = [i for i, c in enumerate(text) if is_han(c)]
    if len(syllables) != len(han_pos):
        raise ValueError(
            f"拼音数量（{len(syllables)}）与成品汉字数量（{len(han_pos)}）不一致，无法逐字对齐")
    pinned_han = pinned_han or set()
    sentences: list[tuple[int, int]] = []
    start = 0
    for idx, c in enumerate(text):
        if c in "。！？；\n":
            sentences.append((start, idx + 1))
            start = idx + 1
    if start < len(text):
        sentences.append((start, len(text)))
    if not sentences:
        sentences = [(0, len(text))]
    sent_texts = [text[a:b].strip() for a, b in sentences]

    def sent_of(pos: int) -> int:
        return next(k for k, (a, b) in enumerate(sentences) if a <= pos < b)

    positions = []
    for i, pos in enumerate(han_pos):
        ch = text[pos]
        info = d.char_info(ch)
        readings = list(info["readings"]) if info else []
        target, _stem, _tone = resolve_target(syllables[i], readings)
        if target is None and _tone is None and info:
            # A bare syllable (no tone) that matches exactly one
            # *modern* reading is determined for audit purposes:
            # modern Chinese is what the audit judges (e.g. 子 "zi"
            # classically zi3/zi5, modern only zi3).
            _m = [r for r in info.get("modern_readings") or []
                  if split_reading(r)[0] == _stem]
            if len(_m) == 1:
                target = _m[0]
        if target is None and _tone is not None:
            stem, tone = normalize_syllable(syllables[i])
            target = f"{stem}{tone}"
        si = sent_of(pos)
        positions.append({
            "han_index": i, "index": pos, "char": ch, "out_char": ch,
            "target": target, "pinned": i in pinned_han, "via": "",
            "sentence": sent_texts[si],
            "prev": sent_texts[si - 1] if si > 0 else "",
            "next": sent_texts[si + 1] if si + 1 < len(sent_texts) else "",
        })
    return build_audit(d, positions)
