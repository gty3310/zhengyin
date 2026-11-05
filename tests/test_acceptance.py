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
        # supersede v0.4.0: 說 yue4 is determined (phrase table), so the
        # sound layer no longer asks — it pins 說 at yue4. Which benzi
        # （悦/閲…) expresses the sense is a *semantic* question now and
        # lands in semantic_questions, not in sound questions.
        r = self.engine.convert(LUNYU)
        self.assertEqual(r.status, "ok", r.questions)
        self.assertEqual(r.questions, [])
        hints = [h for h in r.ssml_hints if h["char"] == "說"]
        self.assertTrue(hints and hints[0]["reading"] == "yue4", r.ssml_hints)
        sem = [s for s in r.semantic_questions if s["char"] == "說"]
        self.assertTrue(sem, r.semantic_questions)
        self.assertEqual(sem[0]["target_reading"], "yue4")
        cands = sem[0]["candidates"]
        self.assertTrue(
            any(c["char"] == "悦" and c["reading"] == "yue4"
                and "拥戴悦服" in c.get("sense", "") for c in cands),
            cands)

    def test_case2_substitute_for_xin1(self):
        # supersede v0.4.0: the pure homophone substitute index no
        # longer lands automatically （新 here came from that index in
        # v0.1–v0.3). With no semantic-equivalent mapping for 親 xin1,
        # the sound closure pins 親 at xin1 instead; the plain-text
        # product keeps the original glyph.
        r = self.engine.convert("在親民", pinyin="zai4 xin1 min2".split())
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.output, "在親民")
        rec = [c for c in r.changes if c["char"] == "親"]
        self.assertTrue(rec and rec[0]["via"] == "pin", r.changes)
        hints = [h for h in r.ssml_hints if h["char"] == "親"]
        self.assertTrue(hints and hints[0]["reading"] == "xin1", r.ssml_hints)
        self.assertFalse([c for c in r.changes if c["via"] == "substitute"],
                         r.changes)

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
        # supersede v0.4.0: with the reading locked at gong1, the
        # tongjia competition （供/恭） is a semantic question and the
        # sound layer pins 共 — there is no sound question to answer.
        r1 = self.engine.convert("共", pinyin=["gong1"])
        self.assertEqual(r1.status, "ok", r1.questions)
        hints = [h for h in r1.ssml_hints if h["char"] == "共"]
        self.assertTrue(hints and hints[0]["reading"] == "gong1", r1.ssml_hints)
        sem = [s for s in r1.semantic_questions if s["char"] == "共"]
        self.assertTrue(sem, r1.semantic_questions)
        self.assertIn("供", [c["char"] for c in sem[0]["candidates"]],
                      sem[0]["candidates"])
        # Answer validation still rejects bad answers on the residual
        # sound-question loop (here: a two-character answer).
        q1 = self.engine.convert("共")
        self.assertEqual(q1.status, "questions")
        qid = q1.questions[0]["id"]
        r2 = self.engine.convert("共", answers={qid: "供供"})
        self.assertEqual(r2.status, "questions")
        self.assertIn("未通过校验", r2.questions[0]["reason"])
        self.assertEqual(len(self.learned.entries), 0)

    def test_pinyin_count_mismatch(self):
        with self.assertRaises(ValueError):
            self.engine.convert(LUNYU, pinyin=["xue2"])


if __name__ == "__main__":
    unittest.main()
