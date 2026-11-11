"""步骤1验收：核心状态机——归属/执行依赖/事件一致性/整合闭合/并发短事务。

对应任务清单第一步通过标准：不提前完成、不绕过依赖；状态与事件一致。
"""
from __future__ import annotations

import threading

import pytest

from dag_agent.graph import GraphError


def test_create_mission_creates_root(graph):
    mid, root = graph.create_mission("演示", "验收", root_check="python -c 'print(1)'")
    node = graph.repo.get_node(root)
    assert node.kind == "aggregate" and node.status == "pending"
    assert node.check_cmd == "python -c 'print(1)'"
    events = graph.repo.events_of(mid)
    assert events[0].type == "create" and events[0].node_id == root


def test_add_node_belonging_and_deps(graph):
    mid, _root = graph.create_mission("归属")
    a = graph.add_node(mid, "调查", statement="收集资料成初稿", parent_id=_root)
    b = graph.add_node(mid, "实验", statement="跑实验记录数据", parent_id=_root)
    c = graph.add_node(mid, "整合报告", statement="汇总成报告", parent_id=_root, deps=(a, b))
    assert graph.repo.get_node(a).parent_id == _root
    assert {d.depends_on for d in graph.repo.deps_of(c)} == {a, b}
    # 归属不是依赖：A/B 的 deps 为空，不等待父节点
    assert graph.repo.deps_of(a) == [] and graph.repo.deps_of(b) == []


def test_add_node_validations(graph):
    mid, root = graph.create_mission("校验")
    with pytest.raises(GraphError, match="父节点不存在"):
        graph.add_node(mid, "x", statement="探测错误路径", parent_id="n999")
    a = graph.add_node(mid, "a", statement="基线节点甲", parent_id=root)
    with pytest.raises(GraphError, match="死锁"):
        graph.add_node(mid, "c", statement="死锁探针", parent_id=a, deps=(a,))
    with pytest.raises(GraphError, match="依赖节点不存在"):
        graph.add_node(mid, "d", statement="悬空依赖探针", parent_id=root, deps=("n999",))


def test_dependency_gate_blocks_start(graph):
    mid, root = graph.create_mission("依赖门")
    a = graph.add_node(mid, "上游", parent_id=root, check_cmd="python -c 'pass'")
    b = graph.add_node(mid, "下游", parent_id=root, deps=(a,), check_cmd="python -c 'pass'")
    with pytest.raises(GraphError, match="依赖未满足"):
        graph.start_attempt(mid, b)
    # 上游走完 → 下游可开始
    aid = graph.start_attempt(mid, a)
    graph.submit_attempt(mid, a, aid)
    assert graph.repo.get_node(a).status == "accepted"
    assert b in graph.frontier(mid)


def test_fail_closed_without_check_cmd(graph):
    mid, root = graph.create_mission("fail-closed")
    a = graph.add_node(mid, "无检查", statement="故意不配验收", parent_id=root)
    aid = graph.start_attempt(mid, a)
    result = graph.submit_attempt(mid, a, aid)
    assert result["verdict"] == "fail"
    assert graph.repo.get_node(a).status == "failed"
    assert "fail-closed" in result["evidence"]


def test_normal_lifecycle_events_consistent(graph):
    mid, root = graph.create_mission("事件一致")
    a = graph.add_node(mid, "任务",
                   statement="【输入】无/【交付清单】无/【接口签名】无/【样例】无/【自测入口】python -c 'pass'",
                   parent_id=root, check_cmd="python -c 'pass'")
    aid = graph.start_attempt(mid, a)
    graph.submit_attempt(mid, a, aid)
    types = [e.type for e in graph.repo.events_of(mid)]
    # create → add_node → node_to_waiting(挂孩子即转分解态，步骤4语义) 
    # → attempt_started → submitted → done，顺序固定
    assert types == ["create", "add_node", "node_to_waiting",
                     "attempt_started", "submitted", "done", "integration_ready"]
    node = graph.repo.get_node(a)
    assert node.status == "accepted"
    arts = graph.repo.artifacts_of(a)
    assert arts == []  # 未登记工件文件时无 artifact 行
    # 状态与事件一致：accepted 节点必有 done 事件
    done_events = [e for e in graph.repo.events_of(mid)
                   if e.type == "done" and e.node_id == a]
    assert len(done_events) == 1


