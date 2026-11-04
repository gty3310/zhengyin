"""Pinyin normalisation helpers.

Internal canonical form is numbered pinyin as used by zhengyin-dict
(``yue4``; neutral tone = 5; ``ü`` kept as ``ü``). User input may carry
tone marks (``yuè``), tone numbers (``yue4``), ``v``/``u:`` for ``ü``,
or no tone at all (then only the syllable stem is known).
"""
from __future__ import annotations

_TONE_GROUPS = {
    "a": "āáǎà",
    "e": "ēéěè",
    "i": "īíǐì",
    "o": "ōóǒò",
    "u": "ūúǔù",
    "ü": "ǖǘǚǜ",
    "m": "ḿ",
    "n": "ńňǹ",
}
_TONE_MAP: dict[str, tuple[str, int]] = {}
for _base, _forms in _TONE_GROUPS.items():
    for _i, _ch in enumerate(_forms):
        _TONE_MAP[_ch] = (_base, _i + 1 if _i < 4 else 2)
        _TONE_MAP[_ch.upper()] = (_base, _i + 1 if _i < 4 else 2)
# ḿ is tone 2; ń tone 2, ň tone 3, ǹ tone 4 (fixed above approximately for m/n)


def split_reading(reading: str) -> tuple[str, int | None]:
    """Split a numbered reading like ``yue4`` into (``yue``, 4)."""
    if reading and reading[-1].isdigit():
        return reading[:-1], int(reading[-1])
    return reading, None


def normalize_syllable(raw: str) -> tuple[str, int | None]:
    """Normalise one user-supplied syllable to (stem, tone|None).

    Accepts tone marks, tone numbers, ``v``/``u:`` for ``ü``; a bare
    syllable yields tone=None (only the stem is asserted).
    """
    s = raw.strip()
    if not s:
        return "", None
    tone: int | None = None
    if s[-1].isdigit():
        tone = int(s[-1])
        s = s[:-1]
    s = s.replace("u:", "ü").replace("U:", "ü")
    chars: list[str] = []
    for ch in s:
        low = ch.lower()
        if low == "v":
            chars.append("ü")
        elif ch in _TONE_MAP:
            base, t = _TONE_MAP[ch]
            chars.append(base)
            if tone is None:
                tone = t
        else:
            chars.append(low)
    return "".join(chars), tone


def reading_matches(reading: str, stem: str, tone: int | None) -> bool:
    """True if numbered ``reading`` agrees with (stem, tone|None)."""
    r_stem, r_tone = split_reading(reading)
    if r_stem != stem:
        return False
    return tone is None or r_tone == tone


def resolve_target(
    syllable_raw: str, readings: list[str]
) -> tuple[str | None, str, int | None]:
    """Resolve a user syllable against a character's readings.

    Returns (target_reading|None, stem, tone|None). A full numbered
    reading is produced when the tone is given, or when the stem matches
    exactly one reading. Otherwise the target stays open (stem known).
    """
    stem, tone = normalize_syllable(syllable_raw)
    if tone is not None:
        return f"{stem}{tone}", stem, tone
    matches = [r for r in readings if split_reading(r)[0] == stem]
    if len(matches) == 1:
        return matches[0], stem, tone
    return None, stem, tone
