"""Metric invariants: CER/accuracy in [0,1] via dist/max normalization.

Guards the P0 fixes: unbounded ref-normalized CER, negative word_accuracy on
unspaced Thai, and the degenerate sacrebleu-13a BLEU on Thai. CER now uses
dist / max(len(ref), len(hyp)), which is bounded in [0,1] by construction
(Levenshtein distance never exceeds the longer text's length).
"""

from __future__ import annotations

import pytest

from goat_model.metrics import cer, corpus_bleu, trace_cer, word_accuracy


def test_cer_bounded_when_hypothesis_longer_than_reference() -> None:
    # dist("สวัสดี", 20 junk chars) = 20; denom = max(5, 20) = 20 -> 1.0.
    assert cer("สวัสดี", "x" * 20) == 1.0


def test_cer_lenient_when_hypothesis_adds_junk() -> None:
    # dist("abcde", "abcdexxxxx") = 5 insertions, denom = max(5, 10) = 10:
    # the whole reference survived, half the output is extra text (bounded
    # formula). Thai is avoided here: combining marks make "สวัสดี" 6 codepoints.
    assert cer("abcde", "abcdexxxxx") == pytest.approx(0.5)


def test_cer_zero_iff_identical() -> None:
    assert cer("ทองเนื้อเก้า", "ทองเนื้อเก้า") == 0.0


def test_cer_empty_cases() -> None:
    assert cer("", "") == 0.0
    assert cer("สวัสดี", "") == 1.0
    assert cer("", "สวัสดี") == 1.0


def test_cer_never_exceeds_one_on_garbage() -> None:
    ref = "สวัสดีครับ ยินดีต้อนรับ"
    for junk in ("x" * 500, "th" * 300):
        assert 0.0 <= cer(ref, junk) <= 1.0


def test_word_accuracy_is_one_minus_cer() -> None:
    ref, hyp = "สวัสดีครับ ยินดีต้อนรับ", "สวัสดีครับ xxxxxxxx"
    assert word_accuracy(ref, hyp) == pytest.approx(1.0 - cer(ref, hyp))
    assert 0.0 <= word_accuracy(ref, hyp) <= 1.0
    assert word_accuracy("ทองเนื้อเก้า", "ทองเนื้อเก้า") == 1.0


def test_trace_cer_matches_cer_and_lists_every_step() -> None:
    ref, hyp = "สวัสดีครับ ยินดีต้อนรับ", "สวัสดีครับ ยินดี"
    t = trace_cer(ref, hyp)
    assert t["cer"] == pytest.approx(cer(ref, hyp))
    assert t["word_accuracy"] == pytest.approx(1.0 - cer(ref, hyp))
    assert t["ref_len"] == len(ref) and t["hyp_len"] == len(hyp)
    assert t["normalizer"] == max(len(ref), len(hyp))
    # edit operations are consistent with the distance when traced
    if t["substitutions"] is not None:
        ops = t["substitutions"] + t["deletions"] + t["insertions"]
        assert ops == t["distance"]


def test_trace_cer_ops_add_up_on_junk() -> None:
    t = trace_cer("สวัสดี", "x" * 20)
    assert t["substitutions"] + t["deletions"] + t["insertions"] == t["distance"]
    assert t["distance"] == 20 and t["cer"] == 1.0


# BLEU needs >= 4 word-tokens per corpus for the 4-gram precision to be
# defined (sacrebleu scores 0.0 otherwise); use realistic Thai sentences.
_LONG1 = "สวัสดีครับ ยินดีต้อนรับเข้าสู่ระบบการเรียนรู้ของเรา"
_LONG2 = "การประมวลผลภาษาธรรมชาติเป็นสาขาหนึ่งของปัญญาประดิษฐ์"
_LONG3 = "ตอนนี้ผมกำลังอ่านหนังสือภาษาไทยอยู่ที่ห้องสมุดมหาวิทยาลัย"


@pytest.mark.parametrize("ref", [_LONG1, _LONG2, _LONG3])
def test_corpus_bleu_identity_is_100(ref: str) -> None:
    pytest.importorskip("pythainlp")  # mt extra only
    assert corpus_bleu([ref], [ref]) == pytest.approx(100.0)


def test_corpus_bleu_different_texts_below_100() -> None:
    pytest.importorskip("pythainlp")
    score = corpus_bleu([_LONG2], [_LONG1 + " " + _LONG3])
    assert 0.0 <= score < 100.0