def test_premature_completion_blocked(graph):
    mid, root = graph.create_mission("不提前完成")
    a = graph.decompose(mid, root, [{"title": "A", "statement": "子任务甲", "check_cmd": "python -c 'pass'"}])[0]
    # 错误路径：孩子没完成就想整合根节点
    with pytest.raises(GraphError, match="整合尝试未解锁"):
        graph.start_attempt(mid, root)
    # 正常路径：孩子完成 → 解锁整合
    aid = graph.start_attempt(mid, a)
    graph.submit_attempt(mid, a, aid)
    assert root in graph.integration_ready(mid)


def test_integration_closes_parent_and_mission(graph):
    mid, root = graph.create_mission("整合闭合", root_check="python -c 'pass'")
    a = graph.decompose(mid, root, [{"title": "唯一子", "statement": "独子陈述", "check_cmd": "python -c 'pass'"}])[0]
    aid = graph.start_attempt(mid, a)
    graph.submit_attempt(mid, a, aid)
    assert graph.repo.get_node(a).status == "accepted"
    assert graph.repo.get_node(root).status == "waiting_children"  # 未提前完成
    events = [e.type for e in graph.repo.events_of(mid)]
    assert "integration_ready" in events
    iaid = graph.start_attempt(mid, root)  # 整合尝试
    graph.submit_attempt(mid, root, iaid)
    assert graph.repo.get_node(root).status == "accepted"
    assert graph.repo.get_mission(mid).status == "done"
    assert "mission_done" in [e.type for e in graph.repo.events_of(mid)]


def test_decompose_preserves_parent_deps(graph):
    mid, root = graph.create_mission("依赖保留")
    x = graph.add_node(mid, "前置X", parent_id=root, check_cmd="python -c 'pass'")
    p = graph.add_node(mid, "父P", parent_id=root, deps=(x,), check_cmd="python -c 'pass'")
    graph.decompose(mid, p, [{"title": "P1", "statement": "P的第一半", "check_cmd": "python -c 'pass'"}])
    assert [d.depends_on for d in graph.repo.deps_of(p)] == [x]  # 分解不清空原依赖
    k = graph.children_of(p)[0]
    kid_aid = graph.start_attempt(mid, k.id)
    graph.submit_attempt(mid, k.id, kid_aid)
    # 孩子全完成但父自己的依赖 X 未完成 → 整合尝试仍被依赖门拦住
    assert p in graph.integration_ready(p and mid)
    with pytest.raises(GraphError, match="依赖未满足"):
        graph.start_attempt(mid, p)
    xaid = graph.start_attempt(mid, x)
    graph.submit_attempt(mid, x, xaid)
    assert p not in graph.frontier(mid)  # waiting_children 不在普通前沿
    iaid = graph.start_attempt(mid, p)  # 现在依赖满足，整合可开始
    graph.submit_attempt(mid, p, iaid)


def test_block_unblock_flow(graph):
    mid, root = graph.create_mission("阻塞")
    a = graph.add_node(mid, "A", parent_id=root, check_cmd="python -c 'pass'")
    graph.block(mid, a, "等外部输入")
    with pytest.raises(GraphError, match="blocked"):
        graph.start_attempt(mid, a)
    graph.unblock(mid, a)
    assert graph.repo.get_node(a).status == "pending"


def test_goal_version_stale_guard(graph):
    mid, root = graph.create_mission("目标版本")
    a = graph.add_node(mid, "A", parent_id=root, check_cmd="python -c 'pass'")
    aid = graph.start_attempt(mid, a)
    graph.change_goal(mid, "目标变了")
    with pytest.raises(GraphError, match="目标版本已变更"):
        graph.submit_attempt(mid, a, aid)


