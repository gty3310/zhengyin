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
from .pinyinutil import resolve_target, split_reading

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
    ssml_hints: list = field(default_factory=list)
    ssml_output: str = ""
    audit: dict = field(default_factory=dict)
    semantic_questions: list = field(default_factory=list)


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

        # Preliminary per-char targets (pinyin lock > phrase > heteronym),
        # computed once: prescribed-phrase conflict checks and group-answer
        # key resolution both need targets for *other* positions.
        prelim_target: list[str | None] = [None] * n
        prelim_raw: list[str | None] = [None] * n
        prelim_src: list[str | None] = [None] * n
        for _i in range(n):
            _info = d.char_info(han_chars[_i])
            _readings = list(_info["readings"]) if _info else []
            if pinyin is not None:
                _t, _stem, _tone = resolve_target(pinyin[_i], _readings)
                if _t:
                    _raw = _t
                    if han_canon[_i] == "一" and _t in ("yi2", "yi4"):
                        _t = "yi1"
                    elif han_canon[_i] == "不" and _t == "bu2":
                        _t = "bu4"
                    prelim_target[_i], prelim_raw[_i], prelim_src[_i] = (
                        _t, _raw, "pinyin")
            if prelim_target[_i] is None and phrase_reading[_i]:
                prelim_target[_i] = phrase_reading[_i]
                prelim_raw[_i] = phrase_reading[_i]
                prelim_src[_i] = "phrase"
            if prelim_target[_i] is None and hetero_reading[_i]:
                prelim_target[_i] = hetero_reading[_i]
                prelim_raw[_i] = hetero_reading[_i]
                prelim_src[_i] = "heteronym"

        # Answers keyed by any occurrence id of a (char, target) group
        # resolve that whole group: map group_key -> answer value.
        group_answer_map: dict[str, str] = {}
        for _k, _v in answers.items():
            if isinstance(_k, str) and _k.startswith("q") and _k[1:].isdigit():
                _idx = int(_k[1:])
                if 0 <= _idx < n and prelim_target[_idx] is not None:
                    group_answer_map[
                        f"{han_chars[_idx]}|{prelim_target[_idx]}"] = _v

        prescribed_phrase = self._prescribed_phrase_match(
            han_chars, han_canon, han_canon_str, prelim_target)
        sent_han_canon: dict[int, str] = {}
        for j in range(n):
            sent_han_canon[sent_of[j]] = sent_han_canon.get(sent_of[j], "") + han_canon[j]

        out = list(text)
        changes: list[dict] = []
        questions: list[dict] = []
        ssml_hints: list[dict] = []
        semantic_raw: list[dict] = []
        mapping_rejections: list[dict] = []
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
            # (precomputed: pinyin lock > phrase > heteronym)
            target: str | None = prelim_target[i]
            raw: str | None = prelim_raw[i]
            tsrc: str | None = prelim_src[i]
            stem: str | None = None

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
                cands = []
                for rec in by_key.values():
                    ci = d.char_info(rec["char"])
                    if not ci:
                        continue
                    if rec["reading"] not in ci["readings"]:
                        continue
                    if ci["default"] != rec["reading"]:
                        continue
                    cands.append(rec)

                def _cand_key(rec):
                    ci = d.char_info(rec["char"]) or {}
                    rank = ci.get("freq_rank")
                    return (0 if ci.get("in_8105") else 1,
                            0 if rank is not None else 1,
                            rank if rank is not None else 1 << 30)

                cands.sort(key=_cand_key)
                return cands[:10]

            def ask(reason, entries_at, podu_at, tgt, group=False):
                """Raise (or answer) a question for this character.

                Returns True if the question stands, False if an answer
                resolved it. With ``group=True`` (or an answer keyed by
                any occurrence id of the same (char, target) group) the
                answer adjudicates the whole group: a single character
                (validated, then learned as a group ruling), ``"ssml"``
                (pin every occurrence) or ``"keep"`` (keep every
                occurrence, reading recorded, no change).
                """
                ans = answers.get(qid)
                via_group_key = False
                if ans is None and tgt is not None:
                    gans = group_answer_map.get(f"{ch}|{tgt}")
                    if gans is not None:
                        ans = gans
                        via_group_key = True
                is_group = group or via_group_key
                if ans is not None:
                    if ans in ("ssml", "keep") and tgt is not None:
                        self.learned.add_group(
                            ch, tgt, "ssml" if ans == "ssml" else "keep")
                        if ans == "ssml":
                            apply_pin(tgt)
                        else:
                            final_reading[i] = tgt
                        return False
                    ok, eff, fail = self._validate_answer(ans, tgt)
                    if ok:
                        sense = ""
                        for c in candidates_for(entries_at, podu_at, tgt):
                            if c["char"] == ans:
                                sense = c.get("sense", "")
                                break
                        if is_group and tgt is not None:
                            self.learned.add_group(
                                ch, tgt, "substitute", ans)
                        else:
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
                cands = candidates_for(entries_at, podu_at, tgt)
                if not cands:
                    reason = f"{reason}（暂无通过校验的候选，可自答他字或保持原字）"
                qrec = {
                    "id": qid, "char": ch, "index": han_pos[i],
                    "sentence": sentence, "prev": prev_s, "next": next_s,
                    "target_reading": tgt, "reason": reason,
                    "candidates": cands,
                }
                if group and tgt is not None:
                    qrec["group"] = True
                    qrec["group_key"] = f"{ch}|{tgt}"
                    qrec["occurrences"] = [{
                        "id": qid, "index": han_pos[i],
                        "sentence": sentence, "prev": prev_s, "next": next_s,
                    }]
                questions.append(qrec)
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

            def apply_pin(tgt, note="", ref="ssml pin (group ruling)"):
                """SSML pin: keep the original char, record its reading."""
                final_reading[i] = tgt
                rec = {
                    "index": han_pos[i], "han_index": i, "char": ch,
                    "to": ch, "reading": tgt, "via": "pin",
                    "reading_via": tsrc or "",
                    "ref": ref,
                }
                if note:
                    rec["note"] = note
                changes.append(rec)
                ssml_hints.append({
                    "index": han_pos[i], "han_index": i, "char": ch,
                    "reading": tgt, "sentence": sentence,
                    "ssml": (f'<phoneme alphabet="py" ph="{tgt}">'
                             f"{ch}</phoneme>"),
                })

            def apply_group_ruling(gr, tgt):
                """Apply a learned group ruling for (char, target)."""
                action = gr.get("action")
                if action == "substitute" and gr.get("substitute"):
                    replace(gr["substitute"], tgt, "learned", "group",
                            "", "zhengyin learned layer (group ruling)")
                elif action == "ssml":
                    apply_pin(tgt)
                elif action == "keep":
                    final_reading[i] = raw if raw is not None else tgt
                else:
                    return False
                return True

            def record_semantic(kind, tgt, cand_list, rec_action,
                                rec_char, rec_reason):
                semantic_raw.append({
                    "han_index": i, "char": ch, "target": tgt,
                    "kind": kind, "candidates": cand_list,
                    "recommendation": {"action": rec_action,
                                       "char": rec_char,
                                       "reason": rec_reason},
                    "sentence": sentence, "index": han_pos[i],
                })

            def record_rejection(tgt, benzi, via):
                mapping_rejections.append({
                    "han_index": i, "char": ch, "reading": tgt,
                    "substitute": benzi, "via": via,
                    "reason": (f"替字「{benzi}」未过终检校验（须非多音字且"
                               f"唯一读音＝{tgt}），已驳回、改钉音原字"),
                    "sentence": sentence,
                })

            def benzi_candidates(benzis_map, tgt):
                out_c = []
                for b, es in benzis_map.items():
                    sense = "/".join(sorted(
                        {e.get("sense", "") for e in es if e.get("sense")}))
                    out_c.append({"char": b, "reading": tgt,
                                  "sense": sense or "义项待补"})
                return out_c

            def tongjia_try(tgt, allow_corroboration):
                """Returns 'replaced' | 'asked' | 'kept' | None (no usable entry).

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
                    # Default-reading floor: a benzi is auto-usable only
                    # when the full tongjia table holds >=2 entries for
                    # it at this reading (counted over all of tj, not
                    # just the example-filtered subset).
                    from collections import Counter
                    full_counts = Counter(
                        e["benzi"] for e in tj if e["reading"] == tgt)
                    benzis_all: dict[str, list[dict]] = {}
                    for e in entries_at:
                        benzis_all.setdefault(e["benzi"], []).append(e)
                    qualifying = {b for b in benzis_all
                                  if full_counts.get(b, 0) >= 2}
                    if not qualifying:
                        # v0.4.0: sound is unaffected (target == default);
                        # whether to swap in the benzi is a *semantic*
                        # judgement on isolated evidence — it goes to
                        # semantic_questions, never to a sound question.
                        _bc = benzi_candidates(benzis_all, tgt)
                        record_semantic(
                            "isolated_evidence", tgt, _bc, "substitute",
                            _bc[0]["char"] if _bc else None,
                            "例句与本文语境吻合，但该本字在全表仅单条通假"
                            "孤证：声音层保留原字，义项是否换本字请用户确认")
                        return "kept"
                    if len(qualifying) == 1:
                        b = next(iter(qualifying))
                        es = benzis_all[b]
                        if d.strict_substitute_ok(b, tgt):
                            sense = "；".join(
                                dict.fromkeys(e.get("sense", "") for e in es if e.get("sense")))
                            ref = "；".join(
                                dict.fromkeys(e.get("src_ref") or e.get("src", "") for e in es))
                            replace(b, tgt, "tongjia", tsrc or "default", sense, ref)
                            return "replaced"
                        if d.benzi_valid(b, tgt):
                            record_rejection(tgt, b, "tongjia")
                            record_semantic(
                                "substitute_rejected", tgt,
                                benzi_candidates(benzis_all, tgt),
                                "keep_pinned", None,
                                f"通假本字「{b}」语义等值，但其本身为多音字、"
                                "未过声音校验，声音层不自动落字；是否换字请"
                                "用户确认语义")
                            return "kept"
                        return None
                    entries_at = [e for e in entries_at
                                  if e["benzi"] in qualifying]
                    benzis = {b: es for b, es in benzis_all.items()
                              if b in qualifying}
                else:
                    if not entries_at:
                        return None
                    benzis: dict[str, list[dict]] = {}
                    for e in entries_at:
                        benzis.setdefault(e["benzi"], []).append(e)
                    if len(benzis) == 1:
                        b, es = next(iter(benzis.items()))
                        if d.strict_substitute_ok(b, tgt):
                            sense = "；".join(
                                dict.fromkeys(e.get("sense", "") for e in es if e.get("sense")))
                            ref = "；".join(
                                dict.fromkeys(e.get("src_ref") or e.get("src", "") for e in es))
                            replace(b, tgt, "tongjia", tsrc or "default", sense, ref)
                            return "replaced"
                        if d.benzi_valid(b, tgt):
                            record_rejection(tgt, b, "tongjia")
                            apply_pin(tgt, note="通假替字未过终检校验，驳回改钉音",
                                      ref="ssml pin (auto, v0.4.0 sound closure)")
                            return "pinned"
                        return None
                # several benzi compete
                pick = None
                if allow_corroboration:
                    pick = self._podu_corroboration(pd, tgt, han_canon_str, benzis)
                    if pick and d.benzi_valid(pick, tgt) \
                            and not d.strict_substitute_ok(pick, tgt):
                        record_rejection(tgt, pick, "tongjia")
                        record_semantic(
                            "substitute_rejected", tgt,
                            benzi_candidates(benzis, tgt), "keep_pinned", None,
                            f"破读旁证荐本字「{pick}」，但其本身为多音字、"
                            "未过声音校验；是否换字请用户确认语义")
                        pick = None
                    if pick and d.strict_substitute_ok(pick, tgt):
                        es = benzis[pick]
                        sense = "；".join(
                            dict.fromkeys(e.get("sense", "") for e in es if e.get("sense")))
                        ref = "；".join(
                            dict.fromkeys(e.get("src_ref") or e.get("src", "") for e in es))
                        replace(pick, tgt, "tongjia", tsrc or "default", sense, ref,
                                note="多本字竞争，由破读语境条目旁证消歧（读音已由用户拼音锁定）")
                        return "replaced"
                # Evidence gate (non-default, user-pinyin only): when no
                # competing entry's example matches this sentence and no
                # corroboration hit, keep the original character instead
                # of asking.
                if tgt != default and tsrc == "pinyin":
                    competed = [e for es in benzis.values() for e in es]
                    if not pick and not any(
                            self._example_matches(e.get("example", ""),
                                                  sent_han_canon[si])
                            for e in competed):
                        record_semantic(
                            "competition", tgt,
                            benzi_candidates(benzis, tgt), "keep_pinned",
                            None,
                            "多本字竞争且无例句语境吻合、无旁证命中：保字"
                            "保义优先，声音层钉音解决；义项请用户确认")
                        return "kept"
                # v0.4.0: competing benzi is a semantic judgement; the
                # sound layer closes by pinning (non-default) or by the
                # final audit (default). It never becomes a sound question.
                record_semantic(
                    "competition", tgt, benzi_candidates(benzis, tgt),
                    "keep_pinned", None,
                    "多义项通假竞争、上下文不足以定夺：保字保义优先，"
                    "声音层钉音解决；义项请用户确认")
                if tgt != default:
                    apply_pin(tgt, note="通假多义项竞争，声音层自动钉音",
                              ref="ssml pin (auto, v0.4.0 sound closure)")
                    return "pinned"
                return "kept"

            # 2) resolution ----------------------------------------------------
            # Priority: learned group ruling > prescribed > tongjia/podu
            # > substitute index. (Learned context match ran above.)
            if i in prescribed_phrase:
                pp = prescribed_phrase[i]
                eff = target if target is not None else pp["reading"]
                gr = self.learned.match_group(ch, eff)
                if gr and apply_group_ruling(gr, eff):
                    continue
                replace(pp["substitute"], pp["reading"], "prescribed",
                        tsrc or "prescribed", "", pp.get("src_ref", ""))
                continue
            if target is not None:
                gr = self.learned.match_group(ch, target)
                if gr and apply_group_ruling(gr, target):
                    continue
                pe = d.prescribed_for(ch, target)
                if pe:
                    replace(pe["substitute"], target, "prescribed",
                            tsrc or "", "", pe.get("src_ref")
                            or pe.get("src", ""))
                    continue
                r = tongjia_try(target, allow_corroboration=(tsrc == "pinyin"))
                if r == "kept":
                    final_reading[i] = raw if raw is not None else target
                    continue
                if r:
                    continue
                if target != default:
                    pd_at = [e for e in pd if e["reading"] == target]
                    done = False
                    for e in pd_at:
                        if e.get("verified") in ("human_checked", "user_confirmed"):
                            b = parse_benzi(e.get("sense", ""))
                            if b and d.benzi_valid(b, target) \
                                    and not d.strict_substitute_ok(b, target):
                                record_rejection(target, b, "podu")
                                continue
                            if b and d.strict_substitute_ok(b, target):
                                replace(b, target, "podu", tsrc or "",
                                        gloss_of(e.get("sense", "")),
                                        e.get("src", ""))
                                done = True
                                break
                    if done:
                        continue
                    # v0.4.0 sound closure: the target reading is locked
                    # but no semantic-equivalent mapping landed. Sound
                    # problems are never put to the user and the pure
                    # homophone substitute index no longer lands
                    # automatically — the position is pinned by SSML.
                    if info and target in (info.get("common") or []):
                        _note = ("常用读音≠默认读音且已被锁定，自动钉音"
                                 "（v0.4.0 声音闭环）")
                    elif pd_at:
                        _note = ("破读条目未核、读音已锁定，自动钉音"
                                 "（v0.4.0 声音闭环）")
                    else:
                        _note = ("无语义等值映射，自动钉音；纯同音替字索引"
                                 "不再自动落字（v0.4.0 声音闭环）")
                    apply_pin(target, note=_note,
                              ref="ssml pin (auto, v0.4.0 sound closure)")
                    continue
                # target == default and no tongjia replacement: keep
                final_reading[i] = raw if raw is not None else target
                continue

            # no target determined yet
            if info is None:
                # Unknown glyph with no locked reading: a genuine
                # residual — the executing agent must answer it.
                ask(f"{ch} 不在字典字表（未知字），且无拼音锁定读音，"
                    "无法定音", [], [], None)
                continue
            if len(readings) == 1:
                t = readings[0]
                r = tongjia_try(t, allow_corroboration=False)
                if r == "kept":
                    final_reading[i] = t
                    continue
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

        # ---- v0.4.0 final audit （终检） + sound closure -----------------
        # Detect B/D on the provisional product, then close every
        # closable position here (auto-pin; a semantic-equivalent
        # substitute only if it passes the strict A validation) and
        # re-audit. Positions already covered by a residual sound
        # question (target undetermined / unknown glyph) are left to
        # the question loop. Exit 0 <=> final audit fully passed.
        from .audit import build_audit as _build_audit

        _covered_han: set = set()
        _q_target: dict = {}
        for _q in questions:
            _qid = _q.get("id", "")
            if _qid.startswith("q") and _qid[1:].isdigit():
                _covered_han.add(int(_qid[1:]))
                _q_target[int(_qid[1:])] = _q.get("target_reading")

        def _positions():
            pin_han = {h["han_index"] for h in ssml_hints}
            ch_by_han = {}
            for c in changes:
                ch_by_han[c["han_index"]] = c
            pos = []
            for i in range(n):
                tgt = final_reading[i]
                if tgt is None:
                    tgt = _q_target.get(i)
                rec = ch_by_han.get(i)
                si = sent_of[i]
                pos.append({
                    "han_index": i, "index": han_pos[i], "char": han_chars[i],
                    "out_char": out[han_pos[i]], "target": tgt,
                    "pinned": i in pin_han,
                    "via": rec.get("via", "") if rec else "",
                    "sentence": sent_texts[si],
                    "prev": sent_texts[si - 1] if si > 0 else "",
                    "next": sent_texts[si + 1] if si + 1 < len(sent_texts) else "",
                })
            return pos

        _detected = _build_audit(d, _positions())
        _detected_by_han = {det["han_index"]: det for det in _detected["details"]}
        resolutions: list[dict] = []

        def _audit_pin(det, rule, note):
            i = det["han_index"]
            oc = det["out_char"]
            if not any(h["han_index"] == i for h in ssml_hints):
                ssml_hints.append({
                    "index": han_pos[i], "han_index": i, "char": oc,
                    "reading": det["target_norm"], "sentence": det["sentence"],
                    "ssml": (f'<phoneme alphabet="py" '
                             f'ph="{det["target_norm"]}">{oc}</phoneme>'),
                })
                changes.append({
                    "index": han_pos[i], "han_index": i,
                    "char": han_chars[i], "to": oc,
                    "reading": det["target_norm"], "via": "pin",
                    "reading_via": "audit-auto",
                    "ref": "ssml pin (audit closure, v0.4.0)",
                    "note": note,
                })
            resolutions.append({
                "han_index": i, "char": det["char"], "out_char": oc,
                "target": det["target_norm"], "detected": det["category"],
                "rule": rule, "sentence": det["sentence"],
            })

        for det in _detected["details"]:
            if not det["blocking"]:
                continue
            i = det["han_index"]
            if i in _covered_han or det["target_norm"] is None:
                continue
            tgt = det["target_norm"]
            orig = han_chars[i]
            oc = det["out_char"]
            if oc != orig:
                # A substitute already landed (learned / prescribed).
                _oinfo = d.char_info(oc)
                if det["category"] == "B":
                    _audit_pin(det, "rule1_default_pin",
                               "已落替字为多音字、目标音＝其默认音，钉音锁定")
                else:
                    # The landed substitute fails the audit itself:
                    # reject it, revert to the original glyph, pin that.
                    mapping_rejections.append({
                        "han_index": i, "char": orig, "reading": tgt,
                        "substitute": oc,
                        "via": det.get("via", ""),
                        "reason": (f"已落替字「{oc}」未过终检"
                                   f"（{det['basis']}），已驳回、改钉音原字"),
                        "sentence": det["sentence"],
                    })
                    out[han_pos[i]] = orig
                    det2 = dict(det)
                    det2["out_char"] = orig
                    _audit_pin(det2, "rule2_mapping_rejected_pin",
                               f"替字{oc}未过终检，驳回改钉音原字")
                continue
            # Original glyph kept: try a semantic-equivalent mapping
            # (tongjia single benzi / prescribed) under strict validation.
            # Only a prescribed mapping may land at the audit stage:
            # tongjia benzi choices are semantic-gated (competition /
            # isolated evidence) and are adjudicated in the engine's
            # tongjia pass under its evidence rules, never here.
            _cand = None
            _pe = d.prescribed_for(orig, tgt)
            if _pe:
                _cand = _pe["substitute"]
            _landed = False
            if _cand is not None:
                if d.strict_substitute_ok(_cand, tgt):
                    out[han_pos[i]] = _cand
                    final_reading[i] = tgt
                    changes.append({
                        "index": han_pos[i], "han_index": i, "char": orig,
                        "to": _cand, "reading": tgt, "via": "tongjia",
                        "reading_via": "audit",
                        "ref": "semantic-equivalent mapping (audit closure)",
                    })
                    resolutions.append({
                        "han_index": i, "char": orig, "out_char": _cand,
                        "target": tgt, "detected": det["category"],
                        "rule": "rule2_semantic_substitute",
                        "sentence": det["sentence"],
                    })
                    _landed = True
                elif d.benzi_valid(_cand, tgt):
                    mapping_rejections.append({
                        "han_index": i, "char": orig, "reading": tgt,
                        "substitute": _cand, "via": "mapping",
                        "reason": (f"语义等值替字「{_cand}」未过终检校验"
                                   "（须非多音字且唯一读音＝目标音），"
                                   "已驳回、改钉音原字"),
                        "sentence": det["sentence"],
                    })
            if _landed:
                continue
            _oinfo = d.char_info(orig)
            if _oinfo and not _oinfo.get("is_polyphonic") \
                    and _oinfo.get("unique_reading"):
                _odef = _oinfo["unique_reading"]
            else:
                _odef = _oinfo["default"] if _oinfo else None
            _tstem = split_reading(tgt)[0]
            _dstem = split_reading(_odef)[0] if _odef else None
            if _tstem is not None and _tstem == _dstem:
                if tgt != _odef:
                    _rule = "rule1_tone_only_pin"
                    _note = "与默认读音只差声调，自动钉调"
                else:
                    _rule = "rule1_default_pin"
                    _note = "多音字目标音＝默认音，自动钉音锁定"
            elif any(r["han_index"] == i for r in mapping_rejections):
                _rule = "rule2_mapping_rejected_pin"
                _note = "语义替字未过校验，驳回改钉音原字"
            else:
                _rule = "rule2_syllable_pin"
                _note = "音节不同且无语义等值映射，自动钉全读音"
            _audit_pin(det, _rule, _note)

        audit = _build_audit(d, _positions())
        audit["detected_counts"] = _detected["counts"]
        for det in audit["details"]:
            _dd = _detected_by_han.get(det["han_index"])
            det["detected_category"] = _dd["category"] if _dd else det["category"]
        audit["resolutions"] = resolutions
        audit["mapping_rejections"] = mapping_rejections

        # Group semantic questions by (char, target, kind).
        from collections import OrderedDict as _OD3
        _sgroups: dict = _OD3()
        for _sr in semantic_raw:
            _sgroups.setdefault(
                (_sr["char"], _sr["target"], _sr["kind"]), []).append(_sr)
        semantic_questions: list[dict] = []
        for _k, (_key, _occs) in enumerate(_sgroups.items()):
            _ch, _tgt, _kind = _key
            _first = _occs[0]
            semantic_questions.append({
                "id": f"s{_k + 1}",
                "char": _ch,
                "target_reading": _tgt,
                "kind": _kind,
                "group": True,
                "group_key": f"{_ch}|{_tgt}|{_kind}",
                "candidates": _first["candidates"],
                "recommendation": _first["recommendation"],
                "reason": _first["recommendation"].get("reason", ""),
                "occurrences": [{
                    "han_index": o["han_index"], "index": o["index"],
                    "sentence": o["sentence"]} for o in _occs],
            })

        if questions:
            return Result(status="questions",
                          questions=self._merge_group_questions(questions),
                          changes=changes, ssml_hints=ssml_hints,
                          audit=audit, semantic_questions=semantic_questions)

        # sentence pinyin (converted text, per sentence)
        sent_py: list[tuple[str, str]] = []
        for si, (a, b) in enumerate(sentences):
            conv = "".join(out[a:b]).strip()
            syl = [final_reading[i] or "—"
                   for i in range(n) if sent_of[i] == si]
            sent_py.append((conv, " ".join(syl)))
        pin_by_pos = {h["index"]: h["ssml"] for h in ssml_hints}
        ssml_output = "".join(
            pin_by_pos.get(pos, out[pos]) for pos in range(len(out)))
        return Result(status="ok", output="".join(out), changes=changes,
                      sentence_pinyin=sent_py, ssml_hints=ssml_hints,
                      ssml_output=ssml_output, audit=audit,
                      semantic_questions=semantic_questions)

    # ------------------------------------------------------------------
    @staticmethod
    def _merge_group_questions(questions: list[dict]) -> list[dict]:
        """Merge questions sharing (char, target_reading) into one group.

        The merged question keeps the first occurrence's id (so old
        answers files stay valid), carries ``group``/``group_key``/
        ``occurrences`` and notes the group size in its reason.
        Single-occurrence questions not marked as group questions pass
        through unchanged.
        """
        merged: list[dict] = []
        by_key: dict[tuple, dict] = {}
        for q in questions:
            tgt = q.get("target_reading")
            if tgt is None:
                merged.append(q)
                continue
            key = (q["char"], tgt)
            if key not in by_key:
                by_key[key] = q
                merged.append(q)
                continue
            first = by_key[key]
            if not first.get("group"):
                first["group"] = True
                first["group_key"] = f"{key[0]}|{key[1]}"
                first["occurrences"] = [{
                    "id": first["id"], "index": first["index"],
                    "sentence": first["sentence"], "prev": first["prev"],
                    "next": first["next"],
                }]
            occ = q.get("occurrences") or [{
                "id": q["id"], "index": q["index"],
                "sentence": q["sentence"], "prev": q["prev"],
                "next": q["next"],
            }]
            first["occurrences"].extend(occ)
            seen = {(c["char"], c["reading"]) for c in first["candidates"]}
            for c in q["candidates"]:
                if (c["char"], c["reading"]) not in seen:
                    first["candidates"].append(c)
                    seen.add((c["char"], c["reading"]))
        for q in merged:
            if q.get("group"):
                cnt = len(q.get("occurrences") or [])
                if cnt and "本组共" not in q["reason"]:
                    q["reason"] = f"{q['reason']}；本组共 {cnt} 处"
        return merged

    # ------------------------------------------------------------------
    def _prescribed_phrase_match(self, han_chars, han_canon,
                                 han_canon_str, prelim_target):
        """Match prescribed phrases over the Han sequence.

        A phrase applies only when no already-determined target (pinyin
        lock / phrase table) conflicts with the entry's reading for
        any of its characters.
        """
        d = self.d
        n = len(han_chars)
        result: dict[int, dict] = {}
        covered = [False] * n
        for i in range(n):
            if covered[i]:
                continue
            for e in d.prescribed_phrases:
                canon = e["_canon"]
                L = len(canon)
                if i + L > n or han_canon_str[i:i + L] != canon:
                    continue
                if any(covered[i + j] for j in range(L)):
                    continue
                readings = e["_readings"]
                if any(prelim_target[i + j] is not None
                       and prelim_target[i + j] != readings[j]
                       for j in range(L)):
                    continue
                for j in range(L):
                    result[i + j] = {
                        "substitute": e["substitute"][j],
                        "reading": readings[j],
                        "src_ref": e.get("src_ref") or e.get("src", ""),
                        "key": e["key"],
                    }
                    covered[i + j] = True
                break
        return result

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
