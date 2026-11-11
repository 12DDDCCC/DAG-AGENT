# -*- coding: utf-8 -*-
"""R6 Pi 不完整流/并发状态 + 平台结算门禁回归（v63，第二轮）。

- 解析错误/截断 → 不默认成功（incomplete，ok=False）；
- 部分费用缺失 → cost_complete=False + receipt incomplete（保留小计）；
- 矛盾终态（stopReason=aborted vs settled.aborted=false）→ aborted；
- 未知 stopReason → incomplete；
- 重复 message_end 按 responseId 稳定标识去重（不重复累计）；
- 共享 backend 并发：executor_started_at/session/tokens 各自独立；
- 非法 input 分量影响完整性；
- 平台级：适配器标 incomplete 的回执，executor_contract 不判 valid
  → 自动拆分门禁拒绝。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from dag_agent.executor_contract import record_execution  # noqa: E402
from dag_agent.pi_backend import PiBackend  # noqa: E402
from dag_agent.pi_events import PiEventStream  # noqa: E402
from dag_agent.repository import Repository  # noqa: E402
from tests.v3.test_v56_pi_backend import (SESSION, SETTLED_OK, full_run,  # noqa: E402
                                          make_backend, make_fake_cli, me,
                                          msg, run_case)


def test_parse_error_downgrades_outcome(tmp_path):
    """正常消息间夹一条损坏 JSON → 不默认成功（incomplete，ok=False）。"""
    events = full_run([msg(100, 1)])
    cli = make_fake_cli(tmp_path, events)
    # 在 fake 输出开头插入一行非 JSON（模拟日志混流进 stdout）
    lines = cli.read_text(encoding="utf-8").splitlines(True)
    out = []
    for ln in lines:
        out.append(ln)
        if ln.startswith('process.stdin.on("end"'):
            out.append('console.log("{{broken json");\n')
    cli.write_text("".join(out), encoding="utf-8")
    b = make_backend(cli)
    r = b.run("stmt", tmp_path / "wd", None)
    assert r["ok"] is False, "解析错误不得默认成功"
    assert r["resources"]["session_outcome"] == "incomplete"
    assert r["resources"]["diagnostics"]["parse_errors"] >= 1
    assert r["resources"]["receipt_status"] == "incomplete"


def test_partial_cost_marks_incomplete(tmp_path):
    """两条消息只有第一条有 cost → 小计保留 + cost_complete=False +
    receipt incomplete（不再无声吞掉缺失部分）。"""
    no_cost = msg(200, None)   # usage 有 totalTokens、无 cost
    r = run_case(tmp_path, full_run([msg(100, 1), no_cost]))
    cost = r["resources"]["cost"]
    assert cost["amount"] == 1.0            # 已知小计
    assert cost["basis"] == "estimated"
    assert cost["cost_complete"] is False
    assert cost["cost_missing_messages"] == 1
    assert "全程费用未知" in cost.get("cost_note", "")
    assert r["resources"]["receipt_status"] == "incomplete"


def test_contradictory_abort_terminal(tmp_path):
    """stopReason=aborted 与 settled.aborted=false 矛盾 → aborted（不
    默认 completed）。"""
    events = [SESSION, {"type": "agent_start"},
              me(msg(10, 0.1, text="partial", stopReason="aborted")),
              {"type": "agent_end", "messages": [], "willRetry": False},
              {"type": "agent_settled", "aborted": False}]
    r = run_case(tmp_path, events)
    assert r["ok"] is False
    assert r["resources"]["session_outcome"] == "aborted"


def test_unknown_stop_reason_incomplete(tmp_path):
    events = full_run([msg(10, 0.1, stopReason="quantum")])
    r = run_case(tmp_path, events)
    assert r["ok"] is False
    assert r["resources"]["session_outcome"] == "incomplete"


def test_duplicate_message_end_dedup_by_response_id(tmp_path):
    """同 responseId 的重复 message_end：统计一次，重复计数留痕，
    tokens 不重复累计（稳定标识去重，非正文比对）。"""
    dup1 = msg(100, 1)
    dup2 = msg(100, 1)
    dup2["responseId"] = dup1["responseId"]   # 显式同响应ID=重复事件
    events = full_run([dup1, dup2])
    r = run_case(tmp_path, events)
    assert r["resources"]["tokens"] == 100          # 不重复累计
    assert r["resources"]["diagnostics"]["duplicate_message_ends"] == 1
    # 缺 responseId 的消息各算一次（两条不同 usage）
    ev2 = [SESSION, {"type": "agent_start"},
           me(msg(10, 0.1)), me(msg(20, 0.2)),
           {"type": "agent_end", "messages": [], "willRetry": False},
           SETTLED_OK]
    for e in ev2:
        pass
    stream = PiEventStream()
    for e in ev2:
        m = e.get("message") if e.get("type") == "message_end" else None
        if m is not None:
            m.pop("responseId", None)
        stream.feed(json.dumps(e) + "\n")
    stream.close()
    assert stream.stats.tokens_total == 30
    assert stream.stats.messages_total == 2


def test_invalid_component_affects_completeness(tmp_path):
    """input=-5（非法分量）→ usage_complete=False + receipt incomplete。"""
    bad = msg(100, 1)
    bad["usage"]["input"] = -5
    r = run_case(tmp_path, full_run([bad]))
    assert r["resources"]["usage_stats"]["usage_complete"] is False
    assert r["resources"]["receipt_status"] == "incomplete"
    assert r["resources"]["diagnostics"]["usage_component_errors"] == 1


def test_shared_backend_concurrent_independence(tmp_path):
    """同一 PiBackend 并发 run：executor_started_at/session/tokens 各自
    独立（修复前共享 self._started 被覆盖成相同值）。"""
    shared = make_backend(make_fake_cli(tmp_path / "shared-cli",
                                        full_run([msg(111, 0.1)])))
    results = {}

    def _one(name, tokens, delay):
        time.sleep(delay)
        cli = make_fake_cli(tmp_path / name, full_run([msg(tokens, 0.1)]))
        # 共享实例：用同一 backend 对象跑不同目录（构造时 CLI 可换——
        # 直接改 cli 路径验证状态隔离而非配置隔离）
        b = shared
        import dataclasses
        saved = b.cli
        b.cli = str(cli)
        try:
            results[name] = b.run("stmt", tmp_path / name / "wd", None)
        finally:
            b.cli = saved

    ths = [threading.Thread(target=_one, args=("a", 111, 0.0)),
           threading.Thread(target=_one, args=("b", 222, 0.18))]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout=30)
    ra, rb = results["a"], results["b"]
    assert ra["resources"]["tokens"] == 111
    assert rb["resources"]["tokens"] == 222
    # 并发窗口内两个 attempt 的起始时间不互为覆盖（独立局部状态）
    assert ra["resources"]["executor_started_at"] \
        != rb["resources"]["executor_started_at"]


def test_platform_settlement_rejects_incomplete(tmp_path):
    """平台级：适配器 receipt_status=incomplete + usage_complete=False
    的回执，executor_contract 不得判 valid → 拆分门禁拒绝。"""
    home = Path(tmp_path)
    repo = Repository(home / "mission.db")
    from dag_agent.graph import Graph
    from dag_agent.assessment import default_checker3
    graph = Graph(repo, checker3=default_checker3)
    mid, root = graph.create_mission("R6门禁", "简单任务",
                                     root_check="python -c pass")
    nid = graph.add_node(mid, "n", "陈述", parent_id=root,
                         check_cmd="python -c pass")
    aid = graph.start_attempt(mid, nid, backend="stub")
    incomplete_receipt = {
        "ok": False,
        "summary": "会话未确认完成",
        "session_id": "sess-x",
        "resources": {"backend": "pi-cli", "origin": "pi-cli",
                      "agent_id": "sess-x", "tokens": 100,
                      "duration_ms": 5, "cost_currency": "USD",
                      "cost": {"amount": None, "basis": "unknown",
                               "currency": "USD", "cost_complete": False},
                      "receipt_status": "incomplete",
                      "session_outcome": "incomplete",
                      "usage_stats": {"messages_total": 2,
                                      "messages_counted": 1,
                                      "tokens_sum": 100,
                                      "usage_complete": False},
                      "executor_started_at": "2026-11-08T12:00:00.000+00:00",
                      "executor_ended_at": "2026-11-08T12:00:05.000+00:00",
                      "exit_code": 0, "cancelled": False, "timeout": False}}
    rec = record_execution(repo, aid, incomplete_receipt, mid=mid, nid=nid)
    assert rec["contract_status"] in ("legacy_incomplete",), rec
    assert rec.get("contract_status") != "valid"
    # 自动拆分门禁语义：session_ok=False → 不受理提案
    session_ok = (incomplete_receipt.get("ok") is True
                  and rec.get("contract_status") == "valid")
    assert session_ok is False
    repo.close()
