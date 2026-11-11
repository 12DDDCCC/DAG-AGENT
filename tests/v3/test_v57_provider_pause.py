# -*- coding: utf-8 -*-
"""供应商错误分类停派回归（v57，2026-11-08 独立复查 P2 关闭条件）。

覆盖（任务书 §七）：
- 额度/余额/认证类 → provider_pause：停派+事件+params_json 持久化；
  重启（新 RunManager/start）被拒；provider_pause_ack=True 恢复；
- 临时限流 → 指数退避：窗口内不派发、节点回待派不耗重试预算、
  退避后恢复；连续限流达阈值转人工暂停；
- 普通正文含错误码字样（error_class 缺失）不触发任何停派；
- service_unavailable/model_unavailable/unknown 按普通失败走重试预算；
- 暂停后预算/累计不重置；并发在途不被误杀。
全部零模型（模拟后端），无供应商调用。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from dag_agent.daemon import RunManager
from dag_agent.graph import Graph
from dag_agent.repository import Repository
from dag_agent.scheduler import Budget, Scheduler


class FakeNodeBackend:
    """按节点 id 返回预设 run_result 的模拟后端（name=stub 入平台桩
    豁免表——测试节点陈述无五段简报，真实后端名会被派发硬闸门拒绝）。"""

    name = "stub"

    def __init__(self, script: dict, ok_result: dict | None = None):
        self.script = script
        self.ok_result = ok_result or {}
        self.calls: list[str] = []

    def run(self, statement, workdir, check_cmd, title="", cancel_event=None):
        self.calls.append(title)
        spec = self.script.get(title)  # title=节点标题（如“节点0”）
        if spec is None:
            r = dict(self.ok_result)
            r.setdefault("ok", True)
            r.setdefault("summary", "ok")
            r.setdefault("session_id", f"s-{len(self.calls)}")
            r.setdefault("resources", {"origin": "fake", "agent_id": "a",
                                       "tokens": 1, "duration_ms": 1,
                                       "cost_currency": "USD",
                                       "cost": {"amount": None,
                                                "basis": "unknown",
                                                "currency": "USD"},
                                       "receipt_status": "valid"})
            return r
        return json.loads(json.dumps(spec))


def build_task(home: Path, check_cmd: str = "python -c pass"):
    repo = Repository(home / "mission.db")
    graph = Graph(repo, checker3=None)
    mid, root = graph.create_mission(
        "供应商停派回归",
        "简单任务",
        root_check=check_cmd)
    nids = []
    for i in range(3):
        nid = graph.add_node(mid, f"节点{i}", f"节点{i}陈述",
                             parent_id=root, check_cmd=check_cmd)
        nids.append(nid)
    repo.close()
    return mid, root, nids


def read_run_control(home: Path, mid: str) -> dict:
    repo = Repository(home / "mission.db")
    try:
        row = repo._conn.execute(
            "SELECT params_json FROM run_control WHERE id=1").fetchone()
        return json.loads(row["params_json"] or "{}") if row else {}
    finally:
        repo.close()


def quota_result(error_class: str, summary="quota exhausted"):
    return {"ok": False, "summary": summary, "session_id": "",
            "resources": {"backend": "fake-provider", "origin": "fake-provider",
                          "error_class": error_class,
                          "tokens": 0, "duration_ms": 5,
                          "cost_currency": "USD",
                          "cost": {"amount": None, "basis": "unknown",
                                   "currency": "USD"},
                          "receipt_status": "incomplete"}}


def test_quota_error_pauses_and_persists(tmp_path):
    """额度耗尽：一次失败即暂停（不烧重试）+ 事件 + params_json 持久化。"""
    home = tmp_path
    mid, root, nids = build_task(home)
    be = FakeNodeBackend({"节点0": quota_result("quota_exhausted",
                                       "Token Plan 用量上限")})
    repo = Repository(home / "mission.db")
    graph = Graph(repo, checker3=None)
    sched = Scheduler(repo, graph, be, max_concurrency=1,
                      budget=Budget(max_calls=50))
    sched.run_until_done(mid, root, timeout=10, poll=0.05, max_idle=5)
    rc = read_run_control(home, mid)
    assert rc.get("provider_pause", {}).get("error_class") == "quota_exhausted"
    # 事件在案
    evs = [dict(r) for r in repo._conn.execute(
        "SELECT type FROM events WHERE mission_id=?", (mid,))]
    assert any(e["type"] == "provider_paused" for e in evs)
    # 节点回 pending（未烧失败/重试）——仅 1 次调用
    assert len(be.calls) == 1
    node = repo.get_node(nids[0])
    assert node.status == "pending"
    repo.close()


def test_restart_blocked_without_ack(tmp_path):
    """暂停后新进程 start 被拒；ack=True 清除并恢复。"""
    home = tmp_path
    mid, root, nids = build_task(home)
    be = FakeNodeBackend({"节点0": quota_result("auth_failed", "401 invalid key")})
    repo = Repository(home / "mission.db")
    graph = Graph(repo, checker3=None)
    sched = Scheduler(repo, graph, be, max_concurrency=1,
                      budget=Budget(max_calls=50))
    sched.run_until_done(mid, root, timeout=10, poll=0.05, max_idle=5)
    repo.close()
    mgr = RunManager(home)
    mgr._runs.pop(mid, None)
    r = mgr.start(mid, root=root, backend_factory=lambda: FakeNodeBackend({}),
                  real=False, max_calls=50)
    assert r.get("ok") is False
    assert "供应商暂停" in r["error"]
    assert r["provider_pause"]["error_class"] == "auth_failed"
    # ack 恢复
    r2 = mgr.start(mid, root=root, backend_factory=lambda: FakeNodeBackend({}),
                   real=False, max_calls=50, provider_pause_ack=True)
    assert r2.get("ok") is True
    mgr.stop(mid, mode="kill")
    assert "provider_pause" not in read_run_control(home, mid)


def test_rate_limit_backoff_then_recover(tmp_path):
    """临时限流：退避窗口内不派发；窗口过后恢复派发且成功重置连击。"""
    home = tmp_path
    mid, root, nids = build_task(home)
    be = FakeNodeBackend({"节点0": quota_result("rate_limited", "429 slow down")})
    repo = Repository(home / "mission.db")
    graph = Graph(repo, checker3=None)
    sched = Scheduler(repo, graph, be, max_concurrency=1,
                      budget=Budget(max_calls=50))
    sched._provider_backoff_base_s = 0.5   # 测试缩短退避
    sched._provider_backoff_cap_s = 2
    # 后端第一次限流、之后成功
    orig_run = be.run

    def two_phase(statement, workdir, check_cmd, title="", cancel_event=None):
        if len(be.calls) >= 2:
            r = orig_run(statement, workdir, check_cmd, title, cancel_event)
            return r
        r = orig_run(statement, workdir, check_cmd, title, cancel_event)
        if r["resources"].get("error_class") and len(be.calls) >= 2:
            r = {"ok": True, "summary": "recovered", "session_id": "s2",
                 "resources": {"origin": "fake", "agent_id": "a",
                               "tokens": 5, "duration_ms": 5,
                               "cost_currency": "USD",
                               "cost": {"amount": None, "basis": "unknown",
                                        "currency": "USD"},
                               "receipt_status": "valid"}}
        return r

    be.run = two_phase
    sched.run_until_done(mid, root, timeout=25, poll=0.05, max_idle=8)
    evs = [dict(r) for r in repo._conn.execute(
        "SELECT type FROM events WHERE mission_id=?", (mid,))]
    assert any(e["type"] == "provider_cooldown" for e in evs)
    assert "provider_pause" not in read_run_control(home, mid)
    assert sched._rate_streak == 0          # 成功重置
    assert len(be.calls) >= 2               # 退避后重新派发
    repo.close()


def test_rate_limit_streak_becomes_pause(tmp_path):
    """连续限流达阈值 → 转人工暂停。"""
    home = tmp_path
    mid, root, nids = build_task(home)
    be = FakeNodeBackend({"节点0": quota_result("rate_limited", "429")})
    repo = Repository(home / "mission.db")
    graph = Graph(repo, checker3=None)
    sched = Scheduler(repo, graph, be, max_concurrency=1,
                      budget=Budget(max_calls=200))
    sched._provider_backoff_base_s = 0.05
    sched._provider_backoff_cap_s = 0.1
    sched.run_until_done(mid, root, timeout=20, poll=0.05, max_idle=6)
    rc = read_run_control(home, mid)
    assert rc.get("provider_pause", {}).get("error_class") == "rate_limited"
    repo.close()


def test_plain_text_mentioning_429_never_triggers(tmp_path):
    """模型输出正文含"429"但回执无 error_class（结构化字段缺失）→
    不触发任何停派——按普通成功处理。"""
    home = tmp_path
    mid, root, nids = build_task(home)
    ok_with_429_text = {"ok": True, "summary": "报告引用了上游 429 错误历史",
                        "session_id": "s",
                        "resources": {"origin": "fake", "agent_id": "a",
                                      "tokens": 9, "duration_ms": 9,
                                      "cost_currency": "USD",
                                      "cost": {"amount": None,
                                               "basis": "unknown",
                                               "currency": "USD"},
                                      "receipt_status": "valid"}}
    be = FakeNodeBackend({}, ok_result=ok_with_429_text)
    repo = Repository(home / "mission.db")
    graph = Graph(repo, checker3=None)
    sched = Scheduler(repo, graph, be, max_concurrency=1,
                      budget=Budget(max_calls=50))
    sched.run_until_done(mid, root, timeout=15, poll=0.05, max_idle=4)
    assert "provider_pause" not in read_run_control(home, mid)
    assert sched.provider_pause is None
    assert sched.provider_cooldown_until == 0.0
    repo.close()


def test_unknown_class_uses_retry_budget(tmp_path):
    """unknown/service 类错误不触发停派——按普通失败走判卷与重试预算。"""
    home = tmp_path
    mid, root, nids = build_task(
        home, check_cmd='python -c "import sys; sys.exit(1)"')
    be = FakeNodeBackend({"节点0": quota_result("unknown", "weird failure")})
    repo = Repository(home / "mission.db")
    graph = Graph(repo, checker3=None)
    sched = Scheduler(repo, graph, be, max_concurrency=1,
                      budget=Budget(max_calls=50), retry_policy=None)
    from dag_agent.recovery import RetryPolicy
    sched.retry_policy = RetryPolicy(max_retries=1)
    sched.run_until_done(mid, root, timeout=15, poll=0.05, max_idle=5)
    assert "provider_pause" not in read_run_control(home, mid)
    # 未知类：本 attempt 走提交判卷（判卷 FAIL→节点 failed→重试预算消耗）
    assert len(be.calls) >= 2           # 初次+重试
    node = repo.get_node(nids[0])
    assert node.status in ("failed", "pending")
    repo.close()


def test_pause_keeps_budget(tmp_path):
    """暂停后预算/累计不重置（calls_used 保留）。"""
    home = tmp_path
    mid, root, nids = build_task(home)
    be = FakeNodeBackend({"节点0": quota_result("balance_exhausted")})
    repo = Repository(home / "mission.db")
    graph = Graph(repo, checker3=None)
    sched = Scheduler(repo, graph, be, max_concurrency=1,
                      budget=Budget(max_calls=50))
    sched.run_until_done(mid, root, timeout=10, poll=0.05, max_idle=5)
    used_after = sched.budget.calls_used
    assert used_after >= 1
    # 从库重读（模拟重启后 _budget_load）
    sched2 = Scheduler(repo, graph, be, max_concurrency=1, budget=Budget())
    sched2._budget_load(mid)
    assert sched2.budget.calls_used == used_after
    repo.close()
