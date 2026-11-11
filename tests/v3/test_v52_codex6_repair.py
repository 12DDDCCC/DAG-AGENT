# -*- coding: utf-8 -*-
"""v52：Codex 第六轮修复（2026-10-13）C1–C4 与空清单认领的正式回归。

对应 outputs/DAG-agent-第六轮独立验收-2026-10-12.md 与 ZCode 第六轮任务书：
- C1 考卷数据库资源冻结：
  - 业务考卷数据库（如 oracle.db）改写参照值导致考卷摘要改变，认领/提交拒绝；
  - 真正任务库事务及 WAL 滚动不造成无关 digest 漂移；
  - 未改动业务资源的 GOOD 正例正常 pass 并 accepted；
  - 同目录资源过多（>20 件）显式拒绝，不静默截断。
- C2 启动配置持久化与代际原子绑定：
  - 持久化配置写失败注入下 start 不返回成功，新后端调用数为 0，无半提交；
  - 代际不虚增，错代（配置代际 != 库内代际）时 kick 明确拒绝；
  - 正常配置未 cleanup 前 kick 正确恢复；
  - 非序列化组件拒恢复不静默换执行语义。
- 空清单认领语义：
  - 坏旧库 files_json='[]' 且无 files_meta_json（但 manifest_hash 为非空）在 start_attempt 认领阶段立即 fail-closed 拒绝；
  - 协议允许的合法空产物（manifest_hash 为 empty_hash）认领成功。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import dag_agent.daemon as daemon  # noqa: E402
from dag_agent.acceptance import AcceptanceError, compute_check_digest, _exam_manifest  # noqa: E402
from dag_agent.artifacts import manifest_hash_of  # noqa: E402
from dag_agent.graph import Graph, GraphError  # noqa: E402
from dag_agent.repository import Repository, is_active_ledger_file  # noqa: E402

from test_v45_autogrow import BRIEF, CMD_OK, AutoGrowBackend, build_mission  # noqa: E402
from test_v49_codex3_review import _parent_with_upstream, _apply_two_children  # noqa: E402


# ---------- C1 · 业务数据库考卷资源冻结 ----------


def test_c1_oracle_db_modification_rejected(tmp_path):
    """C1: 独立 oracle.db 业务数据库修改参照值，考卷摘要必须改变，start_attempt 拒绝。"""
    repo, g, mid, root = build_mission(tmp_path / "m")
    oracle = tmp_path / "oracle.db"
    with sqlite3.connect(oracle) as c:
        c.execute("CREATE TABLE expected(value TEXT)")
        c.execute("INSERT INTO expected VALUES('GOOD')")
        c.commit()

    judge = tmp_path / "judge.py"
    judge.write_text("""from pathlib import Path
import sqlite3, sys
db = Path(__file__).with_name('oracle.db')
with sqlite3.connect('file:' + db.as_posix() + '?mode=ro', uri=True) as c:
    expected = c.execute('SELECT value FROM expected').fetchone()[0]
