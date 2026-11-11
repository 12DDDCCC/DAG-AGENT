"""test_review_fixes.py — 代码评审修复项的回归测试（卡死路径/重试/门规/健壮性）。"""
from __future__ import annotations

import json

import pytest

from dag_agent.store import DagError, MissionStore


@pytest.fixture()
def store(tmp_path):
    return MissionStore(home=tmp_path)


def test_unblock_triggers_cascade(store):
    """评审问题1：block的aggregate，children在阻塞期间全done，unblock应级联闭合。"""
    store.create_mission("m1", "t", "b", "交付", "完成")
    store.decompose("m1", "n0", [{"title": "a", "statement": "sa"}])
    store.block("m1", "n0", "等人类决策")
    store.submit("m1", "n1", "子任务完成")   # n0 blocked时不级联
    assert store.status("m1")["root_done"] is False
    r = store.unblock("m1", "n0")
    assert r["auto_closed"] == ["n0"]
    assert store.status("m1")["mission_done"]


def test_aggregate_check_fail_then_retry(store, tmp_path):
    """评审问题2：组合检查失败→auto_fail落盘→看板提示→retry闭合。"""
    store.create_mission("m1", "t", "b", "交付", "完成")
    (store.mission_dir("m1") / "check_combo.py").write_text(
        "from pathlib import Path\n"
        "t = Path('workspace/out.txt').read_text(encoding='utf-8')\n"
        "assert 'FINAL' in t, '缺FINAL'\nprint('combo ok')", encoding="utf-8")
    store.add_node("m1", "汇总", "组合验收", deps=[],
                   checks=[{"name": "combo", "command": ["python", "check_combo.py"]}],
                   kind="aggregate")
    store.decompose("m1", "n1", [{"title": "产出", "statement": "写out"}])
    (store.mission_dir("m1") / "workspace" / "out.txt").write_text("草稿", encoding="utf-8")
    store.submit("m1", "n2", "写完")
    # children(n2)全done → 组合检查失败 → auto_fail落盘、节点仍open
    n1 = store.load("m1")["nodes"]["n1"]
    assert n1["status"] == "open" and n1["result"]["attempts"] == 1
    store.block("m1", "n0", "暂缓")   # 根暂缓→frontier清空，看板提示重试闭合
    board = store.board_path("m1").read_text(encoding="utf-8")
    assert "dag_retry" in board, "看板应提示重试闭合"
    # 修复产物 → retry → 闭合
    (store.mission_dir("m1") / "workspace" / "out.txt").write_text("FINAL", encoding="utf-8")
    r = store.retry("m1", "n1")
    assert r["auto_closed"] == ["n1"]
    assert store.status("m1")["root_done"] is False  # 根n0没有children，不自动闭合
    with pytest.raises(DagError, match="已done"):
        store.submit("m1", "n1", "手工提交")   # 提交不可变优先于aggregate规则


def test_core_gate_rules(store):
    """评审问题10：依赖未完成拒绝/done后不可变/重复unblock/decompose闭合节点。"""
    store.create_mission("m1", "t", "b", "交付", "完成")
    store.add_node("m1", "a", "sa", deps=["n0"])
    with pytest.raises(DagError, match="依赖未完成"):
        store.submit("m1", "n1", "跳步提交")
    store.submit("m1", "n0", "根直接交付")     # 无children的根是task，可直接交付
    with pytest.raises(DagError, match="已done"):
        store.submit("m1", "n0", "重复提交")
    with pytest.raises(DagError, match="无需解锁"):
        store.unblock("m1", "n0")
    with pytest.raises(DagError, match="无需重试闭合"):
        store.retry("m1", "n0")


def test_optimistic_lock_on_concurrent_save(store):
    """评审问题3：rev乐观锁——旧状态写回被拒，已验证结果不丢。"""
    store.create_mission("m1", "t", "b", "交付", "完成")
    stale = store.load("m1")            # 会话A拿到的旧状态
    store.submit("m1", "n0", "会话B先完成")   # 状态被B推进
    with pytest.raises(DagError, match="并发冲突"):
        store._save("m1", stale)         # A用旧rev写回 → 拒绝
    fresh = store.load("m1")
    assert fresh["nodes"]["n0"]["status"] == "done", "B的结果不丢失"


def test_mid_whitelist(store):
    """评审问题5：路径穿越/Windows保留名/非法字符全部拒绝。"""
    for bad in ("../evil", "a:b", "CON", "nul", "a b", "", "x" * 65, "a.b"):
        with pytest.raises(DagError, match="非法任务ID"):
            store.create_mission(bad, "t", "b", "r", "s")


def test_events_tail_zero_and_partial_line(store):
    """评审问题8：tail=0返回空；半行日志跳过不崩。"""
    store.create_mission("m1", "t", "b", "交付", "完成")
    assert store.read_events("m1", tail=0) == []
    p = store.mission_dir("m1") / "events.jsonl"
    with p.open("a", encoding="utf-8") as f:
        f.write('{"at": "x", "type": "half")')   # 故意坏行
    ev = store.read_events("m1", tail=5)
    assert all(isinstance(e, dict) for e in ev)
