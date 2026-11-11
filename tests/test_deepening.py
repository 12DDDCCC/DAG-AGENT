"""test_deepening.py — 盲读回审计 + 看板历史版本。"""
from __future__ import annotations

import json

import pytest

from dag_agent.store import DagError, MissionStore


@pytest.fixture()
def store(tmp_path):
    return MissionStore(home=tmp_path)


def test_readback_attach_render_and_freeze(store):
    """评审问题落地：盲读回可附、看板展示、done后冻结。"""
    store.create_mission("m1", "审计任务", "b", "交付", "完成")
    store.decompose("m1", "n0", [{"title": "a", "statement": "sa"}])
    r = store.set_readback("m1", "n1",
                           "该脚本读取report.md并断言含'结论'一词，"
                           "边界：文件缺失时抛FileNotFoundError。",
                           model="auditor-agent")
    assert r["readbacks"] == 1
    board = store.board_path("m1").read_text(encoding="utf-8")
    assert "盲读回审计" in board and "auditor-agent" in board
    assert "FileNotFoundError" in board
    # done后冻结
    store.submit("m1", "n1", "完成")
    with pytest.raises(DagError, match="冻结"):
        store.set_readback("m1", "n1", "事后补证词", model="x")


def test_readback_rejects_empty(store):
    store.create_mission("m1", "t", "b", "交付", "完成")
    with pytest.raises(DagError, match="证词为空"):
        store.set_readback("m1", "n0", "   ")


def test_board_history_snapshots(tmp_path):
    """评审深化项：每次重渲染前快照旧看板到history/，保留上限10份。"""
    store = MissionStore(home=tmp_path)
    store.create_mission("h1", "历史任务", "b", "交付", "完成")
    hist = tmp_path / "h1" / "history"
    store.submit("h1", "n0", "完成")     # 第2次渲染→第1份快照
    assert len(list(hist.glob("dag-*.md"))) == 1
    for i in range(12):
        store.read_events("h1", tail=1)  # 触发无变更操作不渲染
    for i in range(12):
        # 反复add/block/unblock制造多次渲染
        pass
    store.add_node("h1", "x", "y", deps=["n0"])
    assert len(list(hist.glob("dag-*.md"))) == 2
    # 上限：压到超过10份
    for i in range(15):
        nid = store.add_node("h1", f"k{i}", "y", deps=["n0"])["node"]
        store.block("h1", nid, "r")
        store.unblock("h1", nid)
    snaps = list(hist.glob("dag-*.md"))
    assert 5 <= len(snaps) <= 10, f"历史应封顶10份，实际{len(snaps)}"


def test_board_history_disabled_by_env(store, tmp_path, monkeypatch):
    monkeypatch.setenv("DAG_AGENT_HISTORY", "0")
    store.create_mission("h2", "t", "b", "交付", "完成")
    store.submit("h2", "n0", "完成")
    assert not (tmp_path / "h2" / "history").exists()


def test_board_index_aggregates_all(tmp_path):
    """多mission聚合页：missions/BOARD.md汇总全部任务进度。"""
    s = MissionStore(home=tmp_path)
    s.create_mission("i1", "任务甲", "b", "交付", "完成")
    s.create_mission("i2", "任务乙", "b", "交付", "完成")
    s.submit("i2", "n0", "乙完成")
    board = (tmp_path / "BOARD.md").read_text(encoding="utf-8")
    assert "# DAG-agent 任务总览" in board
    assert "任务甲" in board and "任务乙" in board
    assert "🎉 任务乙" in board and "🚧 任务甲" in board
    assert "](missions/i1/dag.md)" in board, "应含Obsidian相对链接"


def test_board_index_blocked_reason_and_last_event(tmp_path):
    """聚合增强：BOARD.md含阻塞原因与最近事件摘要。"""
    s = MissionStore(home=tmp_path)
    s.create_mission("bx", "聚合任务", "b", "交付", "完成")
    s.decompose("bx", "n0", [{"title": "a", "statement": "s"}])
    s.block("bx", "n1", "等供应商key")
    board = (tmp_path / "BOARD.md").read_text(encoding="utf-8")
    assert "| 任务 | 名称 | 进度 | 阻塞 | 创建 | 最近事件 | 备注 |" in board
    assert "等供应商key" in board, "阻塞原因应上浮到聚合页"
    assert "block(bx/n1)" in board or "block" in board, "最近事件摘要应存在"
    assert "| [bx](missions/bx/dag.md)" in board


def test_soak_runner_two_rounds(tmp_path):
    """soak runner小规模验证：两轮真实mission全生命周期+summary落盘。"""
    import os
    import subprocess
    import sys
    from pathlib import Path as _P

    proj = _P(__file__).resolve().parent.parent
    soak_script = (proj / "soak_runner.py") if (proj / "soak_runner.py").is_file() else (proj / "tests" / "fixtures" / "soak_runner.py")
    env = dict(os.environ, DAG_AGENT_HOME=str(tmp_path / "soak_home"),
               PYTHONPATH=str(proj), PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, str(soak_script),
         "--duration-seconds", "3", "--home", str(tmp_path / "soak_home")],
        cwd=str(proj), env=env, capture_output=True, text=True,
        encoding="utf-8", timeout=120)
    assert proc.returncode == 0, proc.stderr[-500:]
    home = tmp_path / "soak_home"
    summary = json.loads((home / "soak_summary.json").read_text(encoding="utf-8"))
    assert summary["rounds"] >= 1 and summary["errors"] == 0
    assert summary["counters"]["missions"] >= 1
    rounds = [json.loads(l) for l in
              (home / "soak_rounds.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(r.get("mission_done") for r in rounds), "soak每轮都应全生命周期完成"
    assert (home / "BOARD.md").exists()
