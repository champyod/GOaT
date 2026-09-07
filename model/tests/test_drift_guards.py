"""Drift guards: fail if removed transformers APIs are reintroduced.

The unit suite runs without transformers installed, so trainer-construction
breaks (tokenizer=, early_stopping_patience=, feature_extractor) slipped
through green tests. These line-shape checks close that exact gap: removed
kwargs were always passed on their own line, while legitimate uses
(DataCollatorForSeq2Seq(tokenizer=...), processing_class=...) sit inline.
"""
import re
from pathlib import Path

HERE = Path(__file__).resolve()
SRC = HERE.parents[1] / "src" / "goat_model"
TRAINERS = ["ocr/train.py", "mt/train.py"]


def test_no_tokenizer_kwarg_in_trainers():
    for path in TRAINERS:
        for i, line in enumerate((SRC / path).read_text(encoding="utf-8").splitlines(), 1):
            assert not re.match(r"\s*tokenizer\s*=[^=]*,\s*$", line), (
                f"{path}:{i}: removed Seq2SeqTrainer(tokenizer=) reintroduced; "
                "use processing_class= on transformers 5.x"
            )


def test_no_patience_kwarg_in_training_args():
    for path in TRAINERS:
        for i, line in enumerate((SRC / path).read_text(encoding="utf-8").splitlines(), 1):
            assert not re.match(r"\s*early_stopping_patience\s*=", line), (
                f"{path}:{i}: removed TrainingArguments kwarg reintroduced; "
                "use EarlyStoppingCallback instead"
            )


def test_no_feature_extractor():
    for path in ["ocr/train.py", "ocr/engine.py", "mt/engine.py"]:
        text = (SRC / path).read_text(encoding="utf-8")
        assert "feature_extractor" not in text, (
            f"{path}: removed TrOCRProcessor.feature_extractor reintroduced; "
            "use .image_processor / .tokenizer"
        )


if __name__ == "__main__":
    test_no_tokenizer_kwarg_in_trainers()
    test_no_patience_kwarg_in_training_args()
    test_no_feature_extractor()
    print("drift guards ok")
