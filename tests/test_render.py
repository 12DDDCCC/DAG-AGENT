"""test_render.py — Obsidian看板渲染：mermaid/状态着色/前沿/事件日志。"""
from __future__ import annotations

from dag_agent.store import MissionStore


def test_board_contains_mermaid_and_status(tmp_path):
    store = MissionStore(home=tmp_path)
    store.create_mission("b1", "看板任务", "简报", "交付", "总陈述")
    store.decompose("b1", "n0", [{"title": "子甲", "statement": "s"}])
    store.block("b1", "n1", "等数据授权")
    board = store.board_path("b1").read_text(encoding="utf-8")
    assert "```mermaid" in board and "graph TD" in board
    assert "n0 --> n1" in board
    assert "classDef stDone" in board and "class n1 stBlocked" in board
    assert "🚧" in board and "等数据授权" in board
    assert "无前沿" in board, "全阻塞时应提示人类决策"
    assert '"type": "block"' in board or "'type': 'block'" in board


def test_board_updates_on_submit(tmp_path):
    store = MissionStore(home=tmp_path)
    store.create_mission("b2", "看板任务2", "简报", "交付", "总陈述")
    before = store.board_path("b2").read_text(encoding="utf-8")
    assert "✅" not in before.split("节点明细")[0].split("```mermaid")[1]
    store.submit("b2", "n0", "直接完成")
    after = store.board_path("b2").read_text(encoding="utf-8")
    assert "✅ n0 交付" in after
    assert "任务状态：🎉 已完成" in after
