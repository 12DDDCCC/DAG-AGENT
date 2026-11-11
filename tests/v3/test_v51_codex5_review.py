# -*- coding: utf-8 -*-
"""v51：Codex 第五轮独立验收（2026-10-12）B1–B3 的正式回归。

对应 outputs/DAG-agent-第五轮独立验收-2026-10-12.md §4：
- B1 隐式清单维修：空清单节点维修成 .pyw 考卷——修复后 .pyw 入摘要，
  永久改卷必变摘要 → 认领拒绝、BAD 无法发布；.py 考卷 digest 不变
  （兼容性）。
- B2 身份三元组：同内容改 snapshot.node_id / artifact_version →
  播种与重放均拒绝；正例身份一致播种成功。
- B3 恢复配置：①start 即持久化（无 cleanup 直接 kick 成功，数字
  配置续跑）；②不可重建组件（backend_factory）kick 显式失败不静默
  换执行语义。
验收器三夹具（NaN/部分探针区间/not_kill 原因）见 test_v48 扩展。
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
UP = "ORIGINAL upstream payload for v51 identity tests"


# ---------- B1 · 隐式入口入摘要 ----------


def test_b1_implicit_pyw_repair_locks_content(tmp_path):
    """空清单节点维修成 .pyw 考卷（不传 exam_files）→ 修复后 .pyw
    内容入摘要：永久改卷 → 认领拒绝（旧实现摘要不变、BAD 机器闭合）。"""
    repo, graph, mid, root = build_mission(tmp_path)
    exam = tmp_path / "repair_exam.pyw"
    exam.write_text(STRICT, encoding="utf-8")
    new_cmd = f'python "{exam}"'
    # 合法维修（不传 exam_files=继承空清单）
    new_v = graph.repair_check(mid, root, new_check_cmd=new_cmd,
                               reason="b1", exam_files=None)
    assert new_v == 2
    # 修复后：.pyw 内容已在摘要内——永久改卷 → 认领必须拒绝
    exam.write_text(GREEN, encoding="utf-8")
    with pytest.raises(GraphError, match="冻结摘要不一致"):
        graph.start_attempt(mid, root, backend="stub")
    # 不留半更新：改回原内容 → 认领恢复（证明拒绝正是内容锁定所致）
    exam.write_text(STRICT, encoding="utf-8")
    aid = graph.start_attempt(mid, root, backend="stub")
    assert repo.get_attempt(aid).status == "running"
    repo.close()


def test_b1_py_digest_unchanged(tmp_path):
    """兼容性：.py 考卷的隐式摘要与旧行为逐字节一致（manifest 集合
    相同）——修复只新增 .pyw/无扩展名入口入锁，不改变既有 digest。"""
    from dag_agent.acceptance import compute_check_digest as _ccd
    repo, graph, mid, root = build_mission(tmp_path)
    exam = tmp_path / "plain.py"
    exam.write_text(STRICT, encoding="utf-8")
    new_cmd = f'python "{exam}"'
    d_repair = _ccd(check_cmd=new_cmd, exam_files=())
    graph.repair_check(mid, root, new_check_cmd=new_cmd,
                       reason="b1-compat", exam_files=None)
    d_now = _ccd(check_cmd=graph.repo.get_node(root).check_cmd,
                 exam_files=())
    assert d_repair == d_now, ".py 考卷 digest 不得因修复变化"
    repo.close()


# ---------- B2 · 身份三元组 ----------


def _leaf_with_up(tmp_path, payload=UP):
    repo, graph, mid, root = build_mission(tmp_path)
    up = graph.add_node(mid, title="上游", statement=BRIEF.format(body="u"),
                        parent_id=root, check_cmd=CMD_OK,
                        declared_artifacts=("deliverable.md",))
    leaf = graph.add_node(mid, title="普通叶子",
                          statement=BRIEF.format(body="leaf"),
                          parent_id=root, deps=(up,), check_cmd=CMD_OK,
                          declared_artifacts=("deliverable.md",))
    aid = graph.start_attempt(mid, up, backend="stub")
    wd = Path(repo.get_attempt(aid).workdir)
    (wd / "deliverable.md").write_text(payload, encoding="utf-8")
    assert graph.submit_attempt(mid, up, aid,
                                artifact_paths=["deliverable.md"]
                                )["verdict"] == "pass"
    la = graph.start_attempt(mid, leaf, backend="stub")
    binding = repo.get_attempt(la).input_binding_json
    return repo, graph, mid, root, up, leaf, binding, la


def test_b2_source_identity_rejected(tmp_path):
    """同内容改 snapshot.node_id（n2→n3）→ 播种拒绝（不进错误目录）、
    根重放拒绝。"""
    repo, graph, mid, root, up, leaf, binding, la = _leaf_with_up(tmp_path)
    sid = json.loads(binding)[0]["snapshot_id"]
    with repo.tx() as cur:
        cur.execute("UPDATE snapshots SET node_id='n3' WHERE id=?", (sid,))
    sched = Scheduler(repo, graph, AutoGrowBackend())
    cw = Path(repo.get_attempt(la).workdir)
    with pytest.raises(ValueError, match="来源被改写"):
        sched._seed_bound(la, cw)
    assert not list(cw.rglob("deliverable.md")), "不得播种进错误目录"
    # 重放同规则
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "deliverable.md").write_text("x" * 50, encoding="utf-8")
    rv, ev = graph._replay_fresh(mid, root, cand,
                                 json.loads(binding))
    assert rv == "error" and "来源" in ev
    repo.close()


def test_b2_version_identity_rejected(tmp_path):
    """认领后只把 snapshot.artifact_version 改成他值（账本绑 v1、
    内容与来源均一致）→ 播种拒绝（版本身份与账本不符，不读他版）。
    注意：不走 revise——revise 会把旧快照置失效，触发的是
    "绑定快照缺失或已失效"而非版本身份核验。"""
    repo, graph, mid, root, up, leaf, binding, la = _leaf_with_up(tmp_path)
    sid = json.loads(binding)[0]["snapshot_id"]
    # 认领后直改快照行的版本字段：账本 artifact_version=1 vs 快照行=9
    # （快照保持 valid、内容与 node_id 不动，单独破坏"版本"一元）
    with repo.tx() as cur:
        cur.execute("UPDATE snapshots SET artifact_version=9 WHERE id=?",
                    (sid,))
    sched = Scheduler(repo, graph, AutoGrowBackend())
    cw = Path(repo.get_attempt(la).workdir)
    with pytest.raises(ValueError, match="版本"):
        sched._seed_bound(la, cw)
    repo.close()


def test_b2_identity_positive_still_seeds(tmp_path):
    """正例：身份三元组一致 → 播种成功且内容逐字节正确。"""
    repo, graph, mid, root, up, leaf, binding, la = _leaf_with_up(tmp_path)
    sched = Scheduler(repo, graph, AutoGrowBackend())
    cw = Path(repo.get_attempt(la).workdir)
    sched._seed_bound(la, cw)
    seeded = list(cw.rglob("deliverable.md"))
    assert seeded and seeded[0].read_text(encoding="utf-8") == UP
    repo.close()


# ---------- B3 · 恢复配置 ----------


def test_b3_params_persisted_at_start(tmp_path):
    """start 即持久化：drain 后**不做 cleanup**、重建 RunManager →
    kick 从库回退成功（不再"无历史运行参数"）。"""
    from dag_agent.daemon import RunManager
    from test_v46_codex_review_fixes import build_simple, _NullishBackend
    home = tmp_path / "h"
    home.mkdir()
    repo, graph, mid, root = build_simple(home)
    mgr = RunManager(home)
    r = mgr.start(mid, root=root, max_concurrency=1, real=False,
                  project=False, backend_factory=lambda: _NullishBackend(),
                  timeout_s=30)
    assert r.get("ok")
    mgr.stop(mid, mode="drain")
    # 未 cleanup：库内 params 已在 start 时持久化
    raw = repo._conn.execute(
        "SELECT params_json FROM run_control WHERE id=1").fetchone()
    assert raw and json.loads(raw["params_json"]), (
        "start 即必须持久化恢复配置")
    # 重建 RunManager（模拟服务重启）→ kick 走库回退
    mgr2 = RunManager(home)
    rk = mgr2.kick(mid)
    # 参数含不可重建组件 → 显式失败（错误含组件名），不静默换 null
    assert rk.get("ok") is False
    assert "backend_factory" in (rk.get("error") or ""), rk
    assert "无法自动重建" in (rk.get("error") or "")
    mgr2.stop(mid, mode="kill")
    repo.close()


def test_b3_numeric_config_kick_runs(tmp_path):
    """数字配置（无 factory）正例：start→drain→重建 Manager→kick
    从库回退成功并真实重启调度器（产生新 attempt）。"""
    from dag_agent.daemon import RunManager
    from test_v46_codex_review_fixes import build_simple
    home = tmp_path / "h2"
    home.mkdir()
    repo, graph, mid, root = build_simple(home)
    # 先把 root 打到 failed（一次失败消耗重试额度后仍 pending/failed
    # 可派发），保证 kick 有活干
    bad = 'python -c "import sys;sys.exit(1)"'
    graph.repair_check(mid, root, new_check_cmd=bad, reason="b3 数字配置")
    mgr = RunManager(home)
    r = mgr.start(mid, root=root, max_concurrency=1, real=False,
                  project=False, timeout_s=30)   # 无 factory（数字配置）
    assert r.get("ok")
    mgr.stop(mid, mode="drain")
    mgr2 = RunManager(home)
    rk = mgr2.kick(mid)
    assert rk.get("ok") and rk.get("kicked"), (
        f"数字配置应从库回退并重启：{rk}")
    # 真实重启：有新 attempt 产生（Null 语义如实执行失败重试链）
    t0 = time.monotonic()
    while time.monotonic() - t0 < 15:
        n = repo._conn.execute(
            "SELECT COUNT(*) AS c FROM attempts WHERE mission_id=?",
            (mid,)).fetchone()["c"]
        if n >= 2:
            break
        time.sleep(0.2)
    assert n >= 2, "kick 后应有新 attempt（续跑真实发生）"
    mgr2.stop(mid, mode="kill")
    repo.close()


def test_b3_component_guard_message(tmp_path):
    """组件标识：_persist_params 剔除 callable 时记录 _*_component，
    kick 守卫按标识显式失败。"""
    from dag_agent.daemon import _persist_params
    repo, graph, mid, root = build_mission(tmp_path)
    _persist_params(repo, {"mid": "m-1", "root": root, "real": False,
                           "backend_factory": lambda: None})
    raw = repo._conn.execute(
        "SELECT params_json FROM run_control WHERE id=1").fetchone()
    d = json.loads(raw["params_json"])
    assert d["mid"] == "m-1" and d["real"] is False
    assert "_backend_factory_component" in d
    assert d["_backend_factory_component"] == "<lambda>"
    repo.close()
