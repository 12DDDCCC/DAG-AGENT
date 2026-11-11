# -*- coding: utf-8 -*-
"""R2 全通道脱敏回归（v60）：协议错误通道（errorMessage/finalError/正文/
非法字段/stderr）中的虚构凭据标记不得出现在返回对象任何位置；正常非敏感
诊断保留。只用虚构标记，不读取真实凭据。"""
from __future__ import annotations

import json
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.v3.test_v56_pi_backend import (full_run, make_backend,  # noqa: E402
                                          make_fake_cli, msg)

MARKER = "SK-VICTIM-FIXTURE-KEY-000111222333"


def assert_no_marker(obj, path="$"):
    """递归断言整个返回对象不含标记；返回收集到的键路径（验证正常
    诊断仍在）。"""
    text = json.dumps(obj, ensure_ascii=False)
    assert MARKER not in text, f"标记泄漏于返回对象: {path}"


def test_marker_in_error_message_and_retries(tmp_path):
    err_msg = {"role": "assistant", "content": [],
               "stopReason": "error",
               "errorMessage": f"429 upstream echoed {MARKER}",
               "responseId": "r1"}
    events = [SESSION_HDR, {"type": "agent_start"},
              {"type": "auto_retry_start", "attempt": 1, "maxAttempts": 3,
               "delayMs": 5, "errorMessage": f"retry saw {MARKER}"},
              {"type": "auto_retry_end", "success": False, "attempt": 3,
               "finalError": f"final {MARKER}"},
              {"type": "message_end", "message": err_msg},
              {"type": "agent_end", "messages": [], "willRetry": False},
              {"type": "agent_settled", "aborted": False}]
    cli = make_fake_cli(tmp_path, events)
    b = make_backend(cli)
    b.extra_env = {"BIGMODEL_API_KEY": MARKER}   # 后端已知凭据（真实威胁模型）
    b.auth_ready = True
    r = b.run("stmt", tmp_path / "wd", None)
    assert r["ok"] is False
    assert_no_marker(r)
    # 正常非敏感诊断保留：错误类型/分类/重试结构仍在
    res = r["resources"]
    assert res["error_class"] in ("rate_limited", "quota_exhausted")
    assert res["diagnostics"]["retries"], "重试诊断不应被无差别删除"
    assert res["diagnostics"]["retries"][0]["type"] == "auto_retry_start"
    assert "429" in r["summary"] or "429" in json.dumps(res["diagnostics"])


def test_marker_in_result_text_and_invalid_fields(tmp_path):
    bad = {"role": "assistant", "content": [{"type": "text",
                                             "text": f"正文 {MARKER}"}],
           "stopReason": "stop",
           "usage": {"totalTokens": -7, "input": f"{MARKER}"},
           "responseId": "r2"}
    events = [SESSION_HDR, {"type": "agent_start"},
              {"type": "message_end", "message": bad},
              {"type": "agent_end", "messages": [], "willRetry": False},
              {"type": "agent_settled", "aborted": False}]
    cli = make_fake_cli(tmp_path, events)
    b = make_backend(cli)
    b.extra_env = {"BIGMODEL_API_KEY": MARKER}
    b.auth_ready = True
    r = b.run("stmt", tmp_path / "wd", None)
    assert_no_marker(r)
    res = r["resources"]
    # 非法分量如实留痕（类型级），值本身被脱敏
    assert res["usage_stats"]["usage_complete"] is False
    assert res["diagnostics"]["usage_component_errors"] >= 1


def test_marker_in_stderr_and_envvalue(tmp_path):
    """stderr 中的标记 + env 注入值本身出现在输出 → 均脱敏。"""
    events = full_run([msg(50, 0.5, text=f"done with {MARKER}")])
    cli = make_fake_cli(tmp_path, events, exit_code=1,
                        stderr_extra=f"trace {MARKER}")
    from dag_agent.pi_backend import PiBackend
    b = PiBackend(cli_path=str(cli), timeout=20)
    b.extra_env = {"BIGMODEL_API_KEY": MARKER}
    b.auth_ready = True
    r = b.run("stmt", tmp_path / "wd", None)
    assert_no_marker(r)
    # 正常统计仍在
    assert r["resources"]["tokens"] == 50
    assert r["resources"]["diagnostics"]["stderr_tail"]


SESSION_HDR = {"type": "session", "version": 3, "id": "sess-redact",
               "timestamp": "2026-11-08T00:00:00.000Z", "cwd": "/w"}


def test_marker_split_across_chunks(tmp_path):
    """标记跨读取块边界（7 字节/块）→ 增量脱敏出口仍全覆盖。"""
    err_msg = {"role": "assistant", "content": [],
               "stopReason": "error",
               "errorMessage": f"quota hit {MARKER} tail",
               "responseId": "r3"}
    events = [SESSION_HDR, {"type": "agent_start"},
              {"type": "message_end", "message": err_msg},
              {"type": "agent_end", "messages": [], "willRetry": False},
              {"type": "agent_settled", "aborted": False}]
    cli = make_fake_cli(tmp_path, events)
    b = make_backend(cli)
    b.extra_env = {"BIGMODEL_API_KEY": MARKER}
    b.auth_ready = True
    r = b.run("stmt", tmp_path / "wd", None)
    assert_no_marker(r)
