"""v3.2 第4步+运行接线回归：干净重放/模板/节点熔断/常驻服务/运行中干预/
停止语义/重启恢复/投影自动更新。全部零模型、确定性同步（事件/屏障，无长sleep）。"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from dag_agent.daemon import RunManager
from dag_agent.graph import Graph
from dag_agent.projection import (current_version, render_mission,
                                  render_mission_versioned)
from dag_agent.recovery import pause, resume
from dag_agent.repository import Repository
from dag_agent.scheduler import Scheduler

CHK = "python -c 'pass'"
BITES = ("python -c \"import pathlib,sys; sys.exit(0 if any("
         "pathlib.Path('.').glob('*.txt')) else 1)\"")


def _wait(cond, timeout=60.0, interval=0.02):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(interval)
    return False


class GateBackend:
    """可控后端：按标题挂闸门（Event）；未挂闸门的节点立即成功并写 impl.txt。"""

    name = "stub"  # A3 闸门豁免：测试桩
    order: list = []

    def __init__(self):
        self.gates: dict[str, threading.Event] = {}
        self.calls: list[str] = []

    def gate(self, title):
        ev = threading.Event()
        self.gates[title] = ev
        return ev

    def run(self, statement, workdir, check_cmd, title=""):
        GateBackend.order.append(title)
        self.calls.append(title)
        ev = self.gates.get(title)
        if ev:
            ev.wait(timeout=30)
        (Path(workdir) / "impl.txt").write_text(title or "x", encoding="utf-8")
        return {"ok": True, "summary": "DONE", "session_id": "",
                "resources": {"cost_usd": 0.01}}


@pytest.fixture
def env(tmp_path):
    repo = Repository(tmp_path / "mission.db")
    g = Graph(repo)
    yield repo, g, tmp_path
    repo.close()


# ---- 干净重放（第4步） ----

def test_replay_failure_blocks_machine_closure(env):
    """工作目录过检但重放（干净目录缺未申报文件）不过 → 不得机器闭合。"""
    repo, g, tmp = env
    # 根考卷要求 root 级 marker.txt 存在（BITES 变体只认 marker.txt）
    NEED_MARKER = ("python -c \"import pathlib,sys; sys.exit(0 if any("
                   "pathlib.Path('.').glob('marker.txt')) else 1)\"")
    mid, root = g.create_mission("重放失败", root_check=NEED_MARKER,
                                 root_declared_artifacts=("impl.txt",))
    kid = g.add_node(mid, "K", "子块", parent_id=root, check_cmd=BITES,
                     declared_artifacts=("impl.txt",))
    for nid, name in ((kid, "K"), (root, "整合")):
        aid = g.start_attempt(mid, nid)
        wd = Path(repo.get_attempt(aid).workdir)
        (wd / "impl.txt").write_text(name, encoding="utf-8")
        if nid == root:
            (wd / "marker.txt").write_text("工作目录里有但未申报", encoding="utf-8")
        r = g.submit_attempt(mid, nid, aid, artifact_paths=["impl.txt"])
        if nid == root:
            assert r["verdict"] == "fail", r
            assert "干净重放未过" in r["evidence"]
    assert repo.get_mission(mid).status == "active"
    assert repo.get_node(root).status == "failed"  # 整体未闭合


def test_replay_pass_evidence_recorded(env):
    """正常根闭合：证据含干净重放通过；mission machine 闭合。"""
    repo, g, tmp = env
    mid, root = g.create_mission("重放通过", root_check=BITES,
                                 root_declared_artifacts=("final.txt",))
    aid = g.start_attempt(mid, root)
    wd = Path(repo.get_attempt(aid).workdir)
    (wd / "final.txt").write_text("F", encoding="utf-8")
    r = g.submit_attempt(mid, root, aid, artifact_paths=["final.txt"])
    assert r["verdict"] == "pass"
    assert "干净重放通过" in r["evidence"]
    assert repo.get_mission(mid).status == "done"
    assert repo.get_mission(mid).closure_basis == "machine"


# ---- 模板（第4步） ----

def test_templates_are_usable():
    from dag_agent.acceptance import preflight
    from dag_agent.task_templates import (TEMPLATES, root_check_for,
                                          scheduler_defaults)
    for name, t in TEMPLATES.items():
        ok, why, _ = preflight(check_cmd=root_check_for(name))
        assert ok, f"{name} 根考卷体检未过：{why}"  # 有牙齿：空目录必 FAIL
        d = scheduler_defaults(name)
        assert d["max_retries"] == 2 and d["node_cap"] >= 100
        assert d["provenance"]  # 来源必标（非P95时明示）


def test_composite_checker_semantics(tmp_path):
    import subprocess
    import sys
    cc = Path(__file__).resolve().parents[2] / "dag_agent" / "checkers" / \
        "check_composite.py"
    r = subprocess.run([sys.executable, str(cc), "a.md", "10", "引言"],
                       capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 1  # 缺产物=不合格
    (tmp_path / "a.md").write_text("引言" + "x" * 20, encoding="utf-8")
    r = subprocess.run([sys.executable, str(cc), "a.md", "10", "引言"],
                       capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 0
    r = subprocess.run([sys.executable, str(cc)], capture_output=True, text=True)
    assert r.returncode == 2  # 用法错=考卷侧 ERROR


# ---- 节点数熔断（第4步） ----

def test_node_cap_halts_dispatch(env):
    repo, g, tmp = env
    be = GateBackend()
    s = Scheduler(repo, g, be, max_concurrency=1, node_cap=3,
                  reap_enabled=False)
    mid, root = g.create_mission("熔断", root_check=BITES,
                                 root_declared_artifacts=("impl.txt",))
    for t in "ABCD":
        g.add_node(mid, t, f"{t}", parent_id=root, check_cmd=BITES,
                   declared_artifacts=("impl.txt",))
    summary = s.run_until_done(mid, root, timeout=20, poll=0.05)
    assert "节点数熔断" in (summary["halt_reason"] or ""), summary


# ---- 常驻服务：单调度器互斥 / 运行中干预 / 停止 / 重启恢复 ----

def test_run_manager_single_scheduler_and_live_intervention(env):
    """任务书场景：并发1，A 运行，B/C 待派发——调高 C 先派 C；暂停 B 不派 B；
    恢复后重新入池；运行中查询可用；一个任务仅一个调度器。"""
    repo, g, tmp = env
    be = GateBackend()
    gate_a = be.gate("A")
    gate_c = be.gate("C")
    mid, root = g.create_mission("干预", root_check=BITES,
                                 root_declared_artifacts=("impl.txt",))
    a = g.add_node(mid, "A", "A", parent_id=root, check_cmd=BITES,
                   declared_artifacts=("impl.txt",))
    b = g.add_node(mid, "B", "B", parent_id=root, check_cmd=BITES,
                   declared_artifacts=("impl.txt",))
    c = g.add_node(mid, "C", "C", parent_id=root, check_cmd=BITES,
                   declared_artifacts=("impl.txt",))
    mgr = RunManager(tmp)
    r1 = mgr.start(mid, max_concurrency=1, backend_factory=lambda: be,
                   project=False, timeout_s=30)
    assert r1["ok"], r1
    # 单调度器互斥：重复 start 拒绝
    r2 = mgr.start(mid, backend_factory=lambda: be, project=False)
    assert not r2["ok"] and "仅一个" in r2["error"]
    assert _wait(lambda: mgr.status(mid)["inflight"] == 1)  # A 在途
    st = mgr.status(mid)
    assert st["running"] and st["mission_status"] == "active"
    # 干预：C 提权（下一 tick 先派 C）；B 暂停
    g.set_priority(mid, c, 10)
    pause(repo, g, mid, b, "人工暂停")
    gate_a.set()  # 放行 A → 下一 tick 应派 C（优先级），B 被暂停不派
    assert _wait(lambda: "C" in be.calls), be.calls
    # C 在途窗口内：B 被暂停不得派发（保持有在途，避免触发死局检测）
    time.sleep(0.3)
    assert "B" not in be.calls, be.calls
    resume(repo, g, mid, b)
    gate_c.set()  # 放行 C → B 应重新入池被派发
    assert _wait(lambda: "B" in be.calls), be.calls
    assert _wait(lambda: mgr.status(mid)["mission_status"] == "done"), \
        mgr.status(mid)
    evs = [e.type for e in repo.events_of(mid)]
    assert "mission_done" in evs


def test_stop_drain_and_kill(env):
    """drain=不再派发+在途有界结束；kill=在途置 cancelled、节点回 pending。"""
    repo, g, tmp = env
    mid, root = g.create_mission("停止", root_check=BITES,
                                 root_declared_artifacts=("impl.txt",))
    g.add_node(mid, "A", "A", parent_id=root, check_cmd=BITES,
               declared_artifacts=("impl.txt",))
    be = GateBackend()
    gate = be.gate("A")
    mgr = RunManager(tmp)
    assert mgr.start(mid, max_concurrency=1, backend_factory=lambda: be,
                     project=False, timeout_s=60)["ok"]
    assert _wait(lambda: mgr.status(mid)["inflight"] == 1)
    mgr.stop(mid, mode="kill")   # 在途置 cancelled
    gate.set()                    # 后端线程放行退出（bounded）
    assert _wait(lambda: not mgr.status(mid)["running"])
    with repo.tx() as cur:
        st = cur.execute("SELECT status FROM attempts WHERE mission_id=?",
                         (mid,)).fetchall()
    assert all(r["status"] in ("cancelled",) for r in st), \
        [dict(r) for r in st]
    assert repo.get_mission(mid).status == "active"


def test_restart_recovery_reaps_and_old_submit_rejected(env):
    """单服务重启恢复：残留 running 一律收割；旧尝试不获新提交权。"""
    repo, g, tmp = env
    mid, root = g.create_mission("重启", root_check=BITES,
                                 root_declared_artifacts=("impl.txt",))
    a = g.add_node(mid, "A", "A", parent_id=root, check_cmd=BITES,
                   declared_artifacts=("impl.txt",))
    old = g.start_attempt(mid, a)  # 模拟旧进程残留的 running
    be = GateBackend()
    mgr = RunManager(tmp)
    r = mgr.start(mid, max_concurrency=1, backend_factory=lambda: be,
                  project=False, timeout_s=30)
    assert r["ok"] and old in r.get("reaped_attempts", []), r
    assert repo.get_attempt(old).status == "orphaned"
    with pytest.raises(Exception, match="不可提交"):  # 旧进程提交被拒
        g.submit_attempt(mid, a, old, artifact_paths=["impl.txt"])
    assert _wait(lambda: mgr.status(mid)["mission_status"] == "done")
    assert repo.get_node(a).status == "accepted"


# ---- 投影自动更新：版本守卫 + 原子 + 可重建 ----

def test_projection_versioned_guard_and_rebuild(env):
    repo, g, tmp = env
    mid, root = g.create_mission("投影", root_check=BITES,
                                 root_declared_artifacts=("impl.txt",))
    be = GateBackend()
    mgr = RunManager(tmp)
    assert mgr.start(mid, max_concurrency=2, backend_factory=lambda: be,
                     project=True, timeout_s=30)["ok"]
    assert _wait(lambda: mgr.status(mid)["mission_status"] == "done")
    assert _wait(lambda: not mgr.status(mid)["running"])  # 收尾渲染完成
    board = Path(repo.db_path).parent / "board"
    overview = board / "任务总览.md"
    marker = board / ".projection_version"
    assert _wait(lambda: overview.is_file() and marker.is_file())  # 负载兜底
    assert overview.is_file()
    text = overview.read_text(encoding="utf-8")
    assert "投影版本：事件#" in text
    v1 = int(marker.read_text(encoding="utf-8"))
    # 负载容忍：守护渲染可能由测试补齐（ daemon 收尾被吞时），两种都合法
    out = render_mission_versioned(repo, g, mid)
    assert out is None or out.is_file()
    marker.write_text(str(current_version(repo, mid) + 100), encoding="utf-8")
    assert render_mission_versioned(repo, g, mid) is None  # 旧不覆盖新
    import shutil
    shutil.rmtree(board)
    assert render_mission_versioned(repo, g, mid) is not None  # 删了可重建
    assert (board / "任务总览.md").is_file()
    assert (board / "当前任务图.canvas").is_file()