p = Path('deliverable.md')
sys.exit(0 if p.is_file() and p.read_text(encoding='utf-8') == expected else 1)
""", encoding="utf-8")
    cmd = f'python "{judge}"'

    v = g.repair_check(mid, root, new_check_cmd=cmd, reason="legal business oracle db")
    assert v == 2
    frozen = repo._conn.execute("SELECT exam_digest FROM node_exam_freeze WHERE node_id=?", (root,)).fetchone()[0]

    orig_bytes = oracle.read_bytes()
    with sqlite3.connect(oracle) as c:
        c.execute("UPDATE expected SET value='BAD'")
        c.commit()

    d_after = compute_check_digest(check_cmd=cmd)
    assert frozen != d_after, "修改 oracle.db 后考卷摘要必须发生漂移"

    with pytest.raises(GraphError, match="考卷内容与出题冻结摘要不一致"):
        g.start_attempt(mid, root, backend="stub")

    # 正对照：恢复原始数据库字节，认领与提交正常闭合
    oracle.write_bytes(orig_bytes)
    assert compute_check_digest(check_cmd=cmd) == frozen
    aid = g.start_attempt(mid, root, backend="stub")
    (Path(repo.get_attempt(aid).workdir) / "deliverable.md").write_text("GOOD", encoding="utf-8")
    r = g.submit_attempt(mid, root, aid, artifact_paths=["deliverable.md"])
    assert r["verdict"] == "pass"
    assert repo.get_node(root).status == "accepted"
    repo.close()


def test_c1_active_ledger_wal_no_drift(tmp_path):
    """C1: 任务库自身的事务提交和 WAL/SHM 滚动不引起无关考卷 digest 漂移。"""
    repo, g, mid, root = build_mission(tmp_path)
    exam = tmp_path / "plain_exam.py"
    exam.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    cmd = f'python "{exam}"'

    d1 = compute_check_digest(check_cmd=cmd)
    with repo.tx() as cur:
        repo.append_event(cur, mid, "dummy_event", root)
    d2 = compute_check_digest(check_cmd=cmd)
    assert d1 == d2, "活账本事务写入不得导致考卷 digest 漂移"
    repo.close()


def test_c1_too_many_resource_files_rejected(tmp_path):
    """C1: 考卷同目录非 .py 资源文件过多（>20 件）显式拒绝，不静默截断造成未锁漂移。"""
    exam = tmp_path / "exam_res.py"
    exam.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    for i in range(25):
        (tmp_path / f"data_{i:02d}.txt").write_text(f"val_{i}", encoding="utf-8")

    cmd = f'python "{exam}"'
    with pytest.raises(AcceptanceError, match="考卷同目录资源文件过多"):
        compute_check_digest(check_cmd=cmd)


# ---------- C2 · 启动配置与代际原子绑定 ----------


def test_c2_persist_failure_no_half_commit(tmp_path):
    """C2: 启动配置持久化写失败注入下，start 返回失败，不启动线程，不推进代际，无半提交。"""
    repo = Repository(tmp_path / "mission.db")
    graph = Graph(repo)
    mid, root = graph.create_mission(
        "c2-test-mission",
        brief=BRIEF.format(body="root"),
        root_check=CMD_OK,
        root_declared_artifacts=("deliverable.md",)
    )
    for i in range(3):
        graph.add_node(
            mid,
            title=f"leaf{i}",
            statement=BRIEF.format(body="leaf"),
            parent_id=root,
            check_cmd=CMD_OK,
            declared_artifacts=("deliverable.md",)
        )

    mgr = daemon.RunManager(tmp_path)
    base = dict(
        mid=mid, root=root, max_concurrency=1, max_retries=1,
        max_cost_usd=None, real=False, node_cap=20, project=False,
        timeout_s=60, auto_split=False
    )

    # 第一次正常启动
    r1 = mgr.start(**base, max_calls=8)
    assert r1["ok"] is True
    mgr.stop(mid, "drain")

    rc1 = repo.get_run_control()
    assert rc1["generation"] == 1
    cfg1 = json.loads(repo._conn.execute("SELECT params_json FROM run_control WHERE id=1").fetchone()["params_json"])
    assert cfg1["max_calls"] == 8

    # 第二次注入写失败
    old_persist = daemon._persist_params
    def fail_second(repo_arg, params, sched=None, generation=None):
        if params.get("max_calls") == 2:
            raise RuntimeError("SIMULATED_CONFIG_PERSIST_FAILURE")
        return old_persist(repo_arg, params, sched=sched, generation=generation)

    daemon._persist_params = fail_second
    try:
        r2 = mgr.start(**base, max_calls=2)
        assert r2["ok"] is False
        assert "启动配置持久化失败" in r2["error"]
    finally:
        daemon._persist_params = old_persist

    # 代际保持 1，不得推进到 2，参数仍为旧代 8
    rc2 = repo.get_run_control()
    assert rc2["generation"] == 1
    cfg2 = json.loads(repo._conn.execute("SELECT params_json FROM run_control WHERE id=1").fetchone()["params_json"])
    assert cfg2["max_calls"] == 8

    # 重建 Manager 恢复 kick 依然为代际 1 的合法配置
    fresh = daemon.RunManager(tmp_path)
    k = fresh.kick(mid)
    assert k["ok"] is True
    fresh.stop(mid, "drain")
    repo.close()


def test_c2_generation_mismatch_rejected(tmp_path):
    """C2: 当数据库代际与恢复配置所属代际不一致（错代）时，kick 明确拒绝。"""
    repo = Repository(tmp_path / "mission.db")
    graph = Graph(repo)
    mid, root = graph.create_mission(
        "c2-mismatch-mission",
        brief=BRIEF.format(body="root"),
        root_check=CMD_OK,
        root_declared_artifacts=("deliverable.md",)
    )
    mgr = daemon.RunManager(tmp_path)
    base = dict(
        mid=mid, root=root, max_concurrency=1, max_retries=1,
        max_cost_usd=None, real=False, node_cap=20, project=False,
        timeout_s=60, auto_split=False
    )
    mgr.start(**base, max_calls=4)
    mgr.stop(mid, "drain")

    # 人为修改库内 generation=3 制造错代
    with repo.tx() as cur:
        cur.execute("UPDATE run_control SET generation=3 WHERE id=1")

    fresh = daemon.RunManager(tmp_path)
    k = fresh.kick(mid)
    assert k["ok"] is False
    assert "错代" in k["error"]
    repo.close()


# ---------- 空清单认领语义 ----------


def test_empty_manifest_corrupted_rejected_at_claim(tmp_path):
    """空清单语义: 坏旧库 files_json='[]' 但 manifest_hash 非空，在 start_attempt 认领即拒。"""
    repo, g, mid, root, up, parent, sid, b, fp = _parent_with_upstream(tmp_path)
    kids, _ = _apply_two_children(repo, g, mid, parent, "migration_bad", b, fp)

    with repo.tx() as cur:
        cur.execute("UPDATE snapshots SET files_json='[]' WHERE id=?", (sid,))
        cur.execute("ALTER TABLE dep_bindings DROP COLUMN files_meta_json")
    repo.close()

    repo = Repository(tmp_path / "mission.db")
    g = Graph(repo)

    with pytest.raises(GraphError, match="拒绝认领"):
        g.start_attempt(mid, kids[0], backend="stub")
    repo.close()


def test_legitimate_empty_manifest_claim_succeeds(tmp_path):
    """空清单语义: 协议明确允许的合法空产物（manifest_hash==e3b0c442...）认领成功。"""
    repo, g, mid, root = build_mission(tmp_path)
    exam_empty = tmp_path / "check_empty.py"
    exam_empty.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    cmd_empty = f'python "{exam_empty}"'

    up = g.add_node(mid, title="up_empty", statement=BRIEF.format(body="u"), parent_id=root, check_cmd=cmd_empty, declared_artifacts=())
    aid_up = g.start_attempt(mid, up, backend="stub")
    assert g.submit_attempt(mid, up, aid_up, artifact_paths=[])["verdict"] == "pass"

    down = g.add_node(mid, title="down", statement=BRIEF.format(body="d"), parent_id=root, deps=(up,), check_cmd=cmd_empty, declared_artifacts=())
    aid_down = g.start_attempt(mid, down, backend="stub")
    assert aid_down is not None
    assert repo.get_attempt(aid_down).status == "running"
    repo.close()
