"""Acceptance tests for the zhengyin conversion engine.

Uses the zhengyin-dict database (env ZHENGYIN_DB, or the sibling
zhengyin-dict checkout's dist build). Skipped when no database exists.
Run: ZHENGYIN_DB=... python3 -m unittest discover -s tests
"""
import os
import tempfile
import unittest
from pathlib import Path

from zhengyin.datalayer import Dict
from zhengyin.engine import Engine
from zhengyin.learnstore import LearnStore

_DB_CANDIDATES = [
    os.environ.get("ZHENGYIN_DB"),
    str(Path.home() / "workspace/zhengyin-dict/dist/zhengyin.db"),
]
DB_PATH = next((p for p in _DB_CANDIDATES if p and Path(p).exists()), None)

LUNYU = "學而時習之，不亦說乎"
LUNYU_PY = "xue2 er2 shi2 xi2 zhi1 bu4 yi4 yue4 hu1".split()
LUNYU_PY_MARKED = "xué ér shí xí zhī bù yì yuè hū".split()


@unittest.skipUnless(DB_PATH, "zhengyin.db not available")
class Acceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.learned = LearnStore(Path(self.tmp.name) / "learned.jsonl")
        self.engine = Engine(Dict(DB_PATH), self.learned)

    def tearDown(self):
        self.tmp.cleanup()

    def test_case1a_pinyin_lock_tongjia(self):
        r = self.engine.convert(LUNYU, pinyin=LUNYU_PY)
        self.assertEqual(r.status, "ok")
        self.assertIn("悦", r.output)
        rec = [c for c in r.changes if c["char"] == "說"]
        self.assertTrue(rec and rec[0]["via"] == "tongjia", r.changes)

    def test_case1a_tone_marked_pinyin(self):
        r = self.engine.convert(LUNYU, pinyin=LUNYU_PY_MARKED)
        self.assertEqual(r.status, "ok")
        self.assertIn("悦", r.output)

    def test_case1b_no_pinyin_asks_with_yue_candidate(self):
        r = self.engine.convert(LUNYU)
        self.assertEqual(r.status, "questions")
        qs = [q for q in r.questions if q["char"] == "說"]
        self.assertTrue(qs, r.questions)
        cands = qs[0]["candidates"]
        self.assertTrue(
            any(c["char"] == "悦" and c["reading"] == "yue4"
                and "喜悦" in c.get("sense", "") for c in cands),
            cands)

    def test_case2_substitute_for_xin1(self):
        r = self.engine.convert("在親民", pinyin="zai4 xin1 min2".split())
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.output, "在新民")
        rec = [c for c in r.changes if c["char"] == "親"]
        self.assertTrue(rec and rec[0]["via"] == "substitute", r.changes)

    def test_case3_answer_learn_rerun(self):
        r1 = self.engine.convert("共")
        self.assertEqual(r1.status, "questions")
        qid = r1.questions[0]["id"]
        r2 = self.engine.convert("共", answers={qid: "供"})
        self.assertEqual(r2.status, "ok")
        self.assertEqual(r2.output, "供")
        self.assertEqual(len(self.learned.entries), 1)
        r3 = self.engine.convert("共")
        self.assertEqual(r3.status, "ok")
        self.assertEqual(r3.output, "供")

    def test_case4_wrong_answer_rejected(self):
        r1 = self.engine.convert("共", pinyin=["gong1"])
        self.assertEqual(r1.status, "questions")
        qid = r1.questions[0]["id"]
        self.assertEqual(r1.questions[0]["target_reading"], "gong1")
        r2 = self.engine.convert("共", pinyin=["gong1"], answers={qid: "球"})
        self.assertEqual(r2.status, "questions")
        self.assertIn("未通过校验", r2.questions[0]["reason"])
        self.assertEqual(len(self.learned.entries), 0)

    def test_pinyin_count_mismatch(self):
        with self.assertRaises(ValueError):
            self.engine.convert(LUNYU, pinyin=["xue2"])


if __name__ == "__main__":
    unittest.main()
