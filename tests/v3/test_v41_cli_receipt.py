# -*- coding: utf-8 -*-
"""v3.11 阶段 1A 回归：CLI 回执契约接入 scheduler 正式路径。

任务书 1A 判据：必须通过 scheduler 的真实后端分支（假 CLI 子进程）证明
契约生效，不能只直接调用校验函数。覆盖：正常结果、归属错配、异常资源值、
部分结果、重复结算及重启读取；多模型费用不取首项、不重复累加汇总。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from dag_agent.cc_backend import ClaudeCliBackend
from dag_agent.executor_contract import (ContractError, record_execution)
from dag_agent.graph import Graph
from dag_agent.repository import Repository
from dag_agent.scheduler import Budget, Scheduler

SHIM = str(Path(__file__).parent / "fake_claude_shim.py")
CHECKER = str(Path(__file__).resolve().parents[2] / "dag_agent" / "checkers"
              / "check_file_contains.py")
BRIEF = ("【输入】无/【交付清单】hello.txt 内容 DAG-OK/【接口签名】文件交付"
         "/【样例】DAG-OK/【自测入口】cat hello.txt")


def _env_file(tmp_path):
    p = tmp_path / "env.local"
    p.write_text("ANTHROPIC_BASE_URL=https://fake\n"
                 "ANTHROPIC_AUTH_TOKEN=dummy\n", encoding="utf-8")
    return p


def _cli_backend(tmp_path):
    return ClaudeCliBackend(cli_path=SHIM, env_file=_env_file(tmp_path),
                            timeout=60)


ROOT_BRIEF = ("【输入】inputs/ 下叶子交付副本/【交付清单】hello.txt 内容"
              " DAG-OK/【接口签名】文件交付/【样例】DAG-OK/【自测入口】"
              "cat hello.txt")


def _run_scheduler(tmp_path, mode=None):
    """Scheduler 真实后端分支（假 CLI 子进程）跑一个叶子节点到结算。
    root 语句同为五段简报（真实后端派发 root 同过 A3 闸门）。"""
    os.environ["FAKE_MODE"] = mode or "deliver"
    repo = Repository(tmp_path / "m" / "mission.db")
    try:
        g = Graph(repo)
        be = _cli_backend(tmp_path)
        mid, root = g.create_mission("CLI回执", brief=ROOT_BRIEF,
                                     root_check=f'python "{CHECKER}" hello.txt DAG-OK',
                                     root_declared_artifacts=("hello.txt",))
        nid = g.add_node(mid, "L", BRIEF, parent_id=root,
                         check_cmd=f'python "{CHECKER}" hello.txt DAG-OK',
                         declared_artifacts=("hello.txt",))
        s = Scheduler(repo, g, be, max_concurrency=1, reap_enabled=False,
                      budget=Budget())
        summary = s.run_until_done(mid, root, timeout=120, poll=0.05)
        return repo, g, mid, nid, s, summary
    finally:
        os.environ.pop("FAKE_MODE", None)


def _res(repo, nid):
    row = repo._conn.execute(
        "SELECT resources_json FROM attempts WHERE node_id=?"
        " ORDER BY started_at DESC LIMIT 1", (nid,)).fetchone()
    return json.loads(row["resources_json"] or "{}")


def test_scheduler_cli_receipt_valid_contract(tmp_path):
    """正常结果：契约字段齐、UTC 起止、usage 原始字段、费用 reported、
    budget 记账、无告警事件。"""
    repo, g, mid, nid, s, summary = _run_scheduler(tmp_path)
    assert summary["mission_status"] == "done", summary
    r = _res(repo, nid)
    for k in ("backend", "provider_model", "session_id",
              "executor_started_at", "executor_ended_at", "duration_ms",
              "usage_raw", "cost", "receipt_status", "settled_valid"):
        assert k in r, f"缺契约字段 {k}: {sorted(r)}"
    assert r["backend"] == "claude-cli"
    assert r["session_id"] == "fake-sess-0001"
    assert r["receipt_status"] == "valid" and r["settled_valid"] is True
    assert "+" in r["executor_started_at"]  # UTC 带时区
    assert r["duration_ms"] >= 0
    assert r["usage_raw"] == {"input_tokens": 10, "output_tokens": 5}
    assert r["cost"] == {"amount": 0.01, "basis": "reported",
                         "currency": "USD"}
    bj = json.loads(repo.get_mission(mid).budget_json)
    # 叶子+root 两次真实 CLI 派发（root 同过闸门），各 $0.01 如实累计
    assert bj["calls_used"] == 2 and abs(bj["cost_used"] - 0.02) < 1e-9
    warns = [e for e in repo.events_of(mid)
             if e.type == "executor_contract_warning"]
    assert warns == []
    repo.close()


def test_receipt_wrong_owner_rejected(tmp_path):
    """归属错配：未结算 attempt 的回执记到别的任务/节点 → ContractError，
    数据不变。（错配校验为契约层语义——真实后端分支由 valid/partial/
    duplicate/restart/multi 五测覆盖；此处独立任务构造未结算载体。）"""
    repo = Repository(tmp_path / "own" / "mission.db")
    other = Repository(tmp_path / "other" / "mission.db")
    try:
        g = Graph(repo)
        mid, root = g.create_mission("本任务")
        nid = g.add_node(mid, "L", BRIEF, parent_id=root)
        g2 = Graph(other)
        mid2, root2 = g2.create_mission("别的任务")
        nid2 = g2.add_node(mid2, "L2", BRIEF, parent_id=root2)
        fresh = g.start_attempt(mid, nid, backend="stub")
        before = repo.get_attempt(fresh).resources_json
        with pytest.raises(ContractError):
            record_execution(repo, fresh, {"origin": "claude-cli",
                                           "tokens": 1},
                             mid=mid2, nid=nid2)
        assert repo.get_attempt(fresh).resources_json == before  # 数据不变
        # 正确归属可入账（对照）
        ok = record_execution(repo, fresh, {"origin": "claude-cli",
                                            "agent_id": "a1", "tokens": 3,
                                            "duration_ms": 5}, mid=mid, nid=nid)
        assert ok["ok"] is True and ok["replayed"] is False
    finally:
        other.close()
        repo.close()


def _attempt_of(repo, nid):
    return repo._conn.execute(
        "SELECT id FROM attempts WHERE node_id=? ORDER BY started_at DESC"
        " LIMIT 1", (nid,)).fetchone()["id"]


def test_receipt_abnormal_values_rejected(tmp_path):
    """异常资源值（负 tokens / NaN 时长 / 逆序起止 / cost 非结构化）→
    拒收，账目不变。（契约层语义，载体=独立未结算任务。）"""
    repo = Repository(tmp_path / "m" / "mission.db")
    try:
        g = Graph(repo)
        mid, root = g.create_mission("异常值")
        nid = g.add_node(mid, "L", BRIEF, parent_id=root)
        aid = g.start_attempt(mid, nid, backend="stub")
        before = json.loads(repo.get_attempt(aid).resources_json or "{}")
        for bad in ({"tokens": -5}, {"duration_ms": float("nan")},
                    {"executor_started_at": "2026-10-06T10:00:00+00:00",
                     "executor_ended_at": "2026-10-06T09:00:00+00:00"},
                    {"cost": 0.5}):
            bad = {"resources": bad}  # run_result 包装层
            with pytest.raises(ContractError):
                record_execution(repo, aid, bad, mid=mid, nid=nid)
        assert json.loads(repo.get_attempt(aid).resources_json or "{}") == before
    finally:
        repo.close()


def test_scheduler_partial_receipt_incomplete(tmp_path):
    """部分结果（输出不可解析）：receipt_status=incomplete、费用 unknown、
    budget 不按 0 冒充已确认（cost_used 不动，unknown 计数入账）。"""
    repo, g, mid, nid, s, summary = _run_scheduler(tmp_path, mode="garbage")
    assert summary["mission_status"] == "active"  # fail 后无重试（死局停机）
    r = _res(repo, nid)
    assert r["receipt_status"] == "incomplete"
    assert r["cost"]["basis"] == "unknown" and r["cost"]["amount"] is None
    bj = json.loads(repo.get_mission(mid).budget_json)
    assert bj.get("cost_used", 0) == 0
    assert bj.get("cost_unknown", 0) >= 1
    repo.close()


def test_duplicate_settlement_replayed_no_double_charge(tmp_path):
    """重复结算：同一 attempt 同一回执再次处理 → replayed，budget 不重复计费。"""
    repo, g, mid, nid, s, summary = _run_scheduler(tmp_path)
    aid = _attempt_of(repo, nid)
    bj0 = json.loads(repo.get_mission(mid).budget_json)
    again = record_execution(repo, aid, {"origin": "claude-cli",
                                         "tokens": 1}, mid=mid, nid=nid)
    assert again.get("replayed") is True
    bj1 = json.loads(repo.get_mission(mid).budget_json)
    assert bj1["cost_used"] == bj0["cost_used"]
    assert bj1["calls_used"] == bj0["calls_used"]
    repo.close()


def test_restart_budget_restored_after_cli_settlement(tmp_path):
    """重启读取：CLI 分支结算后重建 Repository，budget 从 budget_json 恢复不归零。"""
    repo, g, mid, nid, s, summary = _run_scheduler(tmp_path)
    db_path = repo.db_path
    repo.close()
    repo2 = Repository(db_path)
    try:
        bj = json.loads(repo2.get_mission(mid).budget_json)
        assert bj["calls_used"] >= 1
        assert abs(bj["cost_used"] - 0.02) < 1e-9
    finally:
        repo2.close()


def test_multi_model_cost_not_first_only(tmp_path):
    """多模型费用：汇总一致→reported 汇总值；缺汇总→分项求和 estimated；
    分项无数值→unknown。绝不只取首项、不重复累加汇总。"""
    repo, g, mid, nid, s, summary = _run_scheduler(tmp_path, mode="multi")
    r = _res(repo, nid)
    assert r["cost"] == {"amount": 0.05, "basis": "reported",
                         "currency": "USD"}
    repo.close()

    repo2, g2, mid2, nid2, s2, sum2 = _run_scheduler(
        tmp_path / "b", mode="multi_nototal")
    r2 = _res(repo2, nid2)
    assert r2["cost"]["amount"] == 0.05 and r2["cost"]["basis"] == "estimated"
    repo2.close()

    repo3, g3, mid3, nid3, s3, sum3 = _run_scheduler(
        tmp_path / "c", mode="multi_nonumeric")
    r3 = _res(repo3, nid3)
    assert r3["cost"]["basis"] == "unknown" and r3["cost"]["amount"] is None
    repo3.close()
