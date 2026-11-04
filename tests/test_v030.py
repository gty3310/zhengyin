"""v0.3.0 tests: prescribed substitutes, SSML pin, grouped questions,
and the narrowed common-reading exemption.

Uses a small self-built fixture db (including a ``prescribed`` table)
so the cases are independent of the real zhengyin-dict build.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from zhengyin.datalayer import Dict
from zhengyin.engine import Engine
from zhengyin.learnstore import LearnStore

SRC_DIR = Path(__file__).resolve().parent.parent / "src"

# char, readings, default, freq_rank, common_readings
CHARS = [
    ("好", ["hao3", "hao4"], "hao3", 55, ["hao3", "hao4"]),
    ("色", ["se4"], "se4", 500, ["se4"]),
    ("惡", ["e4", "wu4"], "e4", 1594, ["e4", "wu4"]),
    ("悟", ["wu4"], "wu4", 2251, ["wu4"]),
    ("緡", ["min2", "mian2"], "min2", None, []),
    ("绵", ["mian2"], "mian2", 3421, ["mian2"]),
    ("棉", ["mian2"], "mian2", 992, ["mian2"]),
    ("於", ["yu2", "wu1"], "yu2", 126, ["yu2"]),
    ("戲", ["xi4", "hu1"], "xi4", 1397, ["xi4"]),
    ("嗚", ["wu1"], "wu1", 3787, ["wu1"]),
    ("呼", ["hu1"], "hu1", 1014, ["hu1"]),
    ("屋", ["wu1"], "wu1", 800, ["wu1"]),
    ("忽", ["hu1"], "hu1", 900, ["hu1"]),
    ("謙", ["qian1", "qie4"], "qian1", 3113, ["qian1"]),
    ("窃", ["qie4"], "qie4", 2779, ["qie4"]),
    ("昊", ["hao4"], "hao4", 5000, ["hao4"]),
    ("興", ["xing1", "xing4"], "xing4", 836, ["xing1", "xing4"]),
    ("星", ["xing1"], "xing1", 700, ["xing1"]),
]

SUBSTITUTES = {
    "hao4": ["昊"],
    "wu4": ["悟"],
    "mian2": ["棉", "绵"],  # best_substitute would pick 棉 (freq 992)
    "wu1": ["屋"],
    "hu1": ["忽"],
    "qie4": ["窃"],
}


def build_fixture_db(path: Path) -> Path:
    con = sqlite3.connect(path)
    try:
        con.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE chars (char TEXT, readings TEXT, default_reading TEXT,
            freq_rank INTEGER, in_8105 INTEGER, src TEXT, src_ref TEXT,
            common_readings TEXT);
        CREATE TABLE tongjia (id INTEGER PRIMARY KEY, char TEXT, benzi TEXT,
            reading TEXT, sense TEXT, example TEXT, char_simp TEXT,
            benzi_simp TEXT, src TEXT, src_ref TEXT);
        CREATE TABLE podu (char TEXT, context TEXT, reading TEXT, sense TEXT,
            basis TEXT, confidence TEXT, src TEXT, gen TEXT, verified TEXT);
        CREATE TABLE phrases (phrase TEXT, pinyin TEXT, src TEXT);
        CREATE TABLE heteronyms (word TEXT, readings TEXT, note TEXT,
            src TEXT, src_ref TEXT);
        CREATE TABLE substitutes (reading TEXT PRIMARY KEY,
            candidates TEXT, src TEXT);
        CREATE TABLE prescribed (key TEXT, reading TEXT, substitute TEXT,
            note TEXT, src TEXT, src_ref TEXT, verified TEXT);
        """)
        con.execute("INSERT INTO meta VALUES ('version', 'fixture-0.3')")
        for ch, readings, default, rank, common in CHARS:
            con.execute(
                "INSERT INTO chars VALUES (?,?,?,?,?,?,?,?)",
                (ch, json.dumps(readings), default, rank, 1, "fixture",
                 "fixture", json.dumps(common)))
        for reading, cands in SUBSTITUTES.items():
            con.execute("INSERT INTO substitutes VALUES (?,?,?)",
                        (reading, json.dumps(cands, ensure_ascii=False),
                         "fixture"))
        # Unverified podu for 好 hao4 (mirrors the real db seed).
        con.execute(
            "INSERT INTO podu VALUES (?,?,?,?,?,?,?,?,?)",
            ("好", "《论语·雍也》「知之者不如好之者」", "hao4", "爱好",
             "fixture", "high", "fixture", "", "unverified"))
        con.execute(
            "INSERT INTO prescribed VALUES (?,?,?,?,?,?,?)",
            ("緡", "mian2", "绵", "", "fixture", "fixture-prescribed",
             "user_confirmed"))
        con.execute(
            "INSERT INTO prescribed VALUES (?,?,?,?,?,?,?)",
            ("於戲", "wu1 hu1", "嗚呼", "", "fixture", "fixture-prescribed",
             "user_confirmed"))
        con.commit()
    finally:
        con.close()
    return path


