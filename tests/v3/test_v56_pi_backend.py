# -*- coding: utf-8 -*-
"""Pi 后端修复回归（v56，2026-11-08 独立复查 P1×3 关闭条件）。

覆盖（任务书 §三/§四/§五"必须覆盖"逐项）：
- 用量：多轮文本/纯工具调用/重复事件去重/重试与错误前用量/缺 usage/
  非法值/超尾部缓冲/跨读取块分割/Unicode 分隔符/无 LF 结尾；
- 费用：本地估算（estimated）≠供应商报告≠unknown；零价不冒充；
- 终态：先正文后 error(exit0)/aborted/length/缺 settled/非零退出/缺身份；
- 管道：stdin 独立线程/超时杀进程树/预取消/并发不串会话/诊断脱敏。

全部离线 fake CLI，零模型调用。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from dag_agent.pi_backend import PiBackend
from dag_agent.pi_events import PiEventStream, classify_provider_error


# ---- fake CLI 工具 ----

def _js_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def make_fake_cli(folder: Path, events: list[dict], *, exit_code: int = 0,
                  hang: bool = False, stderr_extra: str = "") -> Path:
    """生成假 pi CLI：--version 应答；stdin 结束后逐条输出事件（LF 分帧）。
    hang=True 时输出完事件后挂起（测超时杀树）。"""
    folder.mkdir(parents=True, exist_ok=True)
    cli = folder / "fake.cjs"
    lines = []
    for ev in events:
        lines.append(f"console.log({_js_str(json.dumps(ev, ensure_ascii=False))});")
    body = "\n".join(lines)
    cli.write_text(
        'if(process.argv.includes("--version")){console.log("offline-fixture");process.exit(0)}\n'
        'const chunks=[];process.stdin.on("data",c=>chunks.push(c));\n'
        'process.stdin.on("end",()=>{\n' + body + "\n"
        + ("setInterval(()=>{},1000);\n" if hang else
           ("process.stderr.write(" + _js_str(stderr_extra) + ");\n" if stderr_extra else "")
           + f"process.exit({exit_code});\n") + "});\n",
        encoding="utf-8")
    return cli


def make_backend(cli: Path, *, timeout: float = 20, **kw) -> PiBackend:
    return PiBackend(cli_path=str(cli), timeout=timeout, **kw)


def run_case(tmp_path: Path, events: list[dict], *, exit_code: int = 0,
             hang: bool = False, stderr_extra: str = "",
             split_chunk: int | None = None, cancel_first: bool = False,
             backend_kw: dict | None = None) -> dict:
    """跑一个 fake 会话返回 run_result。split_chunk=喂入块大小（字节）
    ——None 走后端默认 4096 路径；测试跨块分割用小值驱动真实 pump。"""
    cli = make_fake_cli(tmp_path, events, exit_code=exit_code, hang=hang,
                        stderr_extra=stderr_extra)
    b = make_backend(cli, **(backend_kw or {}))
    if cancel_first:
        ev = threading.Event()
        ev.set()
        return b.run("fixture", tmp_path / "wd", None, cancel_event=ev)
    if split_chunk is not None:
        # 直接驱动解析器路径（等价于后端 pump 的逐块喂入）
        payload = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
        stream = PiEventStream()
        i = 0
        while i < len(payload):
            stream.feed(payload[i:i + split_chunk])
            i += split_chunk
        stream.close()
        return {"_stream_only": True, "stats": stream.stats}
    return b.run("fixture", tmp_path / "wd", None)


_RID = {"n": 0}


def msg(tokens=None, cost=None, text="complete", **extra):
    """assistant message_end 构造器。usage/cost 缺省可控制字段缺失。
    responseId 每条唯一（协议真实语义——供应商响应 ID 逐消息递增；
    固定值会触发 R6 的稳定标识去重）。"""
    _RID["n"] += 1
    m = {"role": "assistant", "content": ([{"type": "text", "text": text}]
                                          if text is not None else []),
         "model": "offline", "responseId": f"resp-{_RID['n']}",
         "stopReason": "stop"}
    usage = {}
    if tokens is not None:
        usage.update({"input": tokens - 10, "output": 10,
                      "cacheRead": 0, "cacheWrite": 0, "totalTokens": tokens})
    if cost is not None:
        usage["cost"] = {"input": 0, "output": cost, "cacheRead": 0,
                         "cacheWrite": 0, "total": cost}
    if usage:
        m["usage"] = usage
    m.update(extra)
    return m


def me(m: dict) -> dict:
    """message → 权威 message_end 事件包装。"""
    return {"type": "message_end", "message": m}


SESSION = {"type": "session", "version": 3, "id": "sess-uuid-xyz",
           "timestamp": "2026-11-08T00:00:00.000Z", "cwd": "/w"}
SETTLED_OK = {"type": "agent_settled", "aborted": False}
SETTLED_ABORT = {"type": "agent_settled", "aborted": True}


def full_run(events):
    out = []
    for e in events:
        # message 对象（含 role 键）自动包装为权威 message_end 事件
        if isinstance(e, dict) and "role" in e:
            e = {"type": "message_end", "message": e}
        out.append(e)
    return [SESSION, {"type": "agent_start"}, {"type": "turn_start"}] + out \
        + [{"type": "agent_end", "messages": [], "willRetry": False}, SETTLED_OK]


# ---- 用量统计 ----

def test_multi_message_usage_accumulates(tmp_path):
    """100+200 tokens / 费用1+2 → 累计 300/3（旧实现只记 200/2）。"""
    r = run_case(tmp_path, full_run([msg(100, 1), msg(200, 2)]))
    assert r["ok"] is True
    assert r["resources"]["tokens"] == 300
    assert r["resources"]["usage_stats"]["messages_total"] == 2
    assert r["resources"]["usage_stats"]["messages_counted"] == 2
    assert r["resources"]["usage_stats"]["usage_complete"] is True
    assert r["resources"]["receipt_status"] == "valid"


def test_tool_only_message_usage_counted(tmp_path):
    """纯工具调用消息（无正文）usage 同样计入（旧实现忽略）。"""
    tool_msg = msg(tokens=70, cost=0.7, text=None,
                   content=[{"type": "toolCall", "toolCallId": "c1",
                             "toolName": "bash"}])
    r = run_case(tmp_path, full_run([tool_msg, msg(50, 0.5)]))
    assert r["resources"]["tokens"] == 120
    assert r["resources"]["usage_stats"]["messages_counted"] == 2
    # 有正文的是最后一条 → 摘要来自它；纯工具消息也参与了累计
    assert r["resources"]["usage_stats"]["messages_total"] == 2


def test_update_and_turn_end_not_double_counted(tmp_path):
    """message_update（流式 usage）与 turn_end（重复携带）不重复累计。"""
    events = [
        {"type": "message_update",
         "usage": {"input": 90, "output": 10, "totalTokens": 100},
         "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0,
                                   "delta": "Hi"}},
        msg(100, 1),
        {"type": "turn_end", "message": msg(100, 1), "toolResults": []},
        msg(30, 0.3, text="more"),
    ]
    r = run_case(tmp_path, full_run(events))
    # 只计两条 message_end：100 + 30（update 与 turn_end 的 100 不重复计）
    assert r["resources"]["tokens"] == 130
    assert r["resources"]["usage_stats"]["messages_total"] == 2


def test_retry_error_then_success(tmp_path):
    """中间错误（错误前用量保留）→ 自动重试成功 → 累计含错误轮用量。"""
    err_msg = msg(80, 0.8, text=None, content=[], stopReason="error",
                  errorMessage="529 overloaded")
    events = [
        SESSION,
        {"type": "agent_start"},
        me(err_msg),
        {"type": "agent_end", "messages": [], "willRetry": True},
        {"type": "auto_retry_start", "attempt": 1, "maxAttempts": 3,
         "delayMs": 10, "errorMessage": "529 overloaded"},
        {"type": "auto_retry_end", "success": True, "attempt": 2},
        me(msg(50, 0.5)),
        {"type": "agent_end", "messages": [], "willRetry": False},
        SETTLED_OK,
    ]
    r = run_case(tmp_path, events)
    assert r["ok"] is True
    assert r["resources"]["tokens"] == 130          # 80(错误轮) + 50(成功轮)
    assert r["resources"]["session_outcome"] == "completed"
    assert r["resources"]["diagnostics"]["retries"]  # 重试留档


def test_missing_usage_not_faked_zero(tmp_path):
    """缺 usage：不补已知零——tokens 缺失、incomplete、留痕。"""
    r = run_case(tmp_path, full_run([{"role": "assistant",
                                      "content": [{"type": "text", "text": "done"}],
                                      "stopReason": "stop"}]))
    assert r["ok"] is True                    # 会话完成与用量缺失分开判定
    assert r["resources"]["receipt_status"] == "incomplete"
    assert r["resources"]["usage_stats"]["usage_complete"] is False
    assert r["resources"]["usage_errors"]
    assert "tokens" not in r["resources"]     # 不伪造 tokens


def test_partial_missing_usage_sums_known_part(tmp_path):
    """一条有 usage 一条缺 → 合计=合法条；usage_complete=False。"""
    r = run_case(tmp_path, full_run([msg(100, 1),
                                     {"role": "assistant",
                                      "content": [{"type": "text", "text": "x"}],
                                      "stopReason": "stop"}]))
    assert r["resources"]["tokens"] == 100
    assert r["resources"]["usage_stats"]["usage_complete"] is False
    assert r["resources"]["receipt_status"] == "incomplete"


def test_invalid_usage_values_rejected(tmp_path):
    """负值/字符串/NaN → usage_errors 留档且不计入合计。"""
    bad1 = {"role": "assistant", "content": [{"type": "text", "text": "a"}],
            "stopReason": "stop",
            "usage": {"totalTokens": -5}}
    bad2 = {"role": "assistant", "content": [{"type": "text", "text": "b"}],
            "stopReason": "stop",
            "usage": {"totalTokens": "lots"}}
    good = msg(40, 0.4)
    r = run_case(tmp_path, full_run([bad1, bad2, good]))
    assert r["resources"]["tokens"] == 40
    assert len(r["resources"]["usage_errors"]) >= 2
    assert r["resources"]["receipt_status"] == "incomplete"


def test_stream_exceeds_tail_buffer(tmp_path):
    """事件流超过尾部缓冲上限：关键 usage 消息在流头 → 统计不丢。"""
    noise = [{"type": "tool_execution_update", "toolCallId": f"c{i}",
              "toolName": "bash", "args": {"x": "y" * 200},
              "partialResult": {"padding": "p" * 500}}
             for i in range(1200)]                     # > 512 KiB
    events = full_run([msg(100, 1)]) 
    r = run_case(tmp_path, noise + events)
    assert r["ok"] is True
    assert r["resources"]["tokens"] == 100
    assert r["resources"]["diagnostics"]["stdout_truncated"] is True


def test_record_split_across_chunks(tmp_path):
    """一条 JSON 记录被多个读取块分割 → 增量解析正确。"""
    events = full_run([msg(210, 2.1)])
    r = run_case(tmp_path, events, split_chunk=7)   # 每 7 字节一块
    assert r["_stream_only"] is True
    assert r["stats"].tokens_total == 210
    assert r["stats"].session_id == "sess-uuid-xyz"
    assert r["stats"].settled is True


def test_unicode_separators_inside_json_strings(tmp_path):
    """JSON 字符串内 U+2028/U+2029（合法）不是记录边界——splitlines 反例。"""
    tricky = "line1\u2028line2\u2029line3"
    r = run_case(tmp_path, full_run([msg(55, 0.55, text=tricky)]))
    assert r["ok"] is True
    assert r["resources"]["tokens"] == 55
    assert tricky in r["summary"]


def test_trailing_record_without_newline(tmp_path):
    """最后一条无 LF 结尾 → close() 仍解析并如实标记 trailing。"""
    payload = (json.dumps(SESSION) + "\n"
               + json.dumps({"type": "agent_start"}) + "\n"
               + json.dumps(me(msg(66, 0.66))))  # 无结尾 LF
    stream = PiEventStream()
    stream.feed(payload)
    stream.close()
    assert stream.stats.tokens_total == 66
    assert stream.stats.trailing_partial is True


def test_garbage_line_counted_as_parse_error(tmp_path):
    """夹一行非法 JSON → parse_errors 计数，其余统计不受影响。"""
    stream = PiEventStream()
    stream.feed("not-json-garbage\n")
    for e in full_run([msg(88, 0.88)]):
        stream.feed(json.dumps(e, ensure_ascii=False) + "\n")
    stream.close()
    assert stream.stats.parse_errors == 1
    assert stream.stats.tokens_total == 88


# ---- 费用来源 ----

def test_cost_estimated_not_reported(tmp_path):
    """Pi 本地价格估算 → basis=estimated（旧实现冒充 reported）。"""
    r = run_case(tmp_path, full_run([msg(100, 1), msg(200, 2)]))
    cost = r["resources"]["cost"]
    assert cost["amount"] == 3.0
    assert cost["basis"] == "estimated"
    assert cost["cost_source"] == "pi-local-pricing"


def test_cost_zero_still_estimated(tmp_path):
    """配置缺价产生的零：estimated 零 ≠ 供应商确认免费。"""
    r = run_case(tmp_path, full_run([msg(100, 0)]))
    cost = r["resources"]["cost"]
    assert cost["amount"] == 0.0
    assert cost["basis"] == "estimated"


def test_missing_cost_unknown(tmp_path):
    """缺 cost 字段 → unknown（金额 None，不冒充零）。"""
    raw = {"role": "assistant", "content": [{"type": "text", "text": "d"}],
           "stopReason": "stop",
           "usage": {"input": 5, "output": 5, "totalTokens": 10}}
    r = run_case(tmp_path, full_run([raw]))
    cost = r["resources"]["cost"]
    assert cost["amount"] is None and cost["basis"] == "unknown"         and cost["currency"] == "USD"
    assert cost["cost_complete"] is False           # R6：缺失显式标注
    assert cost["cost_missing_messages"] >= 1


def test_error_keeps_known_cost(tmp_path):
    """错误/取消路径：已知费用不被清零（结算流水由平台差额规则保障）。"""
    err_msg = msg(80, 1.25, text=None, content=[], stopReason="error",
                  errorMessage="quota exhausted")
    events = [SESSION, {"type": "agent_start"}, me(err_msg),
              {"type": "agent_end", "messages": [], "willRetry": False},
              {"type": "auto_retry_end", "success": False, "attempt": 3,
               "finalError": "Token Plan 用量上限"},
              SETTLED_OK]
    r = run_case(tmp_path, events)
    assert r["ok"] is False
    cost = r["resources"]["cost"]
    assert cost["amount"] == 1.25 and cost["basis"] == "estimated"         and cost["cost_source"] == "pi-local-pricing"
    assert cost["cost_complete"] is True           # 错误轮 cost 完整在案
    assert r["resources"]["tokens"] == 80


# ---- 终态识别与回执 ----

def test_error_after_text_with_exit0_not_ok(tmp_path):
    """核心反例：先正常正文 → 最终 stopReason=error 空正文 exit=0 → 必失败。"""
    err_msg = {"role": "assistant", "content": [], "stopReason": "error",
               "errorMessage": "fixture quota exhausted", "responseId": "r9"}
    events = full_run([msg(100, 1), err_msg])
    r = run_case(tmp_path, events)
    assert r["ok"] is False
    assert "fixture quota exhausted" in r["summary"]
    assert r["resources"]["error_class"] == "quota_exhausted"
    assert r["resources"]["receipt_status"] == "incomplete"


def test_error_class_matrix():
    """结构化错误分类表（只吃协议字段，不扫正文）。"""
    assert classify_provider_error("(429) Too Many Requests") == "rate_limited"
    assert classify_provider_error("529 overloaded") == "rate_limited"
    assert classify_provider_error("Token Plan 用量上限") == "quota_exhausted"
    assert classify_provider_error("insufficient balance") == "balance_exhausted"
    assert classify_provider_error("401 Unauthorized: invalid api key") == "auth_failed"
    assert classify_provider_error("2069 model no longer available") == "model_unavailable"
    assert classify_provider_error("fetch failed ECONNRESET") == "service_unavailable"
    assert classify_provider_error("") == "unknown"


def test_aborted_session(tmp_path):
    """agent_settled aborted=true → 失败。"""
    events = [SESSION, {"type": "agent_start"}, me(msg(10, 0.1)),
              {"type": "agent_end", "messages": [], "willRetry": False},
              SETTLED_ABORT]
    r = run_case(tmp_path, events)
    assert r["ok"] is False
    assert r["resources"]["session_outcome"] == "aborted"


def test_length_stop_reason(tmp_path):
    """stopReason=length → 未完成失败。"""
    events = full_run([msg(10, 0.1, text="partial", stopReason="length")])
    r = run_case(tmp_path, events)
    assert r["ok"] is False
    assert "length" in r["summary"]


def test_no_settled_is_incomplete(tmp_path):
    """只有 message_end 无 agent_settled → 未确认完成（协议收紧）。"""
    events = [SESSION, me(msg(100, 1))]
    r = run_case(tmp_path, events, exit_code=0)
    assert r["ok"] is False
    assert r["resources"]["session_outcome"] == "incomplete"


def test_exit_nonzero_with_completed_events(tmp_path):
    """事件流 completed 但 exit=1 → 如实失败（诊断含 stderr）。"""
    r = run_case(tmp_path, full_run([msg(50, 0.5)]), exit_code=1,
                 stderr_extra="boom diagnostics")
    assert r["ok"] is False
    assert "退出码 1" in r["summary"]
    assert "boom diagnostics" in r["resources"]["diagnostics"]["stderr_tail"]


def test_identity_three_way_split(tmp_path):
    """身份三分：session header id / responseId / 平台各归其位。"""
    custom = msg(70, 0.7)
    custom["responseId"] = "provider-resp-abc"
    r = run_case(tmp_path, full_run([custom]))
    res = r["resources"]
    assert r["session_id"] == "sess-uuid-xyz"       # Pi 会话身份
    assert res["agent_id"] == "sess-uuid-xyz"
    assert res["pi_session_id"] == "sess-uuid-xyz"
    assert res["response_id"] == "provider-resp-abc"


def test_missing_session_identity_not_faked(tmp_path):
    """无 session header：不冒充身份（无 agent_id 字段 → 平台 incomplete）。"""
    events = [{"type": "agent_start"}, me(msg(70, 0.7)),
              {"type": "agent_end", "messages": [], "willRetry": False},
              SETTLED_OK]
    r = run_case(tmp_path, events)
    res = r["resources"]
    assert "agent_id" not in res
    assert res["receipt_status"] == "incomplete"


# ---- 管道 / 取消 / 超时 / 并发 / 脱敏 ----

def test_precancelled_no_process(tmp_path):
    """预取消：不启动进程、ok=False。"""
    r = run_case(tmp_path, full_run([msg(1, 0)]), cancel_first=True)
    assert r["ok"] is False
    assert "取消" in r["summary"]


def test_timeout_kills_hung_process(tmp_path):
    """挂起会话超时 → 进程树被终止、ok=False、不再等满 timeout×2。"""
    t0 = time.monotonic()
    r = run_case(tmp_path, full_run([msg(1, 0)]), hang=True,
                 backend_kw={"timeout": 2})
    elapsed = time.monotonic() - t0
    assert r["ok"] is False
    assert r["resources"]["timeout"] is True
    assert elapsed < 15   # 超时路径及时退出（旧 stdin 阻塞缺陷会绕过超时）


def test_big_prompt_does_not_bypass_timeout(tmp_path):
    """大 prompt（>管道缓冲）+ 挂起消费的 CLI：stdin 独立线程写，
    超时照常触发（旧实现 write 阻塞主循环绕过超时）。"""
    big = "x" * (512 * 1024)   # 512KiB prompt
    cli = make_fake_cli(tmp_path, [], hang=True)   # 永不读 stdin
    b = make_backend(cli, timeout=2)
    t0 = time.monotonic()
    r = b.run(big, tmp_path / "wd", None)
    elapsed = time.monotonic() - t0
    assert r["ok"] is False
    assert r["resources"]["timeout"] is True
    assert elapsed < 15


def test_concurrent_sessions_do_not_cross(tmp_path):
    """两个并发 attempt 各自解析各自事件流，不串会话。"""
    results = {}

    def _one(name: str, tokens: int):
        events = full_run([msg(tokens, 0.1)])
        cli = make_fake_cli(tmp_path / name, events)
        b = make_backend(cli)
        results[name] = b.run("stmt", tmp_path / name / "wd", None)

    ths = [threading.Thread(target=_one, args=("a", 111)),
           threading.Thread(target=_one, args=("b", 222))]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout=30)
    assert results["a"]["resources"]["tokens"] == 111
    assert results["b"]["resources"]["tokens"] == 222
    assert results["a"]["session_id"] == results["b"]["session_id"] == "sess-uuid-xyz"


def test_diagnostics_redact_secret(tmp_path):
    """凭据出现在 stderr → 诊断脱敏（用虚构敏感标记，不用真实密钥）。"""
    fake_key = "SK-FIXTURE-NOT-A-REAL-KEY-123456"
    cli = make_fake_cli(tmp_path, full_run([msg(50, 0.5)]), exit_code=1,
                        stderr_extra=f"trace: {fake_key}")
    b = PiBackend(cli_path=str(cli), timeout=20)
    b.extra_env = {"BIGMODEL_API_KEY": fake_key}
    b.auth_ready = True
    r = b.run("stmt", tmp_path / "wd", None)
    tail = r["resources"]["diagnostics"]["stderr_tail"]
    assert fake_key not in tail
    assert "***REDACTED***" in tail
