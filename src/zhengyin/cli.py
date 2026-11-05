"""zhengyin command-line interface.

Exit codes: 0 = success (conversion complete / command done);
2 = conversion paused, questions written for a human to answer;
1 = error (bad input, missing data, failed validation of the pinyin
file, network failure on fetch, ...).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

from . import __version__
from .audit import audit_text
from .datalayer import Dict
from .engine import Engine, is_han
from .learnstore import LearnStore

DICT_REPO = "gty3310/zhengyin-dict"
DATA_DIR = Path.home() / ".zhengyin" / "data"


def resolve_db(cli_db: str | None) -> Path | None:
    if cli_db:
        return Path(cli_db)
    env = os.environ.get("ZHENGYIN_DB")
    if env:
        return Path(env)
    p = DATA_DIR / "zhengyin.db"
    return p if p.exists() else None


def load_dict(cli_db: str | None) -> Dict:
    path = resolve_db(cli_db)
    if path is None or not Path(path).exists():
        raise SystemExit(
            "未找到字典数据。先运行 `zhengyin data fetch` 下载，"
            "或用 --db 指定 zhengyin.db 路径。")
    return Dict(path)


# ----------------------------------------------------------------------
# data subcommands
# ----------------------------------------------------------------------
def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "zhengyin-cli"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def cmd_data_fetch(args) -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    manifest_url = (f"https://github.com/{DICT_REPO}/releases/"
                    "latest/download/manifest.json")
    try:
        manifest = json.loads(_download(manifest_url).decode("utf-8"))
    except Exception as e:  # noqa: BLE001 - report any network failure plainly
        print(f"下载 manifest 失败：{e}", file=sys.stderr)
        return 1
    version = manifest.get("version", "")
    db_url = (f"https://github.com/{DICT_REPO}/releases/download/"
              f"v{version}/zhengyin.db")
    try:
        blob = _download(db_url)
    except Exception as e:  # noqa: BLE001
        print(f"下载 zhengyin.db 失败：{e}", file=sys.stderr)
        return 1
    digest = hashlib.sha256(blob).hexdigest()
    expected = manifest.get("db_sha256", "")
    if expected and digest != expected:
        print(f"sha256 校验失败：期望 {expected}，实际 {digest}", file=sys.stderr)
        return 1
    (DATA_DIR / "zhengyin.db").write_bytes(blob)
    (DATA_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"字典数据 v{version} 已就绪：{DATA_DIR / 'zhengyin.db'}"
          f"（sha256 校验通过，{len(blob)} 字节）")
    return 0


def cmd_data_status(args) -> int:
    manifest_path = DATA_DIR / "manifest.json"
    db_path = DATA_DIR / "zhengyin.db"
    if not db_path.exists():
        print("尚未下载字典数据。运行 `zhengyin data fetch`。")
        return 0
    if manifest_path.exists():
        m = json.loads(manifest_path.read_text(encoding="utf-8"))
        print(f"数据版本：{m.get('version')}（构建于 {m.get('built_at')}）")
        print(f"条数：{json.dumps(m.get('counts', {}), ensure_ascii=False)}")
    d = Dict(db_path)
    print(f"数据库：{db_path}（meta version={d.meta.get('version')}）")
    learned = LearnStore()
    print(f"本地学习层：{len(learned.entries)} 条（{learned.path}）")
    return 0


# ----------------------------------------------------------------------
# convert
# ----------------------------------------------------------------------
def load_rulings(path, learned: LearnStore, engine: Engine) -> None:
    """Load --rulings JSON into the learned layer as group rulings.

    Format: {"rulings": [{"char", "reading", "action",
    "substitute"?}]} with action in substitute|ssml|keep. Substitute
    rulings are validated like user answers before anything is stored;
    a failed validation raises ValueError (caller exits 1).
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rulings = data.get("rulings") if isinstance(data, dict) else data
    if not isinstance(rulings, list):
        raise ValueError("rulings 文件格式应为 {\"rulings\": [...]}")
    # Two passes: validate everything first, then write group records.
    parsed = []
    for r in rulings:
        char = r.get("char", "")
        reading = r.get("reading", "")
        action = r.get("action", "")
        if len(char) != 1 or not reading:
            raise ValueError(f"rulings 条目缺单字 char 或 reading：{r}")
        if action not in ("substitute", "ssml", "keep"):
            raise ValueError(
                f"rulings 条目 action 须为 substitute/ssml/keep：{r}")
        substitute = r.get("substitute")
        if action == "substitute":
            if not substitute:
                raise ValueError(f"rulings 替字条目缺 substitute：{r}")
            ok, _eff, fail = engine._validate_answer(substitute, reading)
            if not ok:
                raise ValueError(
                    f"rulings 替字未通过校验（{char} {reading} → "
                    f"{substitute}）：{fail}")
        parsed.append((char, reading, action, substitute))
    for char, reading, action, substitute in parsed:
        learned.add_group(char, reading, action, substitute,
                          src="user-ruling")


