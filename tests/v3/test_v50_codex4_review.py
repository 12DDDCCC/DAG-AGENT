# -*- coding: utf-8 -*-
"""v50：Codex 四轮独立验收（2026-10-12）N1–N6 的正式回归。

对应 outputs/DAG-agent-第四轮独立验收-2026-10-12.md §4：
- N1 维修"体检→冻结"漂移窗口：STRICT 体检通过后换 GREEN → 维修拒绝
  （前后双摘要）；发布前（事务内）漂移同样拒绝。
- N2 维修 vs 终态竞态：体检期间原提交闭合（节点 accepted/任务 done）→
  维修被拒、原机器证据保持有效；新考卷不给 accepted 节点改卷。
- N3 普通节点播种按认领账本核验：联合改写/清空清单均拒（普通节点无
  dep_bindings 也拦）；正例播种内容逐字节正确；根重放同规则。
- N4 旧库迁移回填：健康旧绑定（旧 v47 表形态）迁移后可认领；
  不一致旧快照不自动"修成合法"（保持拒绝）。
- N5 恢复配置持久化：超过内存归档上限被淘汰的未终态任务，配置仍可
  从任务库 run_control.params_json 回退（kick 不再报"无历史运行参数"）；
  B3：带不可重建组件标识（backend_factory）的配置由组件守卫显式失败。
- N6 停止代际绑定：旧 stop 的退出复查不取消新代际尝试；同代际对照下
  退出复查照常清理（不是"一律跳过清理"的假绿）。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dag_agent.artifacts import manifest_hash_of  # noqa: E402
from dag_agent.graph import Graph, GraphError  # noqa: E402
from dag_agent.repository import Repository  # noqa: E402
from dag_agent.scheduler import AutogrowConfig, Scheduler  # noqa: E402

from test_v45_autogrow import (  # noqa: E402
    BRIEF, CMD_OK, AutoGrowBackend, build_mission)

STRICT = ("from pathlib import Path\nimport sys\n"
          "p=Path('deliverable.md')\n"
          "sys.exit(0 if p.is_file() and p.read_text(encoding='utf-8')"
          "=='GOOD' else 1)\n")
GREEN = "import sys\nsys.exit(0)\n"
UPSTREAM_PAYLOAD = "ORIGINAL upstream payload for v50 tests"


def _accept_upstream(repo, graph, mid, up, payload=UPSTREAM_PAYLOAD) -> int:
    aid = graph.start_attempt(mid, up, backend="stub")
    wd = Path(repo.get_attempt(aid).workdir)
    (wd / "deliverable.md").write_text(payload, encoding="utf-8")
    result = graph.submit_attempt(mid, up, aid,
                                  artifact_paths=["deliverable.md"])
    assert result["verdict"] == "pass"
    return repo._conn.execute(
        "SELECT id FROM snapshots WHERE node_id=? AND valid=1"
        " ORDER BY id DESC LIMIT 1", (up,)).fetchone()["id"]


# ---------- N1 · 维修体检→冻结漂移 ----------


def test_n1_repair_preflight_window_rejected(tmp_path, monkeypatch):
    """STRICT 体检通过、返回前换 GREEN——修复后体检前算摘要①、体检后
    算摘要②，漂移拒绝；不改卷不发布半更新。"""
    import dag_agent.acceptance as acceptance
    repo, graph, mid, root = build_mission(tmp_path)
    exam = tmp_path / "n1_exam.py"
    exam.write_text(STRICT, encoding="utf-8")
    new_cmd = f'python "{exam}"'
    hit = {"n": 0}
    orig_pf = acceptance.preflight

    def intercept(*args, **kwargs):
        result = orig_pf(*args, **kwargs)
        if result[0] and hit["n"] == 0:
            hit["n"] = 1
            exam.write_text(GREEN, encoding="utf-8")   # 窗口正中换卷
        return result

    monkeypatch.setattr(acceptance, "preflight", intercept)
    with pytest.raises(GraphError, match="体检期间漂移"):
        graph.repair_check(mid, root, new_check_cmd=new_cmd,
                           reason="n1", exam_files=[str(exam)])
    assert hit["n"] == 1, "探针未命中（测试自身失效）"
    node = repo.get_node(root)
    assert node.check_version == 1, "无半更新：版本不变"
    repo.close()


def test_n1b_repair_publish_window_rejected(tmp_path):
    """发布前（事务内）漂移：冻结后改卷——digest3 重算≠digest2 拒绝。
    （以确定的"冻结后改卷"手法覆盖事务内重核分支。）"""
    repo, graph, mid, root = build_mission(tmp_path)
    exam = tmp_path / "n1b_exam.py"
    exam.write_text(STRICT, encoding="utf-8")
    new_cmd = f'python "{exam}"'
    # 手动复现"发布前漂移"：直接在事务外算好双摘要一致后改卷再调
    # repair——repair 内部 digest1/2 相同（都读 GREEN），但为了覆盖
    # digest3≠digest2 分支，用 monkeypatch 让第二次摘要计算返回旧值
    import dag_agent.acceptance as acceptance
    orig_ccd = acceptance.compute_check_digest
    calls = {"n": 0}

    def fake_ccd(**kwargs):
        calls["n"] += 1
        # digest1/digest2（外层两次）返回真值（STRICT 可过体检）；
        # digest3（事务内第三次）返回不同假值 → 发布前漂移分支
        if calls["n"] >= 3:
            return "0" * 64
        return orig_ccd(**kwargs)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(acceptance, "compute_check_digest", fake_ccd)
    try:
        with pytest.raises(GraphError, match="发布前漂移"):
            graph.repair_check(mid, root, new_check_cmd=new_cmd,
                               reason="n1b", exam_files=[str(exam)])
    finally:
        monkey.undo()
    assert repo.get_node(root).check_version == 1
    repo.close()


# ---------- N2 · 维修 vs 终态竞态 ----------


def test_n2_repair_after_terminal_close_rejected(tmp_path, monkeypatch):
    """体检期间原提交闭合（节点 accepted、任务 done/machine）→ 维修
    事务内重核终态拒绝；v1 机器证据保持 valid；不改卷。"""
    import dag_agent.acceptance as acceptance
    repo, graph, mid, root = build_mission(tmp_path)
    exam = tmp_path / "n2_exam.py"
    exam.write_text(STRICT, encoding="utf-8")
    new_cmd = f'python "{exam}"'
    orig_pf = acceptance.preflight
    state = {"done": False}

    def intercept(*args, **kwargs):
        result = orig_pf(*args, **kwargs)
        # 体检通过后、维修事务前：原 attempt 正常提交→根 accepted→任务闭合
        if result[0] and not state["done"]:
            state["done"] = True
            aid = graph.start_attempt(mid, root, backend="stub")
            wd = Path(repo.get_attempt(aid).workdir)
            (wd / "deliverable.md").write_text("x" * 60, encoding="utf-8")
            v = graph.submit_attempt(mid, root, aid,
                                     artifact_paths=["deliverable.md"])
            assert v["verdict"] == "pass"
        return result

    monkeypatch.setattr(acceptance, "preflight", intercept)
    with pytest.raises(GraphError, match="终态|闭合"):
        graph.repair_check(mid, root, new_check_cmd=new_cmd,
                           reason="n2", exam_files=[str(exam)])
    node = repo.get_node(root)
    assert node.status == "accepted"
    assert node.check_version == 1, "不改卷：版本不变"
    assert repo.get_mission(mid).status == "done"
    assert repo.get_mission(mid).closure_basis == "machine"
    ev = repo._conn.execute(
        "SELECT COUNT(*) AS c FROM assessments WHERE node_id=? AND"
        " authoritative=1 AND invalid=0 AND verdict='pass'",
        (root,)).fetchone()["c"]
    assert ev == 1, "唯一机器证据保持有效（不作废）"
    repo.close()


# ---------- N3 · 普通节点播种按认领账本核验 ----------


def _leaf_with_dep(tmp_path):
    """根+上游(accepted)+普通依赖叶子（无 dep_bindings）认领态。"""
    repo, graph, mid, root = build_mission(tmp_path)
    up = graph.add_node(mid, title="上游", statement=BRIEF.format(body="u"),
                        parent_id=root, check_cmd=CMD_OK,
                        declared_artifacts=("deliverable.md",))
    leaf = graph.add_node(mid, title="普通叶子",
                          statement=BRIEF.format(body="leaf"),
                          parent_id=root, deps=(up,), check_cmd=CMD_OK,
                          declared_artifacts=("deliverable.md",))
    sid = _accept_upstream(repo, graph, mid, up)
    la = graph.start_attempt(mid, leaf, backend="stub")
    binding = repo.get_attempt(la).input_binding_json
    ctr = repo._conn.execute(
        "SELECT COUNT(*) AS c FROM dep_bindings WHERE node_id=?",
        (leaf,)).fetchone()["c"]
    assert ctr == 0, "前置：普通节点无 dep_bindings"
    return repo, graph, mid, root, up, leaf, sid, binding, la


def test_n3_generic_metadata_drift_rejected(tmp_path):
    """普通节点认领后：同 ID、同文件名、files_json sha/size 与
    manifest_hash 字段联合改写＋accepted 文件同步改 → 播种拒绝
    （账本 hash 独立于 snapshots 表）。"""
    repo, graph, mid, root, up, leaf, sid, binding, la = \
        _leaf_with_dep(tmp_path)
    orig = repo._conn.execute(
        "SELECT files_json FROM snapshots WHERE id=?", (sid,)).fetchone()
    tampered = json.loads(orig["files_json"])
    tampered[0]["sha256"] = "0" * 64
    tampered[0]["size"] = 3
    with repo.tx() as cur:
        cur.execute(
            "UPDATE snapshots SET files_json=?, manifest_hash=? WHERE id=?",
            (json.dumps(tampered), manifest_hash_of(tampered), sid))
    sched = Scheduler(repo, graph, AutoGrowBackend())
    cw = Path(repo.get_attempt(la).workdir)
    with pytest.raises(ValueError, match="认领账本 manifest 不符"):
        sched._seed_bound(la, cw)
    assert not list(cw.rglob("deliverable.md")), "不得播种篡改内容"
    repo.close()


def test_n3_generic_empty_list_rejected(tmp_path):
    """普通节点认领后清空 files_json → 播种拒绝（空清单≠合法无输入）；
    正例恢复后播种内容逐字节正确。"""
    repo, graph, mid, root, up, leaf, sid, binding, la = \
        _leaf_with_dep(tmp_path)
    orig = repo._conn.execute(
        "SELECT files_json FROM snapshots WHERE id=?", (sid,)).fetchone()
    with repo.tx() as cur:
        cur.execute("UPDATE snapshots SET files_json='[]' WHERE id=?", (sid,))
    sched = Scheduler(repo, graph, AutoGrowBackend())
    cw = Path(repo.get_attempt(la).workdir)
    with pytest.raises(ValueError, match="认领账本 manifest 不符|空清单"):
        sched._seed_bound(la, cw)
    assert not list(cw.rglob("deliverable.md")), "不静默交付空目录"
    with repo.tx() as cur:
        cur.execute("UPDATE snapshots SET files_json=? WHERE id=?",
                    (orig["files_json"], sid))
    sched._seed_bound(la, cw)
    seeded = list(cw.rglob("deliverable.md"))
    assert seeded and seeded[0].read_text(encoding="utf-8") == UPSTREAM_PAYLOAD
    repo.close()


def test_n3_replay_uses_ledger_rule(tmp_path):
    """根干净重放同规则：绑定账本 manifest 与快照重算失配 → 重放
    error（根不闭合）。"""
    repo, graph, mid, root = build_mission(tmp_path)
    up = graph.add_node(mid, title="上游", statement=BRIEF.format(body="u"),
                        parent_id=root, check_cmd=CMD_OK,
                        declared_artifacts=("deliverable.md",))
    sid = _accept_upstream(repo, graph, mid, up)
    # 构造与认领账本同构的绑定，但快照清单已清空（重算≠账本）
    binding = [{"source": up, "snapshot_id": sid,
                "manifest_hash": "b" * 64, "files": ["deliverable.md"],
                "artifact_version": 1, "role": "dep"}]
    with repo.tx() as cur:
        cur.execute("UPDATE snapshots SET files_json='[]' WHERE id=?", (sid,))
    cand = tmp_path / "cand"
    cand.mkdir()
    rv, ev = graph._replay_fresh(mid, root, cand, binding)
    assert rv == "error" and ("账本" in ev or "失效" in ev), (rv, ev[:120])
    repo.close()


# ---------- N4 · 旧库迁移回填 ----------


def test_n4_migration_backfills_healthy_old_bindings(tmp_path):
    """按 v47 旧表形态构造库（dep_bindings 的 files_meta_json 抹空＋
    一行坏契约），新 Repository 迁移重开后：健康旧绑定自动回填、可
    认领；不一致旧快照不回填（认领保持拒绝）。"""
    from test_v49_codex3_review import _apply_two_children
    home = tmp_path / "mig"
    home.mkdir()
    db = home / "mission.db"
    repo, graph, mid, root = build_mission(home)
    up = graph.add_node(mid, title="上游", statement=BRIEF.format(body="u"),
                        parent_id=root, check_cmd=CMD_OK,
                        declared_artifacts=("deliverable.md",))
    parent = graph.add_node(mid, title="父任务P",
                            statement=BRIEF.format(body="P"),
                            parent_id=root, deps=(up,), check_cmd=CMD_OK,
                            declared_artifacts=("deliverable.md",))
    sid = _accept_upstream(repo, graph, mid, up)
    pa = graph.start_attempt(mid, parent, backend="stub")
    binding = repo.get_attempt(pa).input_binding_json
    fp = repo.get_attempt(pa).claim_fingerprint_json
    kids, _ = _apply_two_children(repo, graph, mid, parent, "sp-n4",
                                  binding, fp)
    repo.close()
    # 模拟旧形态：files_meta_json 抹空 + 一行不一致坏契约
    import sqlite3
    c = sqlite3.connect(db)
    c.execute("UPDATE dep_bindings SET files_meta_json='[]'")
    c.execute(
        "INSERT INTO dep_bindings(node_id,depends_on,snapshot_id,"
        "manifest_hash,files_json,artifact_version,files_meta_json)"
        " VALUES(?,?,?,?,?,?, '[]')",
        ("n-stale-x", up, sid, "deadbeef", json.dumps(
            [{"rel_path": "deliverable.md", "sha256": "f" * 64,
              "size": 1}]), 1))
    c.commit()
    c.close()
    # 新 Repository 重开（迁移+回填）
    repo2 = Repository(db)
    graph2 = Graph(repo2)
    backfilled = repo2._conn.execute(
        "SELECT files_meta_json FROM dep_bindings WHERE node_id=?",
        (kids[0],)).fetchone()["files_meta_json"]
    assert json.loads(backfilled), "健康旧绑定必须回填完整元信息"
    stale = repo2._conn.execute(
        "SELECT files_meta_json FROM dep_bindings WHERE node_id=?",
        ("n-stale-x",)).fetchone()["files_meta_json"]
    assert stale == "[]", "不一致旧快照不自动回填（fail-closed）"
    ca = graph2.start_attempt(mid, kids[0], backend="stub")
    assert repo2.get_attempt(ca).status == "running"
    repo2.close()


# ---------- N5 · 恢复配置持久化（归档淘汰后可 kick） ----------


def test_n5_kick_survives_archive_eviction(tmp_path):
    """未终态任务归档后，内存归档被清空 → kick 仍从任务库回退读到
    持久化配置（不再报"无历史运行参数"）；B3：配置带组件标识
    （backend_factory 不可序列化，仅存 _backend_factory_component、
    无 callable）→ 组件守卫显式失败，不静默换执行语义。"""
    from dag_agent.daemon import RunManager
    from test_v46_codex_review_fixes import _NullishBackend
    home = tmp_path / "h"
    home.mkdir()
    repo = Repository(home / "mission.db")
    graph = Graph(repo)
    bad_check = 'python -c "import sys;sys.exit(2)"'
    mid, root = graph.create_mission(
        "v50 N5", brief=BRIEF.format(body="root"), root_check=bad_check,
        root_declared_artifacts=("deliverable.md",))
    mgr = RunManager(home)
    mgr.start(mid, root=root, max_concurrency=1, real=False, project=False,
              backend_factory=lambda: _NullishBackend(), timeout_s=30)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 20 and mgr._runs[mid]["thread"].is_alive():
        time.sleep(0.1)
    mgr.stop(mid, mode="drain")
    assert repo.get_mission(mid).status == "active"
    mgr.cleanup_finished(keep=0)
    assert mid not in mgr._runs
    # 库内已有持久化恢复配置（B3-2：callable 被剔除、组件标识入库）
    raw = repo._conn.execute(
        "SELECT params_json FROM run_control WHERE id=1").fetchone()
    assert raw and json.loads(raw["params_json"]), (
        "未终态任务的 params 必须持久化到任务库")
    persisted = json.loads(raw["params_json"])
    assert persisted.get("_backend_factory_component") == "<lambda>", (
        f"组件标识必须入库：{persisted}")
    assert "backend_factory" not in persisted, "callable 不得入库"
    # 模拟内存归档被淘汰殆尽（FIFO 淘汰后缓存无该任务）
    mgr._archive.clear()
    assert mid not in mgr._archive
    # blocked 不可 kick——先合法维修回 pending，再 kick（从库回退）
    graph.repair_check(mid, root, new_check_cmd=CMD_OK, reason="n5 维修")
    # kick 从库回退：配置确实读到了（不报"无历史运行参数"），但 B3-3
    # 组件守卫对不可重建组件显式失败（不静默换执行语义）
    r = mgr.kick(mid)
    assert r.get("ok") is False and r.get("kicked") is not True, (
        f"带组件标识的配置 kick 必须显式失败（不静默续跑）：{r}")
    err = r.get("error") or ""
    assert "无法自动重建" in err, f"错误须指明无法自动重建：{err}"
    assert "backend_factory" in err, f"错误须点名缺失组件 backend_factory：{err}"
    assert "无历史运行参数" not in err, (
        f"失败须是组件守卫而非配置缺失（证明从库回退成功）：{err}")
    # 显式失败后 mission 保持 active：既未闭合、也未静默续跑
    assert repo.get_mission(mid).status == "active"
    mgr.stop(mid, mode="kill")
    repo.close()


# ---------- N6 · 停止代际绑定 ----------


def test_n6_old_stop_recheck_spares_new_generation(tmp_path):
    """旧 stop 捕获目标代际 g1；退出复查时库内已是新代际 g2 → 复查
    跳过清理（新代 attempt 保持 running，不被旧停止误取消）。条目必须
    真实带 generation=1（非 None 兜底），确保命中的是"代际不匹配"分支。"""
    from dag_agent.daemon import RunManager
    home = tmp_path / "h"
    home.mkdir()
    repo = Repository(home / "mission.db")
    graph = Graph(repo)
    mid, root = graph.create_mission(
        "v50 N6", brief=BRIEF.format(body="root"), root_check=CMD_OK,
        root_declared_artifacts=("deliverable.md",))
    mgr = RunManager(home)
    # 手工造运行态：gen=1 + running attempt + 已死线程的运行条目
    with repo.tx() as cur:
        cur.execute(
            "INSERT INTO run_control(id,stop_requested,hard_stop,generation,"
            "updated_at) VALUES(1,0,0,1,datetime('now'))"
            " ON CONFLICT(id) DO UPDATE SET generation=1")
    sched = Scheduler(repo, graph, AutoGrowBackend())
    aid = f"a-{mid[-6:]}"
    with repo.tx() as cur:
        cur.execute(
            "INSERT INTO attempts(id,node_id,mission_id,backend,status,"
            "workdir,started_at) VALUES(?,?,?,?,?,?,datetime('now'))",
            (aid, root, mid, "stub", "running", "", ))
        cur.execute("UPDATE nodes SET status='in_work' WHERE id=?", (root,))
    t = threading.Thread(target=lambda: None, daemon=True)
    t.start()
    t.join()
    assert not t.is_alive()
    mgr._runs[mid] = {"thread": t, "scheduler": sched, "status": "done",
                      "params": {}, "generation": 1}
    # 模拟竞态已发生：库内代际已到 2（新 start 的 clear_stop_flags 效果）
    with repo.tx() as cur:
        cur.execute("UPDATE run_control SET generation=2 WHERE id=1")
    # 包装 sched.repo：stop 开头读到**旧代际 1**（竞态前捕获），
    # 退出复查用新连接读真值 2 → 不匹配 → 跳过清理
    class _OldGenRepo:
        def __init__(self, real):
            self._real = real

        def get_run_control(self):
            return {"stop_requested": False, "hard_stop": False,
                    "generation": 1}

        def __getattr__(self, name):
            return getattr(self._real, name)

    sched.repo = _OldGenRepo(sched.repo)
    r = mgr.stop(mid, mode="kill")
    assert r.get("ok") and r.get("stopped") is True
    row = repo._conn.execute(
        "SELECT status FROM attempts WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "running", (
        f"新代际 attempt 不得被旧 stop 复查取消（现 {row['status']}）")
    assert aid not in (r.get("cancelled_attempts") or [])
    mgr._runs.pop(mid, None)
    repo.close()


def test_n6_same_generation_recheck_cleans_up(tmp_path):
    """同代际对照：运行条目 generation=1 且库内 run_control 代际同为 1
    → stop(kill)（线程已死）的退出复查**照常清理**残留 running
    attempt（attempt→cancelled、cancelled_attempts 含该 aid、节点回
    pending）——证明 N6 并非"一律跳过清理"的假绿。"""
    from dag_agent.daemon import RunManager
    home = tmp_path / "h"
    home.mkdir()
    repo = Repository(home / "mission.db")
    graph = Graph(repo)
    mid, root = graph.create_mission(
        "v50 N6b", brief=BRIEF.format(body="root"), root_check=CMD_OK,
        root_declared_artifacts=("deliverable.md",))
    mgr = RunManager(home)
    # 手工造运行态：条目与库内同代际 gen=1 + running attempt + 已死线程
    with repo.tx() as cur:
        cur.execute(
            "INSERT INTO run_control(id,stop_requested,hard_stop,generation,"
            "updated_at) VALUES(1,0,0,1,datetime('now'))"
            " ON CONFLICT(id) DO UPDATE SET generation=1")
    sched = Scheduler(repo, graph, AutoGrowBackend())
    aid = f"a-{mid[-6:]}"
    with repo.tx() as cur:
        cur.execute(
            "INSERT INTO attempts(id,node_id,mission_id,backend,status,"
            "workdir,started_at) VALUES(?,?,?,?,?,?,datetime('now'))",
            (aid, root, mid, "stub", "running", "", ))
        cur.execute("UPDATE nodes SET status='in_work' WHERE id=?", (root,))
    t = threading.Thread(target=lambda: None, daemon=True)
    t.start()
    t.join()
    assert not t.is_alive()
    mgr._runs[mid] = {"thread": t, "scheduler": sched, "status": "done",
                      "params": {}, "generation": 1}
    # 条目代际 1 == 库内真值 1：无换代 → 退出复查照常清理残留
    r = mgr.stop(mid, mode="kill")
    assert r.get("ok") and r.get("stopped") is True
    row = repo._conn.execute(
        "SELECT status FROM attempts WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "cancelled", (
        f"同代际残留 running attempt 必须照常清理（现 {row['status']}）")
    assert aid in (r.get("cancelled_attempts") or []), (
        f"cancelled_attempts 必须包含被清理的 aid：{r}")
    assert repo.get_node(root).status == "pending", (
        "退出复查清理须把 in_work 节点回 pending")
    mgr._runs.pop(mid, None)
    repo.close()
