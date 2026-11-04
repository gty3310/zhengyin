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
from .datalayer import Dict
from .engine import Engine
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
    learned = LearnStore()
    engine = Engine(d, learned)
    try:
        result = engine.convert(text, pinyin=pinyin, answers=answers)
    except ValueError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1

    if result.status == "questions":
        qpath = Path(args.questions or "questions.json")
        qpath.write_text(json.dumps(result.questions, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        print(json.dumps({
            "status": "questions",
            "questions_file": str(qpath),
            "question_count": len(result.questions),
            "hint": "请用户逐条作答（每条一个现代汉字），写入 answers.json "
                    "（{\"q0\": \"字\", ...}）后以 --answers 重跑本命令。",
        }, ensure_ascii=False))
        return 2

    out_path = Path(args.output)
    out_path.write_text(result.output, encoding="utf-8")
    summary = {"status": "ok", "output": str(out_path),
               "changes": len(result.changes)}
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
    print(json.dumps(summary, ensure_ascii=False))
    return 0


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
    p_conv.add_argument("--questions", help="问题清单 JSON 输出路径（默认 ./questions.json）")
    p_conv.add_argument("--answers", help="用户答案 JSON（{\"q0\": \"字\"}）")
    p_conv.add_argument("--db", help="直接指定 zhengyin.db 路径")
    p_conv.set_defaults(func=cmd_convert)

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
