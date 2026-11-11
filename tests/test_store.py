"""test_store.py — 状态机单元测试：创建/前沿/分解级联/阻塞/错误→动作。"""
from __future__ import annotations

import pytest

from dag_agent.store import DagError, MissionStore


@pytest.fixture()
def store(tmp_path):
    return MissionStore(home=tmp_path)


def _make(store, mid="m1"):
    return store.create_mission(mid, "示例", "简报", "交付", "总陈述")


def test_create_and_status(store):
    st = _make(store)
    assert st["total"] == 1 and st["counts"]["open"] == 1
    assert st["root"] == "n0" and not st["root_done"]
    assert (store.board_path("m1")).exists(), "创建即出看板"


def test_duplicate_create_rejected(store):
    _make(store)
    with pytest.raises(DagError, match="已存在"):
        store.create_mission("m1", "x", "y", "z", "w")


def test_decompose_frontier_and_auto_close(store):
    _make(store)
    r = store.decompose("m1", "n0", [
        {"title": "a", "statement": "sa"},
        {"title": "b", "statement": "sb"},
    ])
    assert r["children"] == ["n1", "n2"]
    # 分解后父节点仍是open（等children），前沿只有children
    fr = store.frontier("m1")
    assert [x["id"] for x in fr] == ["n1", "n2"]
    # 依次提交children（无checks→凭陈述通过）
    store.submit("m1", "n1", "a完成")
    res = store.submit("m1", "n2", "b完成")
    # 依赖闭合：n0 aggregate自动done，任务完成
    st = res["status"]
    assert st["root_done"] and st["counts"]["done"] == 3
    n0 = store.load("m1")["nodes"]["n0"]
    assert n0["auto_resolved"]


def test_diamond_dependency_cascade(store):
    """菱形依赖：n0(aggregate)→children n1,n2；n3(task)依赖n1,n2但在根闭合之外。
    n1,n2 done即闭合n0（Prove2Me语义：父由children构成，延伸任务不闸门根交付）。"""
    _make(store)
    store.decompose("m1", "n0", [
        {"title": "a", "statement": "sa"}, {"title": "b", "statement": "sb"}])
    store.add_node("m1", "c", "sc", deps=["n1", "n2"])
    assert [x["id"] for x in store.frontier("m1")] == ["n1", "n2"]
    store.submit("m1", "n1", "ok")
    assert [x["id"] for x in store.frontier("m1")] == ["n2"]
    res2 = store.submit("m1", "n2", "ok")
    assert res2["auto_closed"] == ["n0"], "children n1,n2齐→根自动闭合"
    assert res2["status"]["root_done"]
    assert [x["id"] for x in res2["status"]["frontier"]] == ["n3"]
    res3 = store.submit("m1", "n3", "ok")
    assert res3["auto_closed"] == []


def test_block_excludes_and_unblock_restores(store):
    _make(store)
    store.decompose("m1", "n0", [{"title": "a", "statement": "sa"}])
    store.block("m1", "n1", "等外部key")
    assert store.frontier("m1") == []
    assert store.status("m1")["counts"]["blocked"] == 1
    board = store.board_path("m1").read_text(encoding="utf-8")
    assert "🚧" in board and "等外部key" in board
    store.unblock("m1", "n1")
    assert [x["id"] for x in store.frontier("m1")] == ["n1"]


def test_submit_errors_point_to_action(store):
    _make(store)
    with pytest.raises(DagError, match="不存在"):
        store.submit("m1", "n99", "x")


def test_aggregate_cannot_be_manually_submitted(store):
    """aggregate节点的完成只能来自依赖闭合——堵住'提前交付'漏洞。"""
    _make(store)
    store.decompose("m1", "n0", [{"title": "a", "statement": "sa"}])
    with pytest.raises(DagError, match="aggregate"):
        store.submit("m1", "n0", "提前交付")
    store.submit("m1", "n1", "子任务完成")
    assert store.status("m1")["mission_done"]


def test_submit_fail_keeps_open_and_records_attempts(store):
    _make(store)
    store.add_node("m1", "带检查", "陈述", deps=[],
                   checks=[{"name": "fail_check",
                            "command": ["python", "check_fail.py"]}])
    (store.mission_dir("m1") / "check_fail.py").write_text(
        "import sys; print('证据不足'); sys.exit(1)", encoding="utf-8")
    r = store.submit("m1", "n1", "尝试1")
    assert r["verdict"] == "FAIL" and "证据不足" in r["evidence"]
    r2 = store.submit("m1", "n1", "尝试2")
    assert r2["verdict"] == "FAIL"
    n1 = store.load("m1")["nodes"]["n1"]
    assert n1["status"] == "open" and n1["result"]["attempts"] == 2


def test_events_append_only(store):
    _make(store)
    store.add_node("m1", "x", "y", deps=[])
    ev = store.read_events("m1")
    assert [e["type"] for e in ev] == ["create", "add_node"]


def test_illegal_check_command_rejected(store):
    _make(store)
    with pytest.raises(DagError, match="python/pytest"):
        store.add_node("m1", "x", "y", deps=[],
                       checks=[{"name": "bad", "command": ["node", "x.js"]}])


def test_unknown_dependency_points_to_action(store):
    _make(store)
    with pytest.raises(DagError, match="先 add_node"):
        store.add_node("m1", "x", "y", deps=["n7"])