def test_failed_node_can_restart(graph):
    mid, root = graph.create_mission("重启")
    a = graph.add_node(mid, "A", parent_id=root, check_cmd="echo FAIL")
    aid = graph.start_attempt(mid, a)
    result = graph.submit_attempt(mid, a, aid)
    assert result["verdict"] == "fail"
    aid2 = graph.start_attempt(mid, a)  # failed 可重新开始
    assert aid2 != aid
    graph.repo.update_attempt(aid2, resources_json='{"integration": false, "goal_version": 1}')
    # 换成可通过的检查（节点 check_cmd 锁定，这里验证失败→重启链路本身）
    result2 = graph.submit_attempt(mid, a, aid2, )
    assert result2["verdict"] == "fail"  # check_cmd 仍锁定为 FAIL——不许现场改检查


def test_artifact_path_escape_rejected(graph):
    mid, root = graph.create_mission("工件越界")
    a = graph.add_node(mid, "A", parent_id=root, check_cmd="python -c 'pass'")
    aid = graph.start_attempt(mid, a)
    with pytest.raises(GraphError, match="越界|不存在"):
        graph.submit_attempt(mid, a, aid, artifact_paths=["../../secret.txt"])


def test_artifact_recorded_with_version(graph):
    mid, root = graph.create_mission("工件版本")
    a = graph.add_node(mid, "A", parent_id=root, check_cmd="python -c 'pass'")
    aid = graph.start_attempt(mid, a)
    from pathlib import Path
    workdir = Path(graph.repo.get_attempt(aid).workdir)
    (workdir / "out.txt").write_text("成果", encoding="utf-8")
    graph.submit_attempt(mid, a, aid, artifact_paths=["out.txt"])
    arts = graph.repo.artifacts_of(a)
    assert len(arts) == 1 and arts[0].version == 1
    assert arts[0].content_hash


def test_action_receipt_idempotent(graph):
    assert graph.repo.receipt_first("op:1", '{"ok": true}') is True
    assert graph.repo.receipt_first("op:1", '{"ok": true}') is False
    assert graph.repo.receipt_get("op:1") == {"ok": True}


def test_concurrent_start_attempt_single_winner(graph):
    mid, root = graph.create_mission("并发认领")
    a = graph.add_node(mid, "A", parent_id=root, check_cmd="python -c 'pass'")
    wins, errors = [], []

    def worker():
        try:
            aid = graph.start_attempt(mid, a)
            wins.append(aid)
        except GraphError as e:
            errors.append(str(e))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1, f"应只有一个认领成功，实际{len(wins)}"
    assert len(errors) == 5
    import sqlite3
    with graph.repo.tx() as cur:
        n = cur.execute(
            "SELECT COUNT(*) AS c FROM attempts WHERE node_id=?", (a,)).fetchone()
    assert n["c"] == 1


def test_concurrent_add_nodes_no_loss(graph):
    mid, root = graph.create_mission("并发建节点")
    ids, lock = [], threading.Lock()

    def worker(i):
        nid = graph.add_node(mid, f"节点{i}", statement=f"并发建点{i}", parent_id=root)
        with lock:
            ids.append(nid)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(ids)) == 8  # id 唯一，无丢失无冲突
    assert len(graph.repo.list_nodes(mid)) == 9  # root + 8


def test_dep_cycle_between_existing_nodes_rejected():
    """兄弟节点互为依赖的环：通过 decompose 后追加依赖的场景被结构阻止（deps 仅创建时设定）。"""
    from dag_agent.repository import Repository
    from dag_agent.graph import Graph as G
    import tempfile, os
    td = tempfile.mkdtemp()
    try:
        repo = Repository(os.path.join(td, "m.db"))
        g = G(repo, checker=lambda cmd, cwd: (True, "ok"))
        mid, root = g.create_mission("环")
        a = g.add_node(mid, "A", statement="环测试A", parent_id=root)
        b = g.add_node(mid, "B", statement="环测试B依赖A", parent_id=root, deps=(a,))
        # B 依赖 A 成立；A 无法再依赖 B（API 不提供事后加依赖）——结构上杜绝环
        assert [d.depends_on for d in g.repo.deps_of(b)] == [a]
        assert g.repo.deps_of(a) == []
        repo.close()
    finally:
        import shutil
        shutil.rmtree(td, ignore_errors=True)
