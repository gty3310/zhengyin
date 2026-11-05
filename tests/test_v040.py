"""v0.4.0 tests: final audit （终检）, sound closure (auto-pin /
validated semantic substitute), semantic_questions, audit subcommand.

Uses a self-built fixture db WITH the dict v0.3.1 modern columns
(modern_readings / modern_src / is_polyphonic / unique_reading), so
the cases are independent of the real zhengyin-dict build.
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

# char, readings, default, modern_readings, is_poly, unique, common
CHARS = [
    ("行", ["xing2", "hang2"], "xing2", ["xing2", "hang2"], 1, None, ["xing2", "hang2"]),
    ("國", ["guo2"], "guo2", ["guo2"], 0, "guo2", ["guo2"]),
    ("惡", ["e4", "wu4"], "e4", ["e4", "wu4"], 1, None, ["e4", "wu4"]),
    ("悟", ["wu4"], "wu4", ["wu4"], 0, "wu4", ["wu4"]),
    ("為", ["wei2", "wei4"], "wei4", ["wei4", "wei2"], 1, None, ["wei2", "wei4"]),
    ("倍", ["bei4", "pei2"], "pei2", ["pei2", "bei4"], 1, None, []),
    ("背", ["bei4", "bei1"], "bei4", ["bei4", "bei1"], 1, None, ["bei4"]),
    ("節", ["jie2", "jie1"], "jie2", ["jie2", "jie1"], 1, None, ["jie2"]),
    ("截", ["jie2"], "jie2", ["jie2"], 0, "jie2", ["jie2"]),
    ("彼", ["bi3"], "bi3", ["bi3"], 0, "bi3", ["bi3"]),
    ("南", ["nan2"], "nan2", ["nan2"], 0, "nan2", ["nan2"]),
    ("山", ["shan1"], "shan1", ["shan1"], 0, "shan1", ["shan1"]),
    ("親", ["qin1"], "qin1", ["qin1"], 0, "qin1", ["qin1"]),
    ("新", ["xin1"], "xin1", ["xin1"], 0, "xin1", ["xin1"]),
    ("好", ["hao3", "hao4"], "hao3", ["hao3", "hao4"], 1, None, ["hao3", "hao4"]),
    ("色", ["se4"], "se4", ["se4"], 0, "se4", ["se4"]),
    ("謙", ["qian1", "qie4"], "qian1", ["qian1"], 0, "qian1", ["qian1"]),
    ("球", ["qiu2"], "qiu2", ["qiu2"], 0, "qiu2", ["qiu2"]),
    ("子", ["zi3", "zi5"], "zi3", ["zi3"], 0, "zi3", ["zi3"]),
]

SUBSTITUTES = {"xin1": ["新"], "wu4": ["悟"]}


def build_fixture_db(path: Path) -> Path:
    con = sqlite3.connect(path)
    try:
        con.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE chars (char TEXT, readings TEXT, default_reading TEXT,
            freq_rank INTEGER, in_8105 INTEGER, src TEXT, src_ref TEXT,
            common_readings TEXT, modern_readings TEXT, modern_src TEXT,
            is_polyphonic INTEGER, unique_reading TEXT);
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
        con.execute("INSERT INTO meta VALUES ('version', 'fixture-0.4')")
        for ch, readings, default, modern, poly, unique, common in CHARS:
            con.execute(
                "INSERT INTO chars VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (ch, json.dumps(readings), default, 100, 1, "fixture",
                 "fixture", json.dumps(common), json.dumps(modern),
                 "fixture-xhc", poly, unique))
        for reading, cands in SUBSTITUTES.items():
            con.execute("INSERT INTO substitutes VALUES (?,?,?)",
                        (reading, json.dumps(cands, ensure_ascii=False),
                         "fixture"))
        # 倍 bei4 通 背 (semantic mapping whose benzi is polyphonic).
        con.execute(
            "INSERT INTO tongjia VALUES (?,?,?,?,?,?,?,?,?,?)",
            (1, "倍", "背", "bei4", "背負", "", "", "", "fixture", "fixture"))
        # 節 jie2 通 截: single isolated entry, example == test sentence.
        con.execute(
            "INSERT INTO tongjia VALUES (?,?,?,?,?,?,?,?,?,?)",
            (2, "節", "截", "jie2", "然也", "節彼南山", "", "",
             "fixture", "fixture"))
        con.commit()
    finally:
        con.close()
    return path


class V040(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = build_fixture_db(Path(self.tmp.name) / "fixture.db")
        self.learned = LearnStore(Path(self.tmp.name) / "learned.jsonl")
        self.engine = Engine(Dict(self.db_path), self.learned)

    def tearDown(self):
        self.tmp.cleanup()

    # 1 ------------------------------------------------------------------
    def test_01_A_guo_final_state(self):
        r = self.engine.convert("國", pinyin=["guo2"])
        self.assertEqual(r.status, "ok", r.questions)
        self.assertEqual(r.output, "國")
        self.assertEqual(r.ssml_hints, [])
        self.assertEqual(r.audit["counts"]["A"], 1, r.audit)
        self.assertEqual(r.audit["detected_counts"]["A"], 1, r.audit)
        self.assertTrue(r.audit["passed"], r.audit)

    # 2 ------------------------------------------------------------------
    def test_02_E_wu4_syllable_differs_auto_pin(self):
        r = self.engine.convert("惡", pinyin=["wu4"])
        self.assertEqual(r.status, "ok", r.questions)
        self.assertEqual(r.questions, [])
        self.assertEqual(r.output, "惡")
        self.assertEqual(len(r.ssml_hints), 1, r.ssml_hints)
        self.assertEqual(r.ssml_hints[0]["reading"], "wu4")
        self.assertEqual(r.audit["counts"]["C"], 1, r.audit)
        self.assertTrue(r.audit["passed"], r.audit)

    # 3 ------------------------------------------------------------------
    def test_03_Wei_wei2_tone_only_auto_pin(self):
        r = self.engine.convert("為", pinyin=["wei2"])
        self.assertEqual(r.status, "ok", r.questions)
        self.assertEqual(r.questions, [])
        self.assertEqual(len(r.ssml_hints), 1, r.ssml_hints)
        self.assertEqual(r.ssml_hints[0]["reading"], "wei2")
        self.assertEqual(r.audit["counts"]["C"], 1, r.audit)

    # 4 ------------------------------------------------------------------
    def test_04_Xing_xing2_B_detected_then_pin_resolved(self):
        r = self.engine.convert("行", pinyin=["xing2"])
        self.assertEqual(r.status, "ok", r.questions)
        self.assertEqual(r.questions, [])
        self.assertEqual(r.semantic_questions, [])
        self.assertEqual(r.audit["detected_counts"]["B"], 1, r.audit)
        self.assertEqual(r.audit["counts"]["C"], 1, r.audit)
        self.assertEqual(len(r.audit["resolutions"]), 1, r.audit)
        self.assertEqual(r.audit["resolutions"][0]["rule"],
                         "rule1_default_pin")
        self.assertTrue(r.audit["passed"], r.audit)

    # 5 ------------------------------------------------------------------
    def test_05_Tongjia_poly_benzi_rejected_to_pin(self):
        # 倍 bei4 -> 背 is a semantic mapping, but 背 is polyphonic:
        # the substitute fails the strict validation, is rejected with
        # a trail entry, and 倍 itself is pinned instead.
        r = self.engine.convert("倍", pinyin=["bei4"])
        self.assertEqual(r.status, "ok", r.questions)
        self.assertEqual(r.output, "倍")
        self.assertEqual(len(r.ssml_hints), 1, r.ssml_hints)
        self.assertEqual(r.ssml_hints[0]["reading"], "bei4")
        rej = [x for x in r.audit["mapping_rejections"]
               if x["substitute"] == "背"]
        self.assertTrue(rej, r.audit["mapping_rejections"])
        self.assertEqual(r.audit["counts"]["C"], 1, r.audit)

    # 6 ------------------------------------------------------------------
    def test_06_Unknown_with_pinyin_pinned(self):
        # 龘 is not in the fixture dictionary at all.
        self.assertIsNone(self.engine.d.char_info("龘"))
        r = self.engine.convert("龘", pinyin=["da2"])
        self.assertEqual(r.status, "ok", r.questions)
        self.assertEqual(len(r.ssml_hints), 1, r.ssml_hints)
        self.assertEqual(r.ssml_hints[0]["reading"], "da2")
        self.assertEqual(r.audit["counts"]["C"], 1, r.audit)

    # 7 ------------------------------------------------------------------
    def test_07_Unknown_without_pinyin_residual_question(self):
        r = self.engine.convert("龘")
        self.assertEqual(r.status, "questions")
        self.assertEqual(len(r.questions), 1, r.questions)
        self.assertEqual(r.questions[0]["char"], "龘")
        self.assertIsNone(r.questions[0]["target_reading"])
        self.assertFalse(r.audit["passed"], r.audit)

    # 8 ------------------------------------------------------------------
    def test_08_Semantic_isolated_tongjia_not_blocking(self):
        r = self.engine.convert("節彼南山",
                                pinyin="jie2 bi3 nan2 shan1".split())
        self.assertEqual(r.status, "ok", r.questions)
        self.assertIn("節", r.output)
        self.assertNotIn("截", r.output)
        self.assertTrue(r.audit["passed"], r.audit)
        sem = [s for s in r.semantic_questions if s["char"] == "節"]
        self.assertEqual(len(sem), 1, r.semantic_questions)
        self.assertEqual(sem[0]["kind"], "isolated_evidence")
        self.assertIn("截", [c["char"] for c in sem[0]["candidates"]])
        self.assertEqual(sem[0]["recommendation"]["action"], "substitute")
        self.assertEqual(sem[0]["recommendation"]["char"], "截")

    # 9 ------------------------------------------------------------------
    def test_09_Substitute_index_no_longer_auto_lands(self):
        r = self.engine.convert("親", pinyin=["xin1"])
        self.assertEqual(r.status, "ok", r.questions)
        self.assertEqual(r.output, "親")
        self.assertEqual(len(r.ssml_hints), 1, r.ssml_hints)
        self.assertEqual(r.ssml_hints[0]["reading"], "xin1")
        self.assertFalse([c for c in r.changes if c["via"] == "substitute"],
                         r.changes)

    # 10 -----------------------------------------------------------------
    def _cli(self, work: Path, home: Path, *argv: str):
        env = dict(os.environ)
        env["HOME"] = str(home)
        env["PYTHONPATH"] = str(SRC_DIR) + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run(
            [sys.executable, "-m", "zhengyin.cli", *argv],
            capture_output=True, text=True, env=env)

    def test_10_CLI_defaults_and_audit_subcommand(self):
        work = Path(self.tmp.name) / "cli"
        work.mkdir()
        home = work / "home"
        home.mkdir()
        (work / "guo.txt").write_text("國", encoding="utf-8")
        (work / "guo.pinyin.txt").write_text("guo2", encoding="utf-8")
        proc = self._cli(work, home, "convert", str(work / "guo.txt"),
                         "-o", str(work / "out.txt"),
                         "--pinyin", str(work / "guo.pinyin.txt"),
                         "--db", str(self.db_path))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue((work / "out.txt").exists())
        self.assertTrue((work / "out.txt.ssml.txt").exists())
        self.assertTrue((work / "ssml_hints.json").exists())
        audit = json.loads((work / "audit.json").read_text(encoding="utf-8"))
        self.assertEqual(audit["counts"]["A"], 1, audit)
        self.assertTrue(audit["passed"], audit)
        # standalone audit: 行 xing2 unlocked -> exit 2, B=1
        (work / "xing.txt").write_text("行", encoding="utf-8")
        (work / "xing.pinyin.txt").write_text("xing2", encoding="utf-8")
        proc = self._cli(work, home, "audit", str(work / "xing.txt"),
                         "--pinyin", str(work / "xing.pinyin.txt"),
                         "--audit", str(work / "audit2.json"),
                         "--db", str(self.db_path))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        audit2 = json.loads((work / "audit2.json").read_text(encoding="utf-8"))
        self.assertEqual(audit2["counts"]["B"], 1, audit2)
        # the same product plus its pin hints -> exit 0, C=1
        proc = self._cli(work, home, "convert", str(work / "xing.txt"),
                         "-o", str(work / "xing.out.txt"),
                         "--pinyin", str(work / "xing.pinyin.txt"),
                         "--db", str(self.db_path))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        proc = self._cli(work, home, "audit", str(work / "xing.out.txt"),
                         "--pinyin", str(work / "xing.pinyin.txt"),
                         "--ssml-hints", str(work / "ssml_hints.json"),
                         "--audit", str(work / "audit3.json"),
                         "--db", str(self.db_path))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        audit3 = json.loads((work / "audit3.json").read_text(encoding="utf-8"))
        self.assertEqual(audit3["counts"]["C"], 1, audit3)
        # unknown glyph without pinyin -> residual, exit 2, sound_questions
        (work / "unk.txt").write_text("龘", encoding="utf-8")
        proc = self._cli(work, home, "convert", str(work / "unk.txt"),
                         "-o", str(work / "unk.out.txt"),
                         "--questions", str(work / "sound_questions.json"),
                         "--db", str(self.db_path))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        sq = work / "sound_questions.json"
        self.assertTrue(sq.exists(), proc.stdout)
        self.assertEqual(json.loads(sq.read_text(encoding="utf-8"))[0]["char"],
                         "龘")

    # 12 -----------------------------------------------------------------
    def test_12_Audit_bare_syllable_modern_fallback(self):
        # 子 classically reads zi3/zi5, modern only zi3: a bare "zi"
        # syllable is undetermined against the classical list but
        # determined against modern readings -> A, exit-state pass.
        from zhengyin.audit import audit_text
        d = Dict(self.db_path)
        a = audit_text(d, "子", ["zi"])
        self.assertEqual(a["counts"]["A"], 1, a)
        self.assertTrue(a["passed"], a)

    # 11 -----------------------------------------------------------------
    def test_11_Rulings_invalid_substitute_rejected(self):
        work = Path(self.tmp.name) / "rul"
        work.mkdir()
        home = work / "home"
        home.mkdir()
        (work / "qian.txt").write_text("謙", encoding="utf-8")
        (work / "qian.pinyin.txt").write_text("qie4", encoding="utf-8")
        (work / "rulings.json").write_text(json.dumps(
            {"rulings": [{"char": "謙", "reading": "qie4",
                          "action": "substitute", "substitute": "球"}]},
            ensure_ascii=False), encoding="utf-8")
        proc = self._cli(work, home, "convert", str(work / "qian.txt"),
                         "-o", str(work / "out.txt"),
                         "--pinyin", str(work / "qian.pinyin.txt"),
                         "--rulings", str(work / "rulings.json"),
                         "--db", str(self.db_path))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("未通过校验", proc.stderr)


if __name__ == "__main__":
    unittest.main()