class V030(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = build_fixture_db(Path(self.tmp.name) / "fixture.db")
        self.learned = LearnStore(Path(self.tmp.name) / "learned.jsonl")
        self.engine = Engine(Dict(self.db_path), self.learned)

    def tearDown(self):
        self.tmp.cleanup()

    # 1 ------------------------------------------------------------------
    def test_1_grouped_question_generation(self):
        r = self.engine.convert("好好色", pinyin="hao4 hao4 se4".split())
        self.assertEqual(r.status, "questions")
        self.assertEqual(len(r.questions), 1, r.questions)
        q = r.questions[0]
        self.assertEqual(q["char"], "好")
        self.assertTrue(q.get("group"), q)
        self.assertEqual(q.get("group_key"), "好|hao4")
        self.assertEqual(len(q["occurrences"]), 2, q)

    # 2 ------------------------------------------------------------------
    def test_2_group_answer_ssml(self):
        r1 = self.engine.convert("好好色", pinyin="hao4 hao4 se4".split())
        qid = r1.questions[0]["id"]
        r2 = self.engine.convert("好好色", pinyin="hao4 hao4 se4".split(),
                                 answers={qid: "ssml"})
        self.assertEqual(r2.status, "ok", r2.questions)
        self.assertEqual(r2.output, "好好色")
        self.assertEqual(len(r2.ssml_hints), 2, r2.ssml_hints)
        for h in r2.ssml_hints:
            self.assertEqual(h["char"], "好")
            self.assertEqual(h["reading"], "hao4")
            self.assertEqual(
                h["ssml"], '<phoneme alphabet="py" ph="hao4">好</phoneme>')
        pins = [c for c in r2.changes if c["via"] == "pin"]
        self.assertEqual(len(pins), 2, r2.changes)
        self.assertEqual(r2.ssml_output.count("<phoneme"), 2)
        # rerun: group ruling is learned, no questions again
        r3 = self.engine.convert("好好色", pinyin="hao4 hao4 se4".split())
        self.assertEqual(r3.status, "ok", r3.questions)
        self.assertEqual(r3.output, "好好色")
        self.assertEqual(len(r3.ssml_hints), 2, r3.ssml_hints)

    # 3 ------------------------------------------------------------------
    def test_3_group_answer_substitute(self):
        r1 = self.engine.convert("惡惡", pinyin="wu4 wu4".split())
        self.assertEqual(r1.status, "questions")
        self.assertEqual(len(r1.questions), 1, r1.questions)
        qid = r1.questions[0]["id"]
        r2 = self.engine.convert("惡惡", pinyin="wu4 wu4".split(),
                                 answers={qid: "悟"})
        self.assertEqual(r2.status, "ok", r2.questions)
        self.assertEqual(r2.output, "悟悟")
        groups = [e for e in self.learned.entries if e.get("scope") == "group"]
        self.assertEqual(len(groups), 1, self.learned.entries)
        g = groups[0]
        self.assertEqual(g["char"], "惡")
        self.assertEqual(g["reading"], "wu4")
        self.assertEqual(g["action"], "substitute")
        self.assertEqual(g["substitute"], "悟")
        self.assertEqual(g["verified"], "user_confirmed")
        # group records are not mixed into the tongjia-format export
        self.assertEqual(self.learned.export_dict_format(), [])

    # 4 ------------------------------------------------------------------
    def test_4_group_answer_keep_by_occurrence_id(self):
        r1 = self.engine.convert("好好色", pinyin="hao4 hao4 se4".split())
        occ_id = r1.questions[0]["occurrences"][1]["id"]
        r2 = self.engine.convert("好好色", pinyin="hao4 hao4 se4".split(),
                                 answers={occ_id: "keep"})
        self.assertEqual(r2.status, "ok", r2.questions)
        self.assertEqual(r2.output, "好好色")
        self.assertFalse([c for c in r2.changes if c["char"] == "好"],
                         r2.changes)
        self.assertEqual(r2.ssml_hints, [])
        r3 = self.engine.convert("好好色", pinyin="hao4 hao4 se4".split())
        self.assertEqual(r3.status, "ok", r3.questions)
        self.assertEqual(r3.output, "好好色")

    # 5 ------------------------------------------------------------------
    def test_5_prescribed_priority(self):
        r = self.engine.convert("緡", pinyin=["mian2"])
        self.assertEqual(r.status, "ok", r.questions)
        self.assertEqual(r.output, "绵")
        rec = [c for c in r.changes if c["char"] == "緡"]
        self.assertTrue(rec and rec[0]["via"] == "prescribed", r.changes)
        self.assertEqual(rec[0]["ref"], "fixture-prescribed")

        r2 = self.engine.convert("於戲", pinyin="wu1 hu1".split())
        self.assertEqual(r2.status, "ok", r2.questions)
        self.assertEqual(r2.output, "嗚呼")
        self.assertEqual(len(r2.changes), 2, r2.changes)
        for c in r2.changes:
            self.assertEqual(c["via"], "prescribed", r2.changes)

    # 6 ------------------------------------------------------------------
    def test_6_pin_via_rulings_cli(self):
        work = Path(self.tmp.name)
        home = work / "home"
        home.mkdir()
        (work / "qian.txt").write_text("謙", encoding="utf-8")
        (work / "qian.pinyin.txt").write_text("qie4", encoding="utf-8")
        (work / "rulings.json").write_text(json.dumps(
            {"rulings": [{"char": "謙", "reading": "qie4",
                          "action": "ssml"}]}, ensure_ascii=False),
            encoding="utf-8")
        env = dict(os.environ)
        env["HOME"] = str(home)
        env["PYTHONPATH"] = str(SRC_DIR) + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-m", "zhengyin.cli", "convert",
             str(work / "qian.txt"), "-o", str(work / "out.txt"),
             "--pinyin", str(work / "qian.pinyin.txt"),
             "--rulings", str(work / "rulings.json"),
             "--ssml", str(work / "out.ssml.txt"),
             "--ssml-hints", str(work / "hints.json"),
             "--db", str(self.db_path)],
            capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual((work / "out.txt").read_text(encoding="utf-8"), "謙")
        self.assertEqual(
            (work / "out.ssml.txt").read_text(encoding="utf-8"),
            '<phoneme alphabet="py" ph="qie4">謙</phoneme>')
        hints = json.loads((work / "hints.json").read_text(encoding="utf-8"))
        self.assertEqual(len(hints), 1, hints)
        self.assertEqual(hints[0]["reading"], "qie4")
        learned_text = (home / ".zhengyin" / "learned.jsonl").read_text(
            encoding="utf-8")
        self.assertIn('"user-ruling"', learned_text)
        self.assertIn('"scope": "group"', learned_text)

    # 7 ------------------------------------------------------------------
    def test_7_no_pinyin_no_evidence_kept(self):
        r = self.engine.convert("興")
        self.assertEqual(r.status, "ok", r.questions)
        self.assertEqual(r.output, "興")
        self.assertEqual(r.questions, [])


if __name__ == "__main__":
    unittest.main()
