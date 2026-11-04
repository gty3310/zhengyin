"""v0.2.0 regression tests for the Daxue-run fixes.

Uses the real zhengyin-dict database (env ZHENGYIN_DB, or the sibling
zhengyin-dict checkout's dist build). Skipped when no database exists.
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


@unittest.skipUnless(DB_PATH, "zhengyin.db not available")
class V020Fixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dict = Dict(DB_PATH)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = Engine(self.dict, LearnStore(Path(self.tmp.name) / "learned.jsonl"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_1_wei_common_reading_retained(self):
        r = self.engine.convert("為人君，止於仁",
                                pinyin="wei2 ren2 jun1 zhi3 yu2 ren2".split())
        self.assertEqual(r.status, "ok", r.questions)
        self.assertEqual(r.output, "為人君，止於仁")
        self.assertFalse([c for c in r.changes if c["char"] == "為"], r.changes)

    def test_2_xing_common_reading_retained(self):
        r = self.engine.convert("民興孝", pinyin="min2 xing1 xiao4".split())
        self.assertEqual(r.status, "ok", r.questions)
        self.assertEqual(r.output, "民興孝")
        self.assertFalse([c for c in r.changes if c["char"] == "興"], r.changes)

    def test_3_bu_tone_normalised_raw_preserved(self):
        r = self.engine.convert("聽而不聞", pinyin="ting1 er2 bu2 wen2".split())
        self.assertEqual(r.status, "ok", r.questions)
        self.assertEqual(r.output, "聽而不聞")
        self.assertFalse([c for c in r.changes if c["char"] == "不"], r.changes)
        self.assertEqual(len(r.sentence_pinyin), 1)
        syllables = r.sentence_pinyin[0][1].split()
        self.assertEqual(syllables[2], "bu2", r.sentence_pinyin)

    def test_4_zeng_kept_no_question(self):
        r = self.engine.convert("曾子曰", pinyin="zeng1 zi3 yue1".split())
        self.assertEqual(r.status, "ok", r.questions)
        self.assertEqual(r.questions, [])
        self.assertEqual(r.output, "曾子曰")

    def test_5_mo_candidates_filtered_by_validation(self):
        r = self.engine.convert("沒世不忘")
        self.assertEqual(r.status, "questions", r)
        qs = [q for q in r.questions if q["char"] == "沒"]
        self.assertTrue(qs, r.questions)
        cands = qs[0]["candidates"]
        self.assertTrue(cands, qs[0])
        self.assertNotIn("昧", [c["char"] for c in cands], cands)
        for c in cands:
            info = self.dict.char_info(c["char"])
            self.assertIsNotNone(info, c)
            self.assertIn(c["reading"], info["readings"], c)
            self.assertEqual(info["default"], c["reading"], c)

    def test_6_jie_isolated_tongjia_not_auto_replaced(self):
        r = self.engine.convert(
            "節彼南山，維石巖巖",
            pinyin="jie2 bi3 nan2 shan1 wei2 shi2 yan2 yan2".split())
        self.assertNotIn("截彼", r.output)
        if r.status == "questions":
            qs = [q for q in r.questions if q["char"] == "節"]
            self.assertTrue(qs, r.questions)
            self.assertIn("截", [c["char"] for c in qs[0]["candidates"]], qs[0])
        else:
            self.assertEqual(r.status, "ok")
            self.assertIn("節彼", r.output)

    def test_7a_hao_common_reading_retained(self):
        r = self.engine.convert("好好色", pinyin="hao4 hao4 se4".split())
        self.assertEqual(r.status, "ok", r.questions)
        self.assertEqual(r.output, "好好色")

    def test_7b_le_yao_still_asks(self):
        r = self.engine.convert("有所好樂", pinyin="you3 suo3 hao4 yao4".split())
        self.assertEqual(r.status, "questions", r)
        qs = [q for q in r.questions if q["char"] == "樂"]
        self.assertTrue(qs, r.questions)
        self.assertEqual(qs[0]["target_reading"], "yao4")


if __name__ == "__main__":
    unittest.main()
