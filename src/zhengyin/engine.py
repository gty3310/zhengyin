"""Conversion engine: deterministic reading assignment + replacement.

Implements the design principles of the zhengyin project:

* priority — learned layer > user pinyin lock > phrase table (longest
  match) > tongjia (sense-keyed) > podu (verified only for automatic
  use; unverified entries are question candidates, never silently
  applied) > heteronym table (context-example matching) > safe
  same-sound substitute;
* anything the algorithm cannot settle becomes a structured question
  (exit path for the calling agent to ask the user); user answers are
  validated against the dictionary before they are learned and used;
* the original text is never modified — replacements happen in the
  output string only, and every replacement is recorded for audit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .datalayer import Dict
from .learnstore import LearnStore
from .pinyinutil import resolve_target

_HAN_RANGES = (
    (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF),
    (0x20000, 0x2A6DF), (0x2A700, 0x2B73F), (0x2B740, 0x2B81F),
    (0x2B820, 0x2CEAF), (0x2CEB0, 0x2EBEF), (0x30000, 0x3134F),
)
_DELIMS = set("。！？；\n")


def is_han(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _HAN_RANGES)


_BENZI_RE = re.compile(r"通\s*[“\"「](.)[”\"」]")


def parse_benzi(sense: str) -> str | None:
    m = _BENZI_RE.search(sense or "")
    return m.group(1) if m else None


def gloss_of(sense: str) -> str:
    """Strip a leading 通“X” marker, leaving the sense gloss."""
    s = _BENZI_RE.sub("", sense or "", count=1)
    return s.strip(" ，,、；;").strip()


@dataclass
class Result:
    status: str  # "ok" | "questions"
    output: str = ""
    changes: list = field(default_factory=list)
    questions: list = field(default_factory=list)
    sentence_pinyin: list = field(default_factory=list)  # [(sentence, pinyin_line)]


class Engine:
    def __init__(self, d: Dict, learned: LearnStore):
        self.d = d
        self.learned = learned
        # heteronym assist index: canonical single-char word -> rows
        self._hetero: dict[str, list[dict]] = {}
        for row in d.heteronyms:
            w = row.get("word") or ""
            if len(w) == 1:
                self._hetero.setdefault(d.canonical(w), []).append(row)

    # ------------------------------------------------------------------
    def convert(self, text: str, pinyin: list[str] | None = None,
                answers: dict | None = None) -> Result:
        d = self.d
        answers = answers or {}
        han_pos = [i for i, c in enumerate(text) if is_han(c)]
        n = len(han_pos)
        if pinyin is not None and len(pinyin) != n:
            raise ValueError(
                f"拼音数量（{len(pinyin)}）与正文汉字数量（{n}）不一致，无法逐字对齐")

        han_chars = [text[i] for i in han_pos]
        han_canon = [d.canonical(c) for c in han_chars]
        han_canon_str = "".join(han_canon)

        sentences = self._sentences(text)  # list of (start, end) char spans
        sent_of = []
        for pos in han_pos:
            si = next(k for k, (a, b) in enumerate(sentences) if a <= pos < b)
            sent_of.append(si)
        sent_texts = [text[a:b].strip() for a, b in sentences]

        phrase_reading = self._phrase_match(han_chars, han_canon, han_canon_str)
        hetero_reading = self._hetero_match(han_chars, han_canon, han_canon_str)
        sent_han_canon: dict[int, str] = {}
        for j in range(n):
            sent_han_canon[sent_of[j]] = sent_han_canon.get(sent_of[j], "") + han_canon[j]

        out = list(text)
        changes: list[dict] = []
        questions: list[dict] = []
        final_reading: list[str | None] = [None] * n

        for i in range(n):
            ch = han_chars[i]
            si = sent_of[i]
            sentence = sent_texts[si]
            prev_s = sent_texts[si - 1] if si > 0 else ""
            next_s = sent_texts[si + 1] if si + 1 < len(sent_texts) else ""
            window = "".join(han_chars[max(0, i - 2): i + 3])
            info = d.char_info(ch)
            readings = list(info["readings"]) if info else []
            default = info["default"] if info else None

            # 0) learned layer ------------------------------------------------
            le = self.learned.match(ch, sentence, window)
            if le:
                out[han_pos[i]] = le["substitute"]
                final_reading[i] = le["reading"]
                changes.append({
                    "index": han_pos[i], "han_index": i, "char": ch,
                    "to": le["substitute"], "reading": le["reading"],
                    "via": "learned", "reading_via": "learned",
                    "sense": le.get("sense", ""),
                    "ref": "zhengyin learned layer (user_confirmed)",
                })
                continue

            # 1) target determination -----------------------------------------
            target: str | None = None
            tsrc: str | None = None
            stem: str | None = None
            if pinyin is not None:
                target, stem, _tone = resolve_target(pinyin[i], readings)
                if target:
                    tsrc = "pinyin"
            if target is None and phrase_reading[i]:
                target, tsrc = phrase_reading[i], "phrase"
            if target is None and hetero_reading[i]:
                target, tsrc = hetero_reading[i], "heteronym"

            tj = d.tongjia_entries(ch)
            pd = d.podu_entries(ch)
            qid = f"q{i}"

            def candidates_for(entries_at, podu_at, tgt):
                by_key: dict[tuple, dict] = {}

                def offer(char_, reading_, sense_, note=None):
                    key = (char_, reading_)
                    if key in by_key:
                        cur = by_key[key]
                        if sense_ and sense_ not in cur["sense"]:
                            cur["sense"] = (cur["sense"] + "；" + sense_).strip("；")
                        if note and "note" not in cur:
                            cur["note"] = note
                        return
                    rec = {"char": char_, "reading": reading_, "sense": sense_}
                    if note:
                        rec["note"] = note
                    by_key[key] = rec

                for e in entries_at:
                    offer(e["benzi"], e["reading"], e.get("sense", ""))
                for e in podu_at:
                    b = parse_benzi(e.get("sense", ""))
                    if b:
                        offer(b, e["reading"], gloss_of(e.get("sense", "")),
                              note="破读未核")
                if tgt:
                    sub = d.best_substitute(tgt)
                    if sub:
                        offer(sub, tgt, "", note="同音替字")
                return list(by_key.values())[:10]

            def ask(reason, entries_at, podu_at, tgt):
                """Raise (or answer) a question for this character.

                Returns True if the question stands, False if an answer
                resolved it.
                """
                ans = answers.get(qid)
                if ans is not None:
                    ok, eff, fail = self._validate_answer(ans, tgt)
                    if ok:
                        sense = ""
                        for c in candidates_for(entries_at, podu_at, tgt):
                            if c["char"] == ans:
                                sense = c.get("sense", "")
                                break
                        self.learned.add(ch, sentence, eff, ans, sense)
                        out[han_pos[i]] = ans
                        final_reading[i] = eff
                        changes.append({
                            "index": han_pos[i], "han_index": i, "char": ch,
                            "to": ans, "reading": eff, "via": "learned",
                            "reading_via": "answer",
                            "sense": sense, "ref": "user answer (validated)",
                        })
                        return False
                    reason = f"{reason}；上次答案「{ans}」未通过校验：{fail}"
                questions.append({
                    "id": qid, "char": ch, "index": han_pos[i],
                    "sentence": sentence, "prev": prev_s, "next": next_s,
                    "target_reading": tgt, "reason": reason,
                    "candidates": candidates_for(entries_at, podu_at, tgt),
                })
                return True

            def replace(to, reading, via, reading_via, sense="", ref="", note=""):
                out[han_pos[i]] = to
                final_reading[i] = reading
                rec = {"index": han_pos[i], "han_index": i, "char": ch,
                       "to": to, "reading": reading, "via": via,
                       "reading_via": reading_via, "sense": sense, "ref": ref}
                if note:
                    rec["note"] = note
                changes.append(rec)

            def tongjia_try(tgt, allow_corroboration):
                """Returns 'replaced' | 'asked' | None (no usable entry).

                At a non-default reading, the special reading itself
                signals a special usage, so tongjia entries at that
                reading are in scope. At the default reading the
                character is presumed ordinary: an entry applies only
                when its attested example matches this sentence's
                context — tongjia records are context-bound usages, and
                converting ordinary occurrences would corrupt the text.
                """
                entries_at = [e for e in tj if e["reading"] == tgt]
                if tgt == default:
                    entries_at = [
                        e for e in entries_at
                        if self._example_matches(e.get("example", ""),
                                                 sent_han_canon[si])]
                if not entries_at:
                    return None
                benzis: dict[str, list[dict]] = {}
                for e in entries_at:
                    benzis.setdefault(e["benzi"], []).append(e)
                if len(benzis) == 1:
                    b, es = next(iter(benzis.items()))
                    if d.benzi_valid(b, tgt):
                        sense = "；".join(
                            dict.fromkeys(e.get("sense", "") for e in es if e.get("sense")))
                        ref = "；".join(
                            dict.fromkeys(e.get("src_ref") or e.get("src", "") for e in es))
                        replace(b, tgt, "tongjia", tsrc or "default", sense, ref)
                        return "replaced"
                    return None
                # several benzi compete
                if allow_corroboration:
                    pick = self._podu_corroboration(pd, tgt, han_canon_str, benzis)
                    if pick and d.benzi_valid(pick, tgt):
                        es = benzis[pick]
                        sense = "；".join(
                            dict.fromkeys(e.get("sense", "") for e in es if e.get("sense")))
                        ref = "；".join(
                            dict.fromkeys(e.get("src_ref") or e.get("src", "") for e in es))
                        replace(pick, tgt, "tongjia", tsrc or "default", sense, ref,
                                note="多本字竞争，由破读语境条目旁证消歧（读音已由用户拼音锁定）")
                        return "replaced"
                pd_at = [e for e in pd if e["reading"] == tgt]
                ask(f"通假多义项竞争：{ch} 读 {tgt} 时可通 "
                    + "、".join(f"{b}（{'/'.join(sorted({e.get('sense','') for e in es if e.get('sense')})) or '义项待补'}）"
                                for b, es in benzis.items())
                    + "，上下文不足以定夺",
                    entries_at, pd_at, tgt)
                return "asked"

            # 2) resolution ----------------------------------------------------
            if target is not None:
                r = tongjia_try(target, allow_corroboration=(tsrc == "pinyin"))
                if r:
                    continue
                if target != default:
                    pd_at = [e for e in pd if e["reading"] == target]
                    done = False
                    for e in pd_at:
                        if e.get("verified") in ("human_checked", "user_confirmed"):
                            b = parse_benzi(e.get("sense", ""))
                            if b and d.benzi_valid(b, target):
                                replace(b, target, "podu", tsrc or "",
                                        gloss_of(e.get("sense", "")),
                                        e.get("src", ""))
                                done = True
                                break
                    if done:
                        continue
                    if pd_at:
                        ask(f"破读条目建议 {ch} 读 {target}，但该条目未经核验，"
                            "不敢直接落字", [], pd_at, target)
                        continue
                    sub = d.best_substitute(target)
                    if sub:
                        replace(sub, target, "substitute", tsrc or "",
                                "", "substitutes index (single-reading, default=target)")
                        continue
                    ask(f"{ch} 应读 {target}，但字典中无本字映射、"
                        "同音替字索引亦无安全候选", [], [], target)
                    continue
                # target == default and no tongjia replacement: keep
                final_reading[i] = target
                continue

            # no target determined yet
            if len(readings) == 1:
                t = readings[0]
                r = tongjia_try(t, allow_corroboration=False)
                if r:
                    continue
                final_reading[i] = t
                continue
            # No reading determined. Ask only on positive evidence of a
            # special usage — a curated podu entry, genuinely competing
            # benzi at non-default readings, or an entry whose attested
            # example matches this sentence. A lone obscure tongjia
            # record (e.g. 而→能) is not evidence about *this* sentence;
            # asking on it would flood every classical text with noise.
            nondefault_benzis = {e["benzi"] for e in tj
                                 if e["reading"] != default}
            evidence = (
                bool(pd)
                or len(nondefault_benzis) >= 2
                or any(self._example_matches(e.get("example", ""),
                                             sent_han_canon[si]) for e in tj)
                or any(self._example_matches(e.get("context", ""),
                                             sent_han_canon[si]) for e in pd)
            )
            if evidence:
                ask(f"{ch} 为多音字（可读 {'、'.join(readings)}），"
                    "且字典中有通假/破读条目，上下文不足以定音",
                    tj, pd, None)
                continue
            final_reading[i] = default  # nothing suggests a non-default reading

        if questions:
            return Result(status="questions", questions=questions, changes=changes)

        # sentence pinyin (converted text, per sentence)
        sent_py: list[tuple[str, str]] = []
        for si, (a, b) in enumerate(sentences):
            conv = "".join(out[a:b]).strip()
            syl = [final_reading[i] or "—"
                   for i in range(n) if sent_of[i] == si]
            sent_py.append((conv, " ".join(syl)))
        return Result(status="ok", output="".join(out), changes=changes,
                      sentence_pinyin=sent_py)

    # ------------------------------------------------------------------
    def _example_matches(self, example: str, sent_han_canon: str) -> bool:
        """Does a tongjia entry's attested example match this sentence?

        Comparison is on canonical (glyph-normalised) Han sequences:
        containment in either direction, or a shared 5-character window.
        """
        if not example or not sent_han_canon:
            return False
        ex = "".join(self.d.canonical(c) for c in example if is_han(c))
        if not ex:
            return False
        if ex in sent_han_canon or sent_han_canon in ex:
            return True
        L = min(5, len(ex))
        return any(ex[k:k + L] in sent_han_canon
                   for k in range(len(ex) - L + 1))

    # ------------------------------------------------------------------
    def _validate_answer(self, ans: str, target: str | None):
        """Validate a user's proposed character.

        Passes when the character's readings include the target reading
        and its default reading *is* the target (so a default-reading
        TTS cannot drift). With no known target, the answer's own
        default reading becomes the target.
        """
        if len(ans) != 1 or not is_han(ans):
            return False, None, "答案必须是单个汉字"
        info = self.d.char_info(ans)
        if not info:
            return False, None, f"「{ans}」不在字典字表中，无法校验其读音"
        if target is not None:
            if target not in info["readings"]:
                return False, None, (
                    f"「{ans}」的读音为 {'、'.join(info['readings'])}，"
                    f"不含目标读音 {target}")
            if info["default"] != target:
                return False, None, (
                    f"「{ans}」默认读音为 {info['default']}，不等于目标读音 "
                    f"{target}，TTS 仍可能读偏")
            return True, target, ""
        return True, info["default"], ""

    def _podu_corroboration(self, podu_entries, target, han_canon_str, benzis):
        """Pick a benzi among competing tongjia candidates using a podu
        entry whose quoted classical context matches this sentence.

        Only used when the reading is already locked by the user's own
        pinyin: the (unverified) podu entry never creates a mapping, it
        only arbitrates between authoritative tongjia candidates, and
        the arbitration is recorded in the change log.
        """
        canon_benzis = {self.d.canonical(b): b for b in benzis}
        for e in podu_entries:
            if e["reading"] != target:
                continue
            b = parse_benzi(e.get("sense", ""))
            if not b:
                continue
            quotes = re.findall(r"「([^」]+)」", e.get("context", ""))
            for q in quotes:
                q_han = "".join(self.d.canonical(c) for c in q if is_han(c))
                if q_han and q_han in han_canon_str:
                    hit = canon_benzis.get(self.d.canonical(b))
                    if hit:
                        return hit
        return None

    # ------------------------------------------------------------------
    def _sentences(self, text: str) -> list[tuple[int, int]]:
        spans, start = [], 0
        for idx, c in enumerate(text):
            if c in _DELIMS:
                spans.append((start, idx + 1))
                start = idx + 1
        if start < len(text):
            spans.append((start, len(text)))
        return spans or [(0, len(text))]

    def _phrase_match(self, han_chars, han_canon, han_canon_str):
        d = self.d
        n = len(han_chars)
        result: list[str | None] = [None] * n
        covered = [False] * n
        for i in range(n):
            if covered[i]:
                continue
            for canon_phrase, pinyins in d.phrases.get(han_canon[i], []):
                L = len(canon_phrase)
                if i + L > n or han_canon_str[i:i + L] != canon_phrase:
                    continue
                if any(covered[i + j] for j in range(L)):
                    continue
                if all(pinyins[j] in d.readings_of(han_chars[i + j])
                       for j in range(L)):
                    for j in range(L):
                        result[i + j] = pinyins[j]
                        covered[i + j] = True
                    break
        return result

    def _hetero_match(self, han_chars, han_canon, han_canon_str):
        d = self.d
        n = len(han_chars)
        result: list[str | None] = [None] * n
        for i in range(n):
            for row in self._hetero.get(han_canon[i], []):
                hit = None
                for r in row.get("readings", []):
                    for ex in r.get("examples") or []:
                        ex_canon = "".join(d.canonical(c) for c in ex if is_han(c))
                        if not ex_canon or han_canon[i] not in ex_canon:
                            continue
                        k = ex_canon.index(han_canon[i])
                        start = i - k
                        if start >= 0 and han_canon_str[start:start + len(ex_canon)] == ex_canon:
                            hit = r.get("pinyin")
                            break
                    if hit:
                        break
                if hit and hit in d.readings_of(han_chars[i]):
                    result[i] = hit
                    break
        return result
