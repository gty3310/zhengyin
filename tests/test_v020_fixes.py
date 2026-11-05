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
        # supersede v0.4.0 (was: v0.2.0 exemption, then v0.3.0 grouped
        # question): wei2 is locked, != default wei4, but differs only
        # in tone — the sound closure auto-pins the tone. No question.
        r = self.engine.convert("為人君，止於仁",
                                pinyin="wei2 ren2 jun1 zhi3 yu2 ren2".split())
        self.assertEqual(r.status, "ok", r)
        self.assertEqual(r.questions, [])
        self.assertEqual(r.output, "為人君，止於仁")
        hints = [h for h in r.ssml_hints if h["char"] == "為"]
        self.assertTrue(hints and hints[0]["reading"] == "wei2", r.ssml_hints)
        self.assertTrue(r.audit.get("passed"), r.audit.get("counts"))

    def test_2_xing_common_reading_retained(self):
        # supersede v0.4.0: same closure as test_1 — xing1 vs default
        # xing4 is tone-only, so it is auto-pinned, never asked.
        r = self.engine.convert("民興孝", pinyin="min2 xing1 xiao4".split())
        self.assertEqual(r.status, "ok", r)
        self.assertEqual(r.questions, [])
        self.assertEqual(r.output, "民興孝")
        hints = [h for h in r.ssml_hints if h["char"] == "興"]
        self.assertTrue(hints and hints[0]["reading"] == "xing1", r.ssml_hints)
        self.assertTrue(r.audit.get("passed"), r.audit.get("counts"))

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
        # supersede v0.4.0: 沒 mo4 is determined by the phrase table;
        # its tongjia competition （昧/殁） is semantic, so the sound
        # layer pins 沒 and the options move to semantic_questions.
        # (Semantic candidates are meaning options for the user, not
        # sound answers, so the old per-candidate sound validation no
        # longer applies to them; sound-answer candidate filtering is
        # still exercised by the residual loop in test_acceptance.)
        r = self.engine.convert("沒世不忘")
        self.assertEqual(r.status, "ok", r)
        self.assertEqual(r.questions, [])
        hints = [h for h in r.ssml_hints if h["char"] == "沒"]
        self.assertTrue(hints and hints[0]["reading"] == "mo4", r.ssml_hints)
        sem = [s for s in r.semantic_questions if s["char"] == "沒"]
        self.assertTrue(sem, r.semantic_questions)
        self.assertEqual(sem[0]["target_reading"], "mo4")
        self.assertIn("殁", [c["char"] for c in sem[0]["candidates"]],
                      sem[0]["candidates"])

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
        # supersede v0.4.0: hao4 locked + common + != default — the
        # sound closure auto-pins both 好 (tone-only vs default hao3).
        r = self.engine.convert("好好色", pinyin="hao4 hao4 se4".split())
        self.assertEqual(r.status, "ok", r)
        self.assertEqual(r.questions, [])
        self.assertEqual(r.output, "好好色")
        hints = [h for h in r.ssml_hints if h["char"] == "好"]
        self.assertEqual(len(hints), 2, r.ssml_hints)
        self.assertTrue(all(h["reading"] == "hao4" for h in hints))

    def test_7b_le_yao_still_asks(self):
        # supersede v0.4.0: the unverified podu suggestion no longer
        # asks either — yao4 is locked by pinyin, so 樂 is auto-pinned.
        r = self.engine.convert("有所好樂", pinyin="you3 suo3 hao4 yao4".split())
        self.assertEqual(r.status, "ok", r)
        self.assertEqual(r.questions, [])
        hints = [h for h in r.ssml_hints if h["char"] == "樂"]
        self.assertTrue(hints and hints[0]["reading"] == "yao4", r.ssml_hints)
        self.assertTrue(r.audit.get("passed"), r.audit.get("counts"))


if __name__ == "__main__":
    unittest.main()
