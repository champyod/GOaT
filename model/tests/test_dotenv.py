"""Unit tests: .env loader (export wins, quoting, comments, missing file)."""
import os
import sys
from pathlib import Path

# Ensure model/src is on sys.path BEFORE any goat_model import
_HERE = Path(__file__).resolve()
SRC = _HERE.parents[1] / "src"  # model/src
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from goat_model.utils import load_dotenv


def test_load_dotenv_parses(tmp_path, monkeypatch):
    for key in ("T_A", "T_B", "T_C", "T_D", "T_E"):
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "T_A=1\nT_B=\"two words\"\nT_C='three'\n# a comment\n\nexport T_D=4\nT_E=\n",
        encoding="utf-8",
    )
    assert load_dotenv(env_file) == env_file
    assert os.environ["T_A"] == "1"
    assert os.environ["T_B"] == "two words"
    assert os.environ["T_C"] == "three"
    assert os.environ["T_D"] == "4"
    assert os.environ["T_E"] == ""


def test_export_wins_over_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("T_KEEP", "keep")
    env_file = tmp_path / ".env"
    env_file.write_text("T_KEEP=overwrite\n", encoding="utf-8")
    load_dotenv(env_file)
    assert os.environ["T_KEEP"] == "keep"


def test_missing_dotenv_returns_none(tmp_path):
    assert load_dotenv(tmp_path / "nope.env") is None


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        test_missing_dotenv_returns_none(Path(tmp))
    print("dotenv smoke ok")
