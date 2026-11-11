# -*- coding: utf-8 -*-
"""R5 供应商分类/退避持久性 + 调度循环 idle 回归（v62，第二轮）。

- 分类：429+quota→quota、429+balance→balance（业务优先于 HTTP 状态）；
  供应商命名空间数字码；未知 provider 不用数字码；
- Retry-After：数字/文本秒/HTTP 日期/非法值；
- 冷却与连续次数跨进程持久（params_json→restore_provider_state）；
- 并发成功不清除活跃冷却；
- idle 修复确定性：真正无进展按 max_idle 结束；长在途不误判；
  冷却等待不消耗阈值；预算停派不被 idle 掩盖。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys = __import__("sys")
sys.path.insert(0, str(ROOT))
from dag_agent.pi_events import (classify_provider_error,  # noqa: E402
                                 parse_retry_after_hint)
from dag_agent.scheduler import Budget, Scheduler  # noqa: E402
from dag_agent.repository import Repository  # noqa: E402
from dag_agent.graph import Graph  # noqa: E402
from tests.v3.test_v57_provider_pause import (FakeNodeBackend,  # noqa: E402
                                              build_task, quota_result,
                                              read_run_control)


def test_business_semantics_beat_http_status():
    assert classify_provider_error(
        "429 Token Plan usage limit exceeded (2056)") == "quota_exhausted"
    assert classify_provider_error(
        "429 insufficient balance") == "balance_exhausted"
    assert classify_provider_error("401 invalid api key") == "auth_failed"
    assert classify_provider_error("429 too many requests") == "rate_limited"
    assert classify_provider_error("529 overloaded") == "rate_limited"
    # 供应商命名空间：glm/bigmodel 的 2056/1113=配额、1002=余额
    assert classify_provider_error("error 2056", provider="glm") \
        == "quota_exhausted"
    assert classify_provider_error("error 1002", provider="glm") \
        == "balance_exhausted"
    # 其他 provider 不解释 bigmodel 数字码（厂商同号隔离）
    assert classify_provider_error("error 2056", provider="other-vendor") \
        == "unknown"


def test_retry_after_hint_parsing():
    assert parse_retry_after_hint("retry after 7 seconds") == 7.0
    assert parse_retry_after_hint("Retry-After: 12") == 12.0
    assert parse_retry_after_hint("wait 3s") == 3.0
    dt = (datetime.now(timezone.utc) + timedelta(seconds=30)).strftime(
        "%a, %d %b %Y %H:%M:%S GMT")
    v = parse_retry_after_hint(f"Retry-After: {dt}")
    assert v is not None and 0 < v <= 60
    assert parse_retry_after_hint("retry after -5") is None
    assert parse_retry_after_hint("no hint here") is None


def test_rate_limit_uses_retry_after_hint(tmp_path):
    """有效 Retry-After 优先（退避=提示值而非指数）；事件留痕标记。"""
    home = tmp_path
    mid, root, nids = build_task(home)
    r = quota_result("rate_limited", "429 slow down. Retry-After: 9")
    r["resources"]["retry_after_s"] = 9.0
    be = FakeNodeBackend({"节点0": r})
    repo = Repository(home / "mission.db")
    graph = Graph(repo, checker3=None)
    sched = Scheduler(repo, graph, be, max_concurrency=1,
                      budget=Budget(max_calls=50))
    sched._provider_backoff_base_s = 60
    sched.run_until_done(mid, root, timeout=8, poll=0.05, max_idle=4)
    evs = [dict(x) for x in repo._conn.execute(
        "SELECT payload_json FROM events WHERE type='provider_cooldown'")]
    assert evs and json.loads(evs[0]["payload_json"]).get("backoff_s") == 9.0
    assert json.loads(evs[0]["payload_json"]).get("retry_after_used") is True
    repo.close()


def test_provider_state_survives_reconstruction(tmp_path):
    """连续次数与冷却截止持久化：新 Scheduler 实例恢复后不清零。"""
    home = tmp_path
    mid, root, nids = build_task(home)
    be = FakeNodeBackend({"节点0": quota_result("rate_limited", "429")})
    repo = Repository(home / "mission.db")
    graph = Graph(repo, checker3=None)
    sched = Scheduler(repo, graph, be, max_concurrency=1,
                      budget=Budget(max_calls=50))
    sched._provider_backoff_base_s = 120
    sched._provider_backoff_cap_s = 120
    sched.run_until_done(mid, root, timeout=8, poll=0.05, max_idle=4)
    old_streak = sched._rate_streak
    old_cooldown = sched.provider_cooldown_until
    assert old_streak >= 1 and old_cooldown > time.monotonic()
    saved = read_run_control(home, mid)
    assert saved.get("provider_cooldown", {}).get("rate_streak") == old_streak
    # 重建调度器（模拟新进程）
    sched2 = Scheduler(repo, graph, be, max_concurrency=1,
                       budget=Budget(max_calls=50))
    assert sched2._rate_streak == 0
    assert sched2.provider_cooldown_until == 0.0
    sched2.restore_provider_state(saved)
    assert sched2._rate_streak == old_streak
    assert sched2.provider_cooldown_until > time.monotonic()  # 仍遵守冷却
    repo.close()


def test_concurrent_success_keeps_active_cooldown(tmp_path):
    """冷却活跃期内另一节点成功 → 冷却与连击不被无条件清除。"""
    home = tmp_path
    mid, root, nids = build_task(home)
    be = FakeNodeBackend({}, ok_result={
        "ok": True, "summary": "ok", "session_id": "s",
        "resources": {"origin": "stub", "agent_id": "a", "tokens": 1,
                      "duration_ms": 1, "cost_currency": "USD",
                      "cost": {"amount": None, "basis": "unknown",
                               "currency": "USD"},
                      "receipt_status": "valid"}})
    repo = Repository(home / "mission.db")
    graph = Graph(repo, checker3=None)
    sched = Scheduler(repo, graph, be, max_concurrency=1,
                      budget=Budget(max_calls=50))
    sched.provider_cooldown_until = time.monotonic() + 120
    sched._rate_streak = 3
    sched.run_until_done(mid, root, timeout=8, poll=0.05, max_idle=4)
    assert sched.provider_cooldown_until > time.monotonic()  # 冷却保留
    assert sched._rate_streak == 3                            # 连击保留
    repo.close()


def _quiet_backend(tmp_path):
    """成功但不产交付的桩（判卷 exit0）。"""
    return FakeNodeBackend({}, ok_result={
        "ok": True, "summary": "ok", "session_id": "s",
        "resources": {"origin": "stub", "agent_id": "a", "tokens": 1,
                      "duration_ms": 1, "cost_currency": "USD",
                      "cost": {"amount": None, "basis": "unknown",
                               "currency": "USD"},
                      "receipt_status": "valid"}})


def test_idle_terminates_true_no_progress(tmp_path):
    """真正无可推进（全部 accepted 但 mission 仍 active）→ 按 max_idle
    结束并给出无进展 halt（修复前 idle 被清零永不触发）。"""
    home = tmp_path
    mid, root, nids = build_task(home)
    repo = Repository(home / "mission.db")
    with repo.tx() as cur:
        cur.execute("UPDATE nodes SET status='accepted' WHERE mission_id=?",
                    (mid,))
        cur.execute("UPDATE missions SET status='active' WHERE id=?",
                    (mid,))
    graph = Graph(repo, checker3=None)
    be = _quiet_backend(tmp_path)
    sched = Scheduler(repo, graph, be, max_concurrency=1,
                      budget=Budget(max_calls=50))
    t0 = time.monotonic()
    sched.run_until_done(mid, root, timeout=30, poll=0.02, max_idle=5)
    elapsed = time.monotonic() - t0
    assert "无进展" in (sched.halt_reason or ""), sched.halt_reason
    assert elapsed < 10   # max_idle=5×0.02s 级别（修复前会拖到 timeout=30s）
    repo.close()


def test_long_inflight_not_misjudged_idle(tmp_path):
    """长在途任务（~1.5s）期间不误判无进展；完成正常闭合。"""
    home = tmp_path
    mid, root, nids = build_task(home)

    class SlowBackend(FakeNodeBackend):
        def run(self, statement, workdir, check_cmd, title="",
                cancel_event=None):
            time.sleep(1.5)
            return super().run(statement, workdir, check_cmd, title,
                               cancel_event)

    repo = Repository(home / "mission.db")
    graph = Graph(repo, checker3=None)
    be = SlowBackend({}, ok_result={
        "ok": True, "summary": "ok", "session_id": "s",
        "resources": {"origin": "stub", "agent_id": "a", "tokens": 1,
                      "duration_ms": 1500, "cost_currency": "USD",
                      "cost": {"amount": None, "basis": "unknown",
                               "currency": "USD"},
                      "receipt_status": "valid"}})
    sched = Scheduler(repo, graph, be,
                      max_concurrency=1, budget=Budget(max_calls=50))
    t0 = time.monotonic()
    sched.run_until_done(mid, root, timeout=25, poll=0.05, max_idle=5)
    elapsed = time.monotonic() - t0
    assert elapsed >= 5.5, f"长在途应实际执行（{elapsed:.1f}s）"
    # 核心：1.5s 长在途窗口不被 idle 误杀——全部节点（3子+1整合）都
    # 被执行（idle 红绿由 test_idle_terminates_true_no_progress 覆盖）
    assert len(be.calls) == 4, f"节点应全部执行（实际 {be.calls}）"
    repo.close()


def test_cooldown_wait_not_counted_idle(tmp_path):
    """冷却等待不计入无进展阈值：冷却短退避后恢复派发成功。"""
    home = tmp_path
    mid, root, nids = build_task(home)
    be = FakeNodeBackend({"节点0": quota_result("rate_limited", "429")})
    repo = Repository(home / "mission.db")
    graph = Graph(repo, checker3=None)
    sched = Scheduler(repo, graph, be, max_concurrency=1,
                      budget=Budget(max_calls=50))
    sched._provider_backoff_base_s = 0.3
    sched._provider_backoff_cap_s = 0.6
    orig_run = be.run

    def two_phase(statement, workdir, check_cmd, title="", cancel_event=None):
        r = orig_run(statement, workdir, check_cmd, title, cancel_event)
        if r["resources"].get("error_class") and len(be.calls) >= 2:
            r = {"ok": True, "summary": "recovered", "session_id": "s2",
                 "resources": {"origin": "stub", "agent_id": "a",
                               "tokens": 5, "duration_ms": 5,
                               "cost_currency": "USD",
                               "cost": {"amount": None, "basis": "unknown",
                                        "currency": "USD"},
                               "receipt_status": "valid"}}
        return r

    be.run = two_phase
    sched.run_until_done(mid, root, timeout=20, poll=0.05, max_idle=5)
    assert sched._rate_streak == 0   # 退避后成功且冷却已过期 → 重置
    assert "无进展" not in (sched.halt_reason or "")
    assert len(be.calls) >= 2
    repo.close()


def test_budget_halt_not_masked_by_idle(tmp_path):
    """预算熔断（max_calls=0）→ 立即 halt，不被 idle 等待拖延。"""
    home = tmp_path
    mid, root, nids = build_task(home)
    repo = Repository(home / "mission.db")
    graph = Graph(repo, checker3=None)
    sched = Scheduler(repo, graph, _quiet_backend(tmp_path),
                      max_concurrency=1, budget=Budget(max_calls=0))
    t0 = time.monotonic()
    sched.run_until_done(mid, root, timeout=20, poll=0.02, max_idle=50)
    assert time.monotonic() - t0 < 5
    assert "熔断" in (sched.halt_reason or "")
    repo.close()