def load_substitute_review(path) -> dict:
    """Load --substitute-review JSON into the engine's review map.

    Format: {"reviews": [{"id", "decision": "approve"|"reject",
    "sense_fit": "一句理由"}]}. An approval must carry a non-empty
    sense_fit (the reviewer states why the substitute fits this
    sentence); anything malformed raises ValueError (caller exits 1).
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    reviews = data.get("reviews") if isinstance(data, dict) else data
    if not isinstance(reviews, list):
        raise ValueError("substitute-review 文件格式应为 {\"reviews\": [...]}")
    out: dict = {}
    for r in reviews:
        rid = r.get("id", "")
        decision = r.get("decision", "")
        if not rid:
            raise ValueError(f"substitute-review 条目缺 id：{r}")
        if decision not in ("approve", "reject"):
            raise ValueError(
                f"substitute-review 条目 decision 须为 approve/reject：{r}")
        sense_fit = (r.get("sense_fit") or "").strip()
        if decision == "approve" and not sense_fit:
            raise ValueError(
                f"substitute-review 批准条目须带非空 sense_fit（审者须"
                f"写明替字为何合此句语境）：{r}")
        out[rid] = {"decision": decision, "sense_fit": sense_fit}
    return out


def cmd_convert(args) -> int:
    d = load_dict(args.db)
    text = Path(args.input).read_text(encoding="utf-8")
    pinyin = None
    if args.pinyin:
        raw = Path(args.pinyin).read_text(encoding="utf-8")
        pinyin = raw.split()
    answers = None
    if args.answers:
        answers = json.loads(Path(args.answers).read_text(encoding="utf-8"))
    substitute_review = None
    if getattr(args, "substitute_review", None):
        try:
            substitute_review = load_substitute_review(args.substitute_review)
        except (ValueError, OSError, json.JSONDecodeError) as e:
            print(f"错误：{e}", file=sys.stderr)
            return 1
    learned = LearnStore()
    engine = Engine(d, learned)
    if getattr(args, "rulings", None):
        try:
            load_rulings(args.rulings, learned, engine)
        except (ValueError, OSError, json.JSONDecodeError) as e:
            print(f"错误：{e}", file=sys.stderr)
            return 1
    try:
        result = engine.convert(text, pinyin=pinyin, answers=answers,
                                substitute_review=substitute_review)
    except ValueError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1

    audit_path = Path(args.audit) if getattr(args, "audit", None) else (
        Path(args.output).with_name("audit.json"))
    if result.audit:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(json.dumps(result.audit, ensure_ascii=False,
                                         indent=2), encoding="utf-8")
    sem_path = None
    if result.semantic_questions:
        sem_path = Path(args.semantic_questions) if getattr(
            args, "semantic_questions", None) else (
            Path(args.output).with_name("semantic_questions.json"))
        sem_path.parent.mkdir(parents=True, exist_ok=True)
        sem_path.write_text(json.dumps(result.semantic_questions,
                                       ensure_ascii=False, indent=2),
                            encoding="utf-8")

    prop_path = None
    if result.proposals:
        prop_path = Path(args.substitute_proposals) if getattr(
            args, "substitute_proposals", None) else (
            Path(args.output).with_name("substitute_proposals.json"))
        prop_path.parent.mkdir(parents=True, exist_ok=True)
        prop_path.write_text(json.dumps(result.proposals,
                                        ensure_ascii=False, indent=2),
                             encoding="utf-8")

    if result.status == "proposals":
        print(json.dumps({
            "status": "proposals",
            "proposals_file": str(prop_path),
            "proposal_count": len(result.proposals),
            "audit_file": str(audit_path) if result.audit else None,
            "audit_counts": result.audit.get("counts") if result.audit else None,
            "semantic_questions_file": str(sem_path) if sem_path else None,
            "hint": "替字提案待语境审（v0.4.1）：请执行 agent 逐条核对提案"
                    "替字的大意是否合该句语境，写 review.json"
                    "（{\"reviews\": [{\"id\", \"decision\": "
                    "\"approve\"|\"reject\", \"sense_fit\": \"一句理由\"}]}，"
                    "approve 必须带非空 sense_fit），后以 "
                    "--substitute-review 重跑；approve 落字（changes 带 "
                    "sense_fit 留痕），reject 改钉音保原字。不出成品。",
        }, ensure_ascii=False))
        return 2

    if result.status == "questions":
        qpath = Path(args.questions or "sound_questions.json")
        qpath.write_text(json.dumps(result.questions, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        print(json.dumps({
            "status": "questions",
            "questions_file": str(qpath),
            "sound_questions_file": str(qpath),
            "question_count": len(result.questions),
            "proposals_file": str(prop_path) if prop_path else None,
            "proposal_count": len(result.proposals),
            "audit_file": str(audit_path) if result.audit else None,
            "audit_counts": result.audit.get("counts") if result.audit else None,
            "semantic_questions_file": str(sem_path) if sem_path else None,
            "hint": "sound_questions 只含声音层残余问题（无目标音可定/未知字"
                    "无锁定读音/映射冲突）：请执行 agent 逐条作答（替字或 "
                    "\"ssml\"），写入 answers.json（{\"q0\": \"字\", ...}）"
                    "后以 --answers 重跑；声音层没有 keep。语义待确认在 "
                    "semantic_questions.json，不挡退出码。"
                    "若同时有替字提案（proposals_file），其答案落字前还须"
                    "过语境审（--substitute-review）。",
        }, ensure_ascii=False))
        return 2

    out_path = Path(args.output)
    out_path.write_text(result.output, encoding="utf-8")
    summary = {"status": "ok", "output": str(out_path),
               "changes": len(result.changes),
               "audit_file": str(audit_path) if result.audit else None,
               "audit_counts": result.audit.get("counts") if result.audit else None,
               "substitute_reviews": (result.audit.get("substitute_reviews")
                                      if result.audit else None),
               "semantic_questions_file": str(sem_path) if sem_path else None,
               "note": "锁定版以 SSML 版为准：纯文本版里被钉音的字无锁"
                       "（尤其音调型多音字），残余风险在 TTS 引擎，合成请用 SSML 版。"}
    if args.changes:
        cpath = Path(args.changes)
        cpath.write_text(json.dumps({
            "dict_version": d.meta.get("version"),
            "input": args.input,
            "changes": result.changes,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        summary["changes_file"] = str(cpath)
    if args.with_pinyin:
        ppath = Path(args.pinyin_out) if args.pinyin_out else Path(
            str(out_path) + ".pinyin.txt")
        lines = []
        for sent, py in result.sentence_pinyin:
            lines.append(sent)
            lines.append(py)
        ppath.write_text("\n".join(lines) + "\n", encoding="utf-8")
        summary["pinyin_file"] = str(ppath)
    # v0.4.0: SSML 版与钉音清单默认随成品一并产出（锁定版以 SSML 为准）。
    spath = Path(args.ssml) if getattr(args, "ssml", None) else Path(
        str(out_path) + ".ssml.txt")
    spath.write_text(result.ssml_output, encoding="utf-8")
    summary["ssml_file"] = str(spath)
    summary["ssml_hints"] = len(result.ssml_hints)
    hpath = Path(args.ssml_hints) if getattr(args, "ssml_hints", None) else (
        out_path.with_name("ssml_hints.json"))
    hpath.write_text(json.dumps(result.ssml_hints,
                                ensure_ascii=False, indent=2),
                     encoding="utf-8")
    summary["ssml_hints_file"] = str(hpath)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


# ----------------------------------------------------------------------
# audit (standalone re-check of an existing product)
# ----------------------------------------------------------------------
def cmd_audit(args) -> int:
    d = load_dict(args.db)
    text = Path(args.input).read_text(encoding="utf-8")
    raw = Path(args.pinyin).read_text(encoding="utf-8")
    syllables = raw.split()
    pinned_han: set = set()
    if getattr(args, "ssml_hints", None):
        hints = json.loads(Path(args.ssml_hints).read_text(encoding="utf-8"))
        han_index_of = {}
        _k = 0
        for _pos, _c in enumerate(text):
            if is_han(_c):
                han_index_of[_pos] = _k
                _k += 1
        for h in hints:
            if "han_index" in h:
                pinned_han.add(h["han_index"])
            elif "index" in h and h["index"] in han_index_of:
                pinned_han.add(han_index_of[h["index"]])
    try:
        audit = audit_text(d, text, syllables, pinned_han=pinned_han)
    except ValueError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1
    apath = Path(args.audit) if getattr(args, "audit", None) else Path("audit.json")
    apath.write_text(json.dumps(audit, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    unlocked = [det for det in audit["details"] if det["blocking"]]
    print(json.dumps({
        "status": "ok" if audit["passed"] else "blocked",
        "audit_file": str(apath),
        "counts": audit["counts"],
        "unlocked_count": len(unlocked),
        "hint": "" if audit["passed"] else
                "终检未全过：未锁定明细见 audit.json 中 blocking 的逐字记录"
                "（B＝多音字默认音未钉音，D＝不合格）。",
    }, ensure_ascii=False))
    return 0 if audit["passed"] else 2


# ----------------------------------------------------------------------
# lookup / learn
# ----------------------------------------------------------------------
def cmd_lookup(args) -> int:
    d = load_dict(args.db)
    ch = args.char
    if len(ch) != 1:
        print("lookup 只接受单个汉字", file=sys.stderr)
        return 1
    info = d.char_info(ch)
    forms = d.variant_forms(ch)
    print(f"{ch}" + (f"（同字异形：{'、'.join(forms)}）" if len(forms) > 1 else ""))
    if info:
        rds = info["readings"]
        print("读音：" + "、".join(
            f"{r}（默认）" if r == info["default"] else r for r in rds))
        print(f"默认读音：{info['default']}"
              f"（频次序 {info.get('freq_rank')}，规范字表 {'收录' if info.get('in_8105') else '未收录'}）")
    else:
        print("字表无此字。")
    tj = d.tongjia_entries(ch)
    if tj:
        print(f"\n通假条目（{len(tj)} 条，键＝字＋读音＋义项）：")
        for e in tj:
            sense = e.get("sense") or "（义项待补）"
            ref = e.get("src_ref") or e.get("src", "")
            print(f"  {e['reading']} → {e['benzi']}：{sense}"
                  + (f"［{ref}］" if ref else ""))
    pd = d.podu_entries(ch)
    if pd:
        print(f"\n破读条目（{len(pd)} 条）：")
        for e in pd:
            ver = e.get("verified") or "unverified"
            print(f"  {e['reading']}：{e.get('sense', '')}"
                  f"［{ver}］{e.get('context', '')}")
    if not tj and not pd:
        print("\n无通假/破读条目。")
    return 0


def cmd_learn_export(args) -> int:
    learned = LearnStore()
    records = learned.export_dict_format()
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"已导出 {len(records)} 条到 {args.output}")
    else:
        sys.stdout.write(text)
    return 0


# ----------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zhengyin",
        description="古文正音：把古文转换成 TTS 可正确朗读的文本（给 AI Agent 用）")
    parser.add_argument("--version", action="version",
                        version=f"zhengyin {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_data = sub.add_parser("data", help="字典数据管理")
    data_sub = p_data.add_subparsers(dest="data_command", required=True)
    data_sub.add_parser("fetch", help="从 zhengyin-dict Release 下载字典数据库"
                          ).set_defaults(func=cmd_data_fetch)
    data_sub.add_parser("status", help="显示本地字典数据版本"
                        ).set_defaults(func=cmd_data_status)

    p_conv = sub.add_parser("convert", help="转换一篇古文")
    p_conv.add_argument("input", help="古文 TXT 路径")
    p_conv.add_argument("-o", "--output", required=True, help="输出 TXT 路径")
    p_conv.add_argument("--pinyin", help="拼音文件（每字一个音节，空白分隔）")
    p_conv.add_argument("--with-pinyin", action="store_true",
                        help="另输出逐句拼音对照 TXT")
    p_conv.add_argument("--pinyin-out", help="拼音对照 TXT 路径")
    p_conv.add_argument("--changes", help="逐字转换清单 JSON 输出路径")
    p_conv.add_argument("--questions", help="声音残余问题 JSON 输出路径（默认 ./sound_questions.json）")
    p_conv.add_argument("--audit", help="终检报告 audit.json 输出路径（默认写到输出 TXT 同目录的 audit.json；停在残余问题环节时也写）")
    p_conv.add_argument("--semantic-questions", help="语义待确认清单 JSON 输出路径（默认写到输出 TXT 同目录的 semantic_questions.json；无待确认组时不写）")
    p_conv.add_argument("--answers", help="用户答案 JSON（{\"q0\": \"字\"}；组问题可答替字、\"ssml\" 或 \"keep\"）")
    p_conv.add_argument("--substitute-proposals", help="替字提案 JSON 输出路径（默认写到输出 TXT 同目录的 substitute_proposals.json；有未审提案时退出码 2、不出成品）")
    p_conv.add_argument("--substitute-review", help="替字语境审结果 JSON（{\"reviews\": [{\"id\", \"decision\": \"approve\"|\"reject\", \"sense_fit\": \"一句理由\"}]}；approve 须带非空 sense_fit）")
    p_conv.add_argument("--rulings", help="组裁决 JSON（{\"rulings\": [{\"char\", \"reading\", \"action\", \"substitute\"?}]}），启动时写入学习层组记录并本轮生效")
    p_conv.add_argument("--ssml", help="SSML 成品 TXT 输出路径（默认 <输出>.ssml.txt；同行结构，仅 pin 处包 phoneme 标签；有残余问题时不出）")
    p_conv.add_argument("--ssml-hints", help="SSML 钉音清单 JSON 输出路径（默认输出同目录 ssml_hints.json；有残余问题时不出）")
    p_conv.add_argument("--db", help="直接指定 zhengyin.db 路径")
    p_conv.set_defaults(func=cmd_convert)

    p_audit = sub.add_parser("audit", help="对既有成品单独终检复检（输出 audit.json；全字锁定退出码 0，有未锁定项 2）")
    p_audit.add_argument("input", help="成品 TXT 路径")
    p_audit.add_argument("--pinyin", required=True, help="拼音文件（每字一个音节，与成品汉字逐字对齐）")
    p_audit.add_argument("--ssml-hints", help="可选：钉音清单 JSON（带 han_index 或 index），命中位置记 C 类")
    p_audit.add_argument("--audit", help="audit.json 输出路径（默认 ./audit.json）")
    p_audit.add_argument("--db", help="直接指定 zhengyin.db 路径")
    p_audit.set_defaults(func=cmd_audit)

    p_lookup = sub.add_parser("lookup", help="查一个字的读音/通假/破读")
    p_lookup.add_argument("char")
    p_lookup.add_argument("--db", help="直接指定 zhengyin.db 路径")
    p_lookup.set_defaults(func=cmd_lookup)

    p_learn = sub.add_parser("learn", help="学习层管理")
    learn_sub = p_learn.add_subparsers(dest="learn_command", required=True)
    p_export = learn_sub.add_parser(
        "export", help="把学习层导出为字典仓格式 JSONL（供提 PR 回流）")
    p_export.add_argument("-o", "--output", help="输出路径（默认标准输出）")
    p_export.set_defaults(func=cmd_learn_export)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
