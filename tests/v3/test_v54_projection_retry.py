# -*- coding: utf-8 -*-
"""v54 投影原子发布与瞬态重试回归测试（任务书 F 部分）。"""
from pathlib import Path
import os
import tempfile
import pytest

from dag_agent.projection import _atomic_write, render_mission_versioned, current_version
from dag_agent.repository import Repository
from dag_agent.graph import Graph


def test_atomic_write_transient_retry(tmp_path, monkeypatch):
    """测试前两次 os.replace 遭遇瞬态 OSError，第三次成功，文件正常发布。"""
    target = tmp_path / "board" / "test.md"
    content = "# Hello World"

    calls = 0
    orig_replace = os.replace

    def mock_replace(src, dst):
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise OSError(13, "Permission denied (simulated lock)")
        return orig_replace(src, dst)

    monkeypatch.setattr(os, "replace", mock_replace)
    _atomic_write(target, content)

    assert calls == 3
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == content


def test_atomic_write_exhaustion_cleans_tmp(tmp_path, monkeypatch):
    """测试持续 OSError 耗尽 5 次重试，抛出异常且临时文件被妥善清理。"""
    target = tmp_path / "board" / "fail.md"
    content = "# Never written"

    calls = 0

    def mock_replace(src, dst):
        nonlocal calls
        calls += 1
        raise OSError(32, "The process cannot access the file because it is being used by another process")

    monkeypatch.setattr(os, "replace", mock_replace)
    with pytest.raises(OSError):
        _atomic_write(target, content)

    assert calls == 5
    assert not target.exists()
    # 临时目录不留 .tmp 残留
    tmps = list(target.parent.glob("*.tmp"))
    assert len(tmps) == 0


def test_render_mission_versioned_failure_does_not_update_marker(tmp_path, monkeypatch):
    """测试渲染写失败时，版本标记文件 .projection_version 不会被误更新。"""
    db_path = tmp_path / "mission.db"
    repo = Repository(db_path)
    g = Graph(repo)
    mid, root = g.create_mission("proj-test", brief="brief", root_check="python -c \"\"")

    board = tmp_path / "board"
    board.mkdir(parents=True, exist_ok=True)
    marker = board / ".projection_version"
    marker.write_text("0", encoding="utf-8")

    def mock_atomic_write(path, text):
        if "任务总览.md" in str(path):
            raise OSError(5, "Access is denied (simulated disk error)")
        path.write_text(text, encoding="utf-8")

    monkeypatch.setattr("dag_agent.projection._atomic_write", mock_atomic_write)

    with pytest.raises(OSError):
        render_mission_versioned(repo, g, mid)

    # 标记必须保持为 0，不被错误推进
    assert marker.read_text(encoding="utf-8").strip() == "0"
    repo.close()
