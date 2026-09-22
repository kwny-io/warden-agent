"""审计归档（只追加 + 完整性清单）测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from warden_agent.runtime.archive import archive_records, verify_archive

_ROWS = [
    {"id": 1, "at": 1.0, "correlation_id": "c1", "operation": "QUERY",
     "prev_hash": "", "hash": "h1"},
    {"id": 2, "at": 2.0, "correlation_id": "c2", "operation": "COMMAND",
     "prev_hash": "h1", "hash": "h2"},
]


def test_归档后可校验通过(tmp_path: Path) -> None:
    info = archive_records(tmp_path, _ROWS, chain_key=None, name="a.jsonl")
    assert info["file"] == "a.jsonl"
    ok, detail = verify_archive(tmp_path, chain_key=None)
    assert ok, detail


def test_拒绝覆盖已有归档(tmp_path: Path) -> None:
    archive_records(tmp_path, _ROWS, chain_key=None, name="a.jsonl")
    with pytest.raises(FileExistsError):
        archive_records(tmp_path, _ROWS, chain_key=None, name="a.jsonl")


def test_文件名不能带目录(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        archive_records(tmp_path, _ROWS, chain_key=None, name="../evil.jsonl")


def test_多个归档串成清单链(tmp_path: Path) -> None:
    archive_records(tmp_path, _ROWS, chain_key=None, name="a.jsonl")
    archive_records(tmp_path, _ROWS[:1], chain_key=None, name="b.jsonl")
    ok, detail = verify_archive(tmp_path, chain_key=None)
    assert ok, detail


def test_篡改归档文件会被发现(tmp_path: Path) -> None:
    archive_records(tmp_path, _ROWS, chain_key=None, name="a.jsonl")
    target = tmp_path / "a.jsonl"
    target.write_text(target.read_text(encoding="utf-8") + '{"id":3}\n', encoding="utf-8")
    ok, detail = verify_archive(tmp_path, chain_key=None)
    assert not ok and "sha256" in detail


def test_删除归档文件会被发现(tmp_path: Path) -> None:
    archive_records(tmp_path, _ROWS, chain_key=None, name="a.jsonl")
    (tmp_path / "a.jsonl").unlink()
    ok, detail = verify_archive(tmp_path, chain_key=None)
    assert not ok and "不存在" in detail


def test_篡改清单会被发现(tmp_path: Path) -> None:
    archive_records(tmp_path, _ROWS, chain_key=None, name="a.jsonl")
    manifest = tmp_path / "manifest.jsonl"
    text = manifest.read_text(encoding="utf-8")
    # 把登记的 sha256 改成别的（试图"连清单一起改"，让文件改动看起来合法）
    manifest.write_text(text.replace('"sha256": "', '"sha256": "0'), encoding="utf-8")
    ok, detail = verify_archive(tmp_path, chain_key=None)
    assert not ok and "清单" in detail
