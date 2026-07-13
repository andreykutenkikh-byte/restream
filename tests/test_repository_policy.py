from pathlib import Path

from scripts.check_repository import check


def test_repository_policy() -> None:
    root = Path(__file__).resolve().parents[1]
    assert check(root) == []
    assert "restream.adojapan.ru" in (root / "README.md").read_text(encoding="utf-8")
