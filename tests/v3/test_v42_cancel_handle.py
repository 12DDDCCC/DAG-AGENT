# -*- coding: utf-8 -*-
"""v3.12 阶段 1B 回归：停止接到具体执行进程。

任务书 1B 判据：
- 一个 attempt 对应一个执行句柄；取消只影响该 attempt 及其子进程树；
- 取消后 10 秒内进程树退出（假 CLI 实测，不 sleep 后猜测）；
- 三窗口：进程启动前 / 运行中 / 退出与提交交界——迟到结果不发布，
  不把取消节点改成 failed/accepted；
- drain 不误杀在途；
- stdout/stderr 持续排空（reader 线程有界收集），执行位释放。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from dag_agent.cc_backend import ClaudeCliBackend
from dag_agent.graph import Graph
from dag_agent.repository import Repository
from dag_agent.scheduler import Scheduler

SHIM = str(Path(__file__).parent / "fake_claude_shim.py")
CHECKER = str(Path(__file__).resolve().parents[2] / "dag_agent" / "checkers"
              / "check_file_contains.py")
BRIEF = ("【输入】无/【交付清单】hello.txt 内容 DAG-OK/【接口签名】文件交付"
         "/【样例】DAG-OK/【自测入口】cat hello.txt")
ROOT_BRIEF = ("【输入】inputs/ 下叶子交付副本/【交付清单】hello.txt 内容"
              " DAG-OK/【接口签名】文件交付/【样例】DAG-OK/【自测入口】"
              "cat hello.txt")


def _backend(tmp_path, timeout=120):
    envf = tmp_path / "env.local"
    envf.write_text("ANTHROPIC_BASE_URL=https://fake\n"
                    "ANTHROPIC_AUTH_TOKEN=d\n", encoding="utf-8")
    return ClaudeCliBackend(cli_path=SHIM, env_file=envf, timeout=timeout)


def _mk(tmp_path, titles, mode="deliver"):
    os.environ["FAKE_MODE"] = mode
    repo = Repository(tmp_path / "m" / "mission.db")
    g = Graph(repo)
    be = _backend(tmp_path)
    mid, root = g.create_mission(
        "1B取消", brief=ROOT_BRIEF,
        root_check=f'python "{CHECKER}" hello.txt DAG-OK',
        root_declared_artifacts=("hello.txt",))
    ids = {}
    for t in titles:
        ids[t] = g.add_node(mid, t, BRIEF, parent_id=root,
                            check_cmd=f'python "{CHECKER}" hello.txt DAG-OK',
                            declared_artifacts=("hello.txt",))
    s = Scheduler(repo, g, be, max_concurrency=1, reap_enabled=False)
    return repo, g, mid, root, ids, s


def _wait_running(repo, timeout=30):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        row = repo._conn.execute(
            "SELECT id FROM attempts WHERE status='running' LIMIT 1"
        ).fetchone()
        if row:
            return row["id"]
        time.sleep(0.1)
    raise AssertionError("30s 内无 running attempt")


def test_backend_cancel_kills_process_within_10s(tmp_path):
    """运行中取消：10 秒内进程树退出（实测时长，非 sleep 猜测）。"""
    os.environ["FAKE_MODE"] = "sleep"
    try:
        be = _backend(tmp_path)
        ev = threading.Event()
        threading.Thread(target=lambda: (time.sleep(1.0), ev.set()),
                         daemon=True).start()
        t0 = time.monotonic()
        r = be.run("长任务", tmp_path / "wd", None, "sleep节点",
                   cancel_event=ev)
        dt = time.monotonic() - t0
        assert r["resources"].get("cancelled") is True
        assert r["resources"]["receipt_status"] == "incomplete"
        assert dt < 12.0, f"取消后 {dt:.1f}s 才返回（阈值 10s+裕量）"
    finally:
        os.environ.pop("FAKE_MODE", None)


def test_scheduler_cancel_running_releases_and_no_publish(tmp_path):
    """运行中取消（scheduler 级）：attempt=cancelled、节点回 pending（不
    是 failed/accepted）、无发布、执行位释放、stdout/stderr 排空线程收尾。"""
    repo, g, mid, root, ids, s = _mk(tmp_path, ["L1"], mode="sleep")
    stop = threading.Event()

    def runner():
        s.run_until_done(mid, root, timeout=120, poll=0.05)

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    aid = _wait_running(repo)
    time.sleep(0.5)  # 让进程真正起来
    s.cancel_attempt(aid, reason="1B回归：运行中取消")
    s.stop_requested.set()  # 防止重派（取消=人工干预语义）
    t.join(timeout=60)
    att = repo.get_attempt(aid)
    assert att.status == "cancelled"
    assert repo.get_node(ids["L1"]).status == "pending"
    pub = repo._conn.execute(
        "SELECT COUNT(*) c FROM assessments WHERE attempt_id=? AND"
        " authoritative=1", (aid,)).fetchone()["c"]
    assert pub == 0  # 迟到结果不发布
    with s._inflight_lock:
        assert not s._futures  # 执行位释放
    repo.close()


def test_cancel_before_process_start(tmp_path):
    """启动前取消：attempt 已建、进程未启动 → 直接撤销提交权，
    从未启动进程、无执行痕迹。"""
    repo, g, mid, root, ids, s = _mk(tmp_path, ["L1", "L2"], mode="sleep")
    stop = threading.Event()

    def runner():
        s.run_until_done(mid, root, timeout=120, poll=0.05)

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    aid1 = _wait_running(repo)  # L1 占住并发 1 执行位
    # 手工为 L2 建在途 attempt（进程启动前窗口；scheduler 不再派并发 2）
    aid2 = g.start_attempt(mid, ids["L2"], backend=s.backend.name)
    s.cancel_attempt(aid2, reason="1B回归：启动前取消")
    s.cancel_attempt(aid1, reason="1B回归：清理")
    s.stop_requested.set()
    t.join(timeout=60)
    a2 = repo.get_attempt(aid2)
    assert a2.status == "cancelled"
    assert repo.get_node(ids["L2"]).status == "pending"
    wd2 = Path(a2.workdir)
    assert not (wd2 / "hello.txt").is_file()  # 从未执行
    evs = [e.type for e in repo.events_of(mid)]
    assert "attempt_cancelled" in evs
    repo.close()


def test_cancel_at_submit_boundary_late_result_not_published(tmp_path):
    """退出与提交交界：进程正常完成、提交前取消 → 结果不发布，
    节点不变成 accepted/failed。"""
    calls = {"n": 0}
    inner = _backend(tmp_path)

    class BoundaryBackend:
        name = "claude-cli"

        def __init__(self):
            self.backend_version = inner.backend_version
            self.sched = None

        def run(self, statement, workdir, check_cmd, title="",
                cancel_event=None):
            r = inner.run(statement, workdir, check_cmd, title)
            if title == "L1" and self.sched is not None:
                # 交界点：结果已在手、提交未发生——此刻取消
                with self.sched._inflight_lock:
                    aids = list(self.sched._futures.keys())
                if aids:
                    self.sched.cancel_attempt(aids[0], reason="1B回归：交界取消")
            return r

    os.environ["FAKE_MODE"] = "deliver"
    try:
        repo = Repository(tmp_path / "m" / "mission.db")
        g = Graph(repo)
        mid, root = g.create_mission(
            "1B交界", brief=ROOT_BRIEF,
            root_check=f'python "{CHECKER}" hello.txt DAG-OK',
            root_declared_artifacts=("hello.txt",))
        nid = g.add_node(mid, "L1", BRIEF, parent_id=root,
                         check_cmd=f'python "{CHECKER}" hello.txt DAG-OK',
                         declared_artifacts=("hello.txt",))
        be = BoundaryBackend()
        s = Scheduler(repo, g, be, max_concurrency=1, reap_enabled=False)
        be.sched = s
        summary = s.run_until_done(mid, root, timeout=60, poll=0.05)
        assert repo.get_node(nid).status == "pending"  # 非 accepted/failed
        row = repo._conn.execute(
            "SELECT status FROM attempts WHERE node_id=?", (nid,)).fetchone()
        assert row["status"] == "cancelled"
        pub = repo._conn.execute(
            "SELECT COUNT(*) c FROM assessments WHERE authoritative=1"
        ).fetchone()["c"]
        assert pub == 0
        repo.close()
    finally:
        os.environ.pop("FAKE_MODE", None)


def test_drain_finishes_inflight_without_killing(tmp_path):
    """drain：停止派新、在途按约定完成（不误杀）——叶子完成发布，
    root 因 drain 不再派发，halt=服务停止。"""
    repo, g, mid, root, ids, s = _mk(tmp_path, ["L1"], mode="slow_deliver")

    def kicker():
        time.sleep(1.0)
        s.stop_requested.set()

    threading.Thread(target=kicker, daemon=True).start()
    summary = s.run_until_done(mid, root, timeout=60, poll=0.05)
    assert repo.get_node(ids["L1"]).status == "accepted"  # 在途完成不误杀
    assert "服务停止" in (summary["halt_reason"] or "")
    # Codex 验收修正：垫片 NameError 崩溃曾让本测试经由"崩溃但文件已写
    # →判卷过"假绿——必须断言回执 valid（正常会话完成）而非仅 accepted
    rj = json.loads(repo._conn.execute(
        "SELECT resources_json FROM attempts WHERE node_id=?",
        (ids["L1"],)).fetchone()["resources_json"])
    assert rj.get("receipt_status") == "valid" and rj.get("session_id")
    repo.close()


def test_cancel_one_node_others_unaffected(tmp_path):
    """并发 2：取消其一，另一节点正常完成发布（取消只影响该 attempt）。"""
    repo, g, mid, root, ids, s = _mk(tmp_path, ["L1", "L2"],
                                     mode="slow_deliver")
    s.max_concurrency = 2

    def kicker():
        time.sleep(1.5)
        rows = repo._conn.execute(
            "SELECT id, node_id FROM attempts WHERE status='running'"
        ).fetchall()
        for r in rows:
            if r["node_id"] == ids["L1"]:
                s.cancel_attempt(r["id"], reason="1B回归：只取消L1")
        s.stop_requested.set()

    threading.Thread(target=kicker, daemon=True).start()
    s.run_until_done(mid, root, timeout=60, poll=0.05)
    assert repo.get_node(ids["L2"]).status == "accepted"
    a1 = repo._conn.execute(
        "SELECT status FROM attempts WHERE node_id=?", (ids["L1"],)
    ).fetchone()["status"]
    assert a1 == "cancelled"
    repo.close()
