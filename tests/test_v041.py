"""v0.4.1 tests: the context-review gate （语境审） for substitutes.

Every dictionary-sourced substitute (tongjia / verified podu /
corroboration pick) and every substitute *answer* must pass two gates
before it lands: (a) the hard gate (``strict_substitute_ok`` for
dictionary candidates — a failure is recorded as a rejection and the
candidate never becomes a proposal) and (b) a context review by the
executing agent: the candidate is written out as a proposal
(``substitute_proposals.json``, exit 2, no product) and lands only on
an explicit approval carrying a ``sense_fit`` reason. Rejection pins
the original glyph. Only learned user-confirmed entries and
``prescribed`` substitutes land directly.

Uses a self-built fixture db with the dict v0.3.1 modern columns, so
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
    ("天", ["tian1"], "tian1", ["tian1"], 0, "tian1", ["tian1"]),
    ("地", ["di4", "de5"], "de5", ["de5", "di4"], 1, None, ["di4", "de5"]),
    ("第", ["di4"], "di4", ["di4"], 0, "di4", ["di4"]),
    ("之", ["zhi1"], "zhi1", ["zhi1"], 0, "zhi1", ["zhi1"]),
    ("道", ["dao4"], "dao4", ["dao4"], 0, "dao4", ["dao4"]),
    ("好", ["hao3", "hao4"], "hao3", ["hao3", "hao4"], 1, None, ["hao3", "hao4"]),
    ("學", ["xue2"], "xue2", ["xue2"], 0, "xue2", ["xue2"]),
    ("近", ["jin4"], "jin4", ["jin4"], 0, "jin4", ["jin4"]),
    ("乎", ["hu1"], "hu1", ["hu1"], 0, "hu1", ["hu1"]),
    ("知", ["zhi1", "zhi4"], "zhi1", ["zhi1"], 0, "zhi1", ["zhi1"]),
    ("智", ["zhi4"], "zhi4", ["zhi4"], 0, "zhi4", ["zhi4"]),
    ("子", ["zi3", "zi5"], "zi3", ["zi3"], 0, "zi3", ["zi3"]),
    ("路", ["lu4"], "lu4", ["lu4"], 0, "lu4", ["lu4"]),
    ("問", ["wen4"], "wen4", ["wen4"], 0, "wen4", ["wen4"]),
    ("強", ["qiang2", "qiang3", "jiang4"], "qiang2",
     ["qiang2", "qiang3"], 1, None, ["qiang2", "qiang3"]),
    ("襁", ["qiang3"], "qiang3", ["qiang3"], 0, "qiang3", ["qiang3"]),
    ("倍", ["bei4", "pei2"], "pei2", ["pei2", "bei4"], 1, None, []),
    ("背", ["bei4", "bei1"], "bei4", ["bei4", "bei1"], 1, None, ["bei4"]),
]

TONGJIA = [
    # (id, char, benzi, reading, sense, example, char_simp, benzi_simp,
    #  src, src_ref)
    # 地 di4 通 第, sense is the adverb "但，只" — wrong for 天地 (noun).
    (1, "地", "第", "di4", "副词。但，只", "西曹地忍之", "", "",
     "fixture", "《汉书》"),
    # 知 zhi4 通 智 "明智" — right for 好學近乎知.
    (2, "知", "智", "zhi4", "明智", "雖有明知之心", "", "",
     "fixture", "《礼记》"),
    # 強 qiang3 通 襁 "在襁褓中撫養" — wrong for 子路問強.
    (3, "強", "襁", "qiang3", "在襁褓中撫養", "程嬰强育其真孤", "", "",
     "fixture", "fixture"),
    # 倍 bei4 通 背: semantic mapping whose benzi is polyphonic —
    # fails the hard gate, must never become a proposal.
    (4, "倍", "背", "bei4", "背負", "", "", "", "fixture", "fixture"),
]


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
        con.execute("INSERT INTO meta VALUES ('version', 'fixture-0.4.1')")
        for ch, readings, default, modern, poly, unique, common in CHARS:
            con.execute(
                "INSERT INTO chars VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (ch, json.dumps(readings), default, 100, 1, "fixture",
                 "fixture", json.dumps(common), json.dumps(modern),
                 "fixture-xhc", poly, unique))
        for row in TONGJIA:
            con.execute(
                "INSERT INTO tongjia VALUES (?,?,?,?,?,?,?,?,?,?)", row)
        con.commit()
    finally:
        con.close()
    return path


class V041(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = build_fixture_db(Path(self.tmp.name) / "fixture.db")
        self.learned = LearnStore(Path(self.tmp.name) / "learned.jsonl")
        self.engine = Engine(Dict(self.db_path), self.learned)

    def tearDown(self):
        self.tmp.cleanup()

    # 1 ------------------------------------------------------------------
    def test_01_di_proposal_then_reject_pins_original(self):
        # 地 in 天地之道 is the noun "earth": the tongjia benzi 第
        # (adverb sense) must be proposed, not landed.
        r = self.engine.convert("天地之道",
                                pinyin="tian1 di4 zhi1 dao4".split())
        self.assertEqual(r.status, "proposals", r.questions)
        self.assertEqual(len(r.proposals), 1, r.proposals)
        p = r.proposals[0]
        self.assertEqual(p["id"], "p1")
        self.assertEqual(p["han_index"], 1)
        self.assertEqual(p["index"], 1)
        self.assertEqual(p["char"], "地")
        self.assertEqual(p["reading"], "di4")
        self.assertEqual(p["candidate"], "第")
        self.assertEqual(p["via"], "tongjia")
        self.assertIn("但", p["sense"])
        self.assertEqual(p["sentence"], "天地之道")
        # Reject: the original glyph stays, pinned at di4, and the
        # rejection is on the audit trail as a context_review record.
        r2 = self.engine.convert(
            "天地之道", pinyin="tian1 di4 zhi1 dao4".split(),
            substitute_review={"p1": {
                "decision": "reject",
                "sense_fit": "地在此为名词天地，非副词但只"}})
        self.assertEqual(r2.status, "ok", r2.questions)
        self.assertEqual(r2.output, "天地之道")
        self.assertNotIn("第", r2.output)
        hints = [h for h in r2.ssml_hints if h["char"] == "地"]
        self.assertTrue(hints and hints[0]["reading"] == "di4",
                        r2.ssml_hints)
        rev = r2.audit["substitute_reviews"]
        self.assertEqual(rev["rejected"], 1, rev)
        self.assertEqual(rev["approved"], 0, rev)
        rec = rev["records"][0]
        self.assertEqual(rec["kind"], "context_review")
        self.assertEqual(rec["candidate"], "第")
        self.assertIn("名词", rec["reason"])
        self.assertTrue(r2.audit["passed"], r2.audit)

    # 2 ------------------------------------------------------------------
    def test_02_zhi_proposal_approve_lands_with_sense_fit(self):
        r = self.engine.convert("好學近乎知",
                                pinyin="hao4 xue2 jin4 hu1 zhi4".split())
        self.assertEqual(r.status, "proposals", r.questions)
        self.assertEqual(len(r.proposals), 1, r.proposals)
        p = r.proposals[0]
        self.assertEqual((p["char"], p["candidate"], p["via"]),
                         ("知", "智", "tongjia"))
        fit = "知通智为智慧义，与好學近乎知所言近于智慧的语境相合"
        r2 = self.engine.convert(
            "好學近乎知", pinyin="hao4 xue2 jin4 hu1 zhi4".split(),
            substitute_review={p["id"]: {"decision": "approve",
                                          "sense_fit": fit}})
        self.assertEqual(r2.status, "ok", r2.questions)
        self.assertEqual(r2.output, "好學近乎智")
        rec = [c for c in r2.changes if c["char"] == "知"]
        self.assertEqual(len(rec), 1, r2.changes)
        self.assertEqual(rec[0]["to"], "智")
        self.assertEqual(rec[0]["via"], "tongjia")
        self.assertEqual(rec[0]["sense_fit"], fit)
        rev = r2.audit["substitute_reviews"]
        self.assertEqual(rev["approved"], 1, rev)
        self.assertTrue(r2.audit["passed"], r2.audit)

    # 3 ------------------------------------------------------------------
    def test_03_qiang_proposal_rejected_falls_back_to_pin(self):
        r = self.engine.convert("子路問強",
                                pinyin="zi3 lu4 wen4 qiang3".split())
        self.assertEqual(r.status, "proposals", r.questions)
        self.assertEqual([p["candidate"] for p in r.proposals], ["襁"],
                         r.proposals)
        r2 = self.engine.convert(
            "子路問強", pinyin="zi3 lu4 wen4 qiang3".split(),
            substitute_review={"p3": {
                "decision": "reject",
                "sense_fit": "問強之強为强弱义，非襁褓抚养义"}})
        self.assertEqual(r2.status, "ok", r2.questions)
        self.assertEqual(r2.output, "子路問強")
        self.assertNotIn("襁", r2.output)
        hints = [h for h in r2.ssml_hints if h["char"] == "強"]
        self.assertTrue(hints and hints[0]["reading"] == "qiang3",
                        r2.ssml_hints)
        self.assertEqual(r2.audit["substitute_reviews"]["rejected"], 1,
                         r2.audit)

    # 4 ------------------------------------------------------------------
    def test_04_learned_confirmed_entry_lands_without_proposal(self):
        # A user-confirmed learned entry (char + reading + sense) is
        # exempt from the gate: it lands directly, no proposal, exit ok.
        self.learned.add("知", "好學近乎知", "zhi4", "智", "明智")
        r = self.engine.convert("好學近乎知",
                                pinyin="hao4 xue2 jin4 hu1 zhi4".split())
        self.assertEqual(r.status, "ok", r.questions)
        self.assertEqual(r.proposals, [])
        self.assertEqual(r.output, "好學近乎智")
        rec = [c for c in r.changes if c["char"] == "知"]
        self.assertTrue(rec and rec[0]["via"] == "learned", r.changes)
        self.assertTrue(r.audit["passed"], r.audit)

    # 5 ------------------------------------------------------------------
    def test_05_hard_gate_failure_never_becomes_proposal(self):
        # 倍 bei4 -> 背 is semantically mapped but 背 is polyphonic:
        # the hard gate rejects it (trail entry) and it must never
        # appear in the proposals; 倍 itself is pinned instead.
        r = self.engine.convert("倍", pinyin=["bei4"])
        self.assertEqual(r.status, "ok", r.questions)
        self.assertEqual(r.proposals, [])
        self.assertEqual(r.output, "倍")
        self.assertEqual(len(r.ssml_hints), 1, r.ssml_hints)
        rej = [x for x in r.audit["mapping_rejections"]
               if x["substitute"] == "背"]
        self.assertTrue(rej, r.audit["mapping_rejections"])

    # 6 ------------------------------------------------------------------
    def test_06_approve_without_sense_fit_stays_pending(self):
        # An approval with an empty sense_fit is not a decision: the
        # proposal stands (the CLI rejects such files outright, tested
        # in test_07; the engine is defensive about it too).
        r = self.engine.convert(
            "好學近乎知", pinyin="hao4 xue2 jin4 hu1 zhi4".split(),
            substitute_review={"p4": {"decision": "approve",
                                       "sense_fit": ""}})
        self.assertEqual(r.status, "proposals", r.questions)
        self.assertEqual(len(r.proposals), 1, r.proposals)

    # 7 ------------------------------------------------------------------
    def _cli(self, work: Path, home: Path, *argv: str):
        env = dict(os.environ)
        env["HOME"] = str(home)
        env["PYTHONPATH"] = str(SRC_DIR) + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run(
            [sys.executable, "-m", "zhengyin.cli", *argv],
            capture_output=True, text=True, env=env)

    def test_07_CLI_proposals_exit2_review_roundtrip(self):
        work = Path(self.tmp.name) / "cli"
        work.mkdir()
        home = work / "home"
        home.mkdir()
        (work / "zhi.txt").write_text("好學近乎知", encoding="utf-8")
        (work / "zhi.pinyin.txt").write_text(
            "hao4 xue2 jin4 hu1 zhi4", encoding="utf-8")
        proc = self._cli(work, home, "convert", str(work / "zhi.txt"),
                         "-o", str(work / "out.txt"),
                         "--pinyin", str(work / "zhi.pinyin.txt"),
                         "--db", str(self.db_path))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertFalse((work / "out.txt").exists())
        props = json.loads((work / "substitute_proposals.json").read_text(
            encoding="utf-8"))
        self.assertEqual(len(props), 1, props)
        self.assertEqual(props[0]["candidate"], "智")
        # approve without sense_fit -> CLI error, exit 1
        (work / "bad.json").write_text(json.dumps(
            {"reviews": [{"id": props[0]["id"], "decision": "approve",
                          "sense_fit": ""}]}, ensure_ascii=False),
            encoding="utf-8")
        proc = self._cli(work, home, "convert", str(work / "zhi.txt"),
                         "-o", str(work / "out.txt"),
                         "--pinyin", str(work / "zhi.pinyin.txt"),
                         "--substitute-review", str(work / "bad.json"),
                         "--db", str(self.db_path))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("sense_fit", proc.stderr)
        # proper approval -> exit 0, substitute landed, audit trail set
        (work / "review.json").write_text(json.dumps(
            {"reviews": [{"id": props[0]["id"], "decision": "approve",
                          "sense_fit": "智慧义合此句语境"}]},
            ensure_ascii=False), encoding="utf-8")
        proc = self._cli(work, home, "convert", str(work / "zhi.txt"),
                         "-o", str(work / "out.txt"),
                         "--pinyin", str(work / "zhi.pinyin.txt"),
                         "--substitute-review", str(work / "review.json"),
                         "--changes", str(work / "changes.json"),
                         "--db", str(self.db_path))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual((work / "out.txt").read_text(encoding="utf-8"),
                         "好學近乎智")
        audit = json.loads((work / "audit.json").read_text(encoding="utf-8"))
        self.assertEqual(audit["substitute_reviews"]["approved"], 1, audit)
        changes = json.loads((work / "changes.json").read_text(
            encoding="utf-8"))["changes"]
        rec = [c for c in changes if c["char"] == "知"]
        self.assertEqual(rec[0]["sense_fit"], "智慧义合此句语境", changes)


if __name__ == "__main__":
    unittest.main()
