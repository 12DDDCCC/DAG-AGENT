# -*- coding: utf-8 -*-
r"""Pi JSON 事件流增量解析器（pi-coding-agent ≥1.5，本机 1.5.0 协议核对）。

协议要点（docs/json.md；缺陷依据 work/2026-11-08-pi-independent-review 与
work/2026-11-08-fix-acceptance 第二轮独立验收）：

- **严格 JSONL 分帧**：每条记录以 LF(``\n``) 结束、可选前置 CR；
  JSON 字符串内的 U+2028/U+2029 等 Unicode 分隔符**不是**记录边界
  （禁止 str.splitlines()）。
- **首条记录 = session header**：id=Pi 会话身份（session_id 权威来源）；
  供应商 responseId 是另一身份，不得混用。
- **用量统计以 message_end 为准**：每条 assistant 消息恰好一条
  message_end。message_update（流式累计 usage）与 turn_end（重复携带
  同一消息）不计数。重复的 assistant message_end 以 **responseId 稳定
  标识**去重（同一供应商响应 ID 只统计一次，重复计数留痕；不依赖
  正文相等；缺 responseId 的消息各算一次并留痕）。
- **agent_end ≠ 会话完成**：agent_end 关闭一次低层 run（willRetry=true
  时自动重试继续）；agent_settled（aborted 字段）才是会话级终态。
  JSON 模式进程 exit code 不反映会话失败。
- **完整性纪律（第二轮 R6）**：解析错误/截断/矛盾或未知终态/缺失或非法
  usage·cost 分量，至少使回执不完整——不默认成功；部分费用缺失保留
  已知小计并显式标注整体不完整（区分缺价零/本地明确零价/已知估算小计/
  未知部分）。
- **费用**：usage.cost.total 是 Pi 按本地价格表计算的估算值（缺价默认
  零）——basis 一律 estimated；会话级 cost_complete=False 表示并非每条
  assistant 消息都携带合法 cost（部分缺失如实标注，不丢失信息）。
- 统计走增量解析（feed 逐块喂入），原始解析数据保留在 stats（内部）；
  可输出诊断由调用方在持久化/返回边界统一脱敏（pi_backend._redact_obj）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field


def _valid_count(v) -> bool:
    """非负整数（bool 排除；浮点整值宽容收整）。"""
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return v >= 0
    if isinstance(v, float):
        return v.is_integer() and v >= 0
    return False


def _valid_amount(v) -> bool:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    return v >= 0 and v == v and v not in (float("inf"), float("-inf"))


# assistant stopReason 的已知合法集合（1.5.0：pending/stop/error/aborted/
# length；集合外=未知终态，不默认成功）
KNOWN_STOP_REASONS = frozenset(
    {"stop", "error", "aborted", "length", "pending"})


@dataclass
class PiSessionStats:
    """一次 Pi 会话事件流的统计与终态材料（内部原始数据——可输出诊断
    由调用方脱敏后生成，本对象不直接落库）。"""
    session_id: str = ""
    protocol_version: int | None = None
    messages_total: int = 0              # assistant message_end 事件数（含重复）
    messages_counted: int = 0            # 唯一且 usage 合法计入的消息数
    tokens_total: int | None = None      # 合法条 totalTokens 求和
    usage_complete: bool = True          # 每条唯一 assistant 消息都有合法 totalTokens
    usage_component_errors: int = 0      # input/output/cache 等分量非法数
    usage_errors: list = field(default_factory=list)
    cost_seen: bool = False              # 是否见过任何 cost.total
    cost_total: float | None = None      # 合法 cost.total 求和（已知小计）
    cost_messages_with: int = 0          # 携带合法 cost.total 的消息数
    cost_messages_missing: int = 0       # 有 usage 但缺 cost 字段/total 的消息数
    cost_messages_invalid: int = 0       # cost.total 非法的消息数
    result_text: str = ""
    stop_reason: str | None = None
    stop_reason_known: bool = True       # 最后一条 assistant stopReason 是否已知
    error_message: str | None = None
    response_id: str = ""
    model: str = ""
    agent_end_count: int = 0
    will_retry: bool = False
    settled: bool = False
    aborted: bool = False
    retries: list = field(default_factory=list)
    parse_errors: int = 0
    records_total: int = 0
    trailing_partial: bool = False
    duplicate_message_ends: int = 0      # 同 responseId 重复 message_end 计数
    conflicting_duplicate_ends: int = 0  # 同 responseId 但终态/用量冲突的重复计数
    conflict_detail: str = ""
    seen_response_ids: set = field(default_factory=set)
    seen_response_fingerprints: dict = field(default_factory=dict)


def _as_int(v) -> int:
    return int(v)


class PiEventStream:
    """增量 JSONL 解析器：feed() 逐块喂入，close() 收尾。

    完整行缓冲无大小上限（统计不能因日志截断丢失）；诊断尾部限长由
    调用方的 _Tail 负责。解析异常不中断 feed（逐行隔离），并计入
    parse_errors 供终态降级。
    """

    _MAX_DIAG = 40

    def __init__(self):
        self._buf: str = ""
        self.stats = PiSessionStats()

    # ---- 分帧 ----

    def feed(self, chunk: str) -> None:
        self._buf += chunk
        while True:
            idx = self._buf.find("\n")
            if idx < 0:
                return
            line = self._buf[:idx]
            self._buf = self._buf[idx + 1:]
            self._handle_line(line)

    def close(self) -> None:
        rest = self._buf
        self._buf = ""
        if rest.strip():
            self.stats.trailing_partial = True
            self._handle_line(rest)

    # ---- 事件分发 ----

    def _handle_line(self, line: str) -> None:
        if line.endswith("\r"):
            line = line[:-1]
        if not line.strip():
            return
        self.stats.records_total += 1
        try:
            ev = json.loads(line)
        except ValueError:
            self.stats.parse_errors += 1
            return
        if not isinstance(ev, dict):
            self.stats.parse_errors += 1
            return
        t = ev.get("type")
        if t == "session":
            self.stats.session_id = str(ev.get("id") or "")
            v = ev.get("version")
            if isinstance(v, int) and not isinstance(v, bool):
                self.stats.protocol_version = v
        elif t == "message_end":
            msg = ev.get("message")
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                self._assistant_message_end(msg)
        elif t == "agent_end":
            self.stats.agent_end_count += 1
            self.stats.will_retry = bool(ev.get("willRetry"))
        elif t == "agent_settled":
            self.stats.settled = True
            self.stats.aborted = bool(ev.get("aborted"))
        elif t in ("auto_retry_start", "auto_retry_end"):
            if len(self.stats.retries) < self._MAX_DIAG:
                keep = {k: ev.get(k) for k in
                        ("type", "attempt", "maxAttempts", "delayMs",
                         "errorMessage", "success", "finalError")
                        if ev.get(k) is not None}
                self.stats.retries.append(keep)

    # ---- assistant message_end ----

    def _assistant_message_end(self, msg: dict) -> None:
        st = self.stats
        st.messages_total += 1
        # 重复 message_end：以 responseId 稳定标识去重（同一供应商响应
        # 只统计一次）；缺 responseId 各算一次并留痕（不依赖正文相等）
        rid = msg.get("responseId")
        rid_key = str(rid) if rid else ""
        usage = msg.get("usage")
        cur_tt = usage.get("totalTokens") if isinstance(usage, dict) else None
        cur_cost = (usage.get("cost", {}).get("total")
                    if isinstance(usage, dict) and isinstance(usage.get("cost"), dict)
                    else None)
        cur_stop = msg.get("stopReason")
        cur_err = msg.get("errorMessage")
        fp = (cur_stop, cur_err, cur_tt, cur_cost)
        if rid_key and rid_key in st.seen_response_ids:
            st.duplicate_message_ends += 1
            prev_fp = st.seen_response_fingerprints.get(rid_key)
            if prev_fp is not None and prev_fp != fp:
                # A5（第三轮）：同一 responseId 重复事件内容/终态不一致 →
                # fail-closed 冲突留痕，绝不掩盖后续 error/aborted
                st.conflicting_duplicate_ends += 1
                st.usage_complete = False
                detail = (f"msg#{st.messages_total}: responseId={rid_key[:24]}"
                          f" 重复事件冲突 prev={prev_fp} vs cur={fp}")
                if not st.conflict_detail:
                    st.conflict_detail = detail
                if cur_stop in ("error", "aborted", "length") \
                        or (cur_stop is not None and cur_stop not in KNOWN_STOP_REASONS):
                    st.stop_reason = cur_stop
                    st.stop_reason_known = (cur_stop in KNOWN_STOP_REASONS)
                if cur_err:
                    st.error_message = str(cur_err)
                if len(st.usage_errors) < self._MAX_DIAG:
                    st.usage_errors.append(detail)
                return
            if len(st.usage_errors) < self._MAX_DIAG:
                st.usage_errors.append(
                    f"msg#{st.messages_total}: 重复 message_end"
                    f"(responseId={rid_key[:24]})——按稳定标识去重不重复统计")
            return
        if rid_key:
            st.seen_response_ids.add(rid_key)
            st.seen_response_fingerprints[rid_key] = fp
        elif len(st.usage_errors) < self._MAX_DIAG:
            st.usage_errors.append(
                f"msg#{st.messages_total}: assistant 消息缺 responseId"
                "（无法跨事件稳定标识）")
        if isinstance(usage, dict):
            tt = usage.get("totalTokens")
            if tt is None:
                st.usage_complete = False
                if len(st.usage_errors) < self._MAX_DIAG:
                    st.usage_errors.append(
                        f"msg#{st.messages_total}: usage 缺 totalTokens（不补已知零）")
            elif _valid_count(tt):
                st.tokens_total = ((0 if st.tokens_total is None
                                    else st.tokens_total) + _as_int(tt))
                st.messages_counted += 1
            else:
                st.usage_complete = False
                if len(st.usage_errors) < self._MAX_DIAG:
                    st.usage_errors.append(
                        f"msg#{st.messages_total}: totalTokens 非法 {tt!r}")
            for k in ("input", "output", "cacheRead", "cacheWrite"):
                if k in usage and usage[k] is not None \
                        and not _valid_count(usage[k]):
                    st.usage_component_errors += 1
                    st.usage_complete = False   # 非法分量影响完整性（R6）
                    if len(st.usage_errors) < self._MAX_DIAG:
                        st.usage_errors.append(
                            f"msg#{st.messages_total}: {k} 非法 {usage[k]!r}")
            cost = usage.get("cost")
            if isinstance(cost, dict) and cost.get("total") is not None:
                ctotal = cost["total"]
                if _valid_amount(ctotal):
                    st.cost_seen = True
                    st.cost_messages_with += 1
                    st.cost_total = round(
                        (0.0 if st.cost_total is None else st.cost_total)
                        + float(ctotal), 9)
                else:
                    st.cost_messages_invalid += 1
                    if len(st.usage_errors) < self._MAX_DIAG:
                        st.usage_errors.append(
                            f"msg#{st.messages_total}: cost.total 非法 {ctotal!r}")
            else:
                # 有 usage 但无 cost（缺价零/未上报）——部分缺失留痕
                st.cost_messages_missing += 1
        else:
            st.usage_complete = False
            st.cost_messages_missing += 1
            if len(st.usage_errors) < self._MAX_DIAG:
                st.usage_errors.append(
                    f"msg#{st.messages_total}: 无 usage 字段（费用与用量均缺失）")
        # 终态材料：以最后一条唯一 assistant message_end 为权威
        parts = msg.get("content") or []
        text = "".join(c.get("text", "") for c in parts
                       if isinstance(c, dict) and c.get("type") == "text")
        if text.strip():
            st.result_text = text
        if rid:
            st.response_id = str(rid)
        st.stop_reason = msg.get("stopReason")
        st.stop_reason_known = (st.stop_reason is None
                                or st.stop_reason in KNOWN_STOP_REASONS)
        st.error_message = msg.get("errorMessage")
        model = msg.get("model")
        if model:
            st.model = str(model)

    # ---- 派生结论 ----

    def cost_of(self) -> dict:
        """费用口径（R6：区分缺价零/明确零价/已知小计/未知部分）：
        - 有任何合法 cost.total → estimated 小计 + cost_complete 标注
          （部分消息缺 cost 时 amount 是已知小计而非全程）；
        - 从未见过 cost → unknown（缺失不补零）。
        cost_complete=False 不丢失缺失信息（缺失/非法计数在 stats）。"""
        st = self.stats
        complete = (st.messages_total > 0
                    and st.cost_messages_missing == 0
                    and st.cost_messages_invalid == 0)
        if not st.cost_seen or st.cost_total is None:
            return {"amount": None, "basis": "unknown", "currency": "USD",
                    "cost_complete": False,
                    "cost_missing_messages": st.cost_messages_missing,
                    "cost_invalid_messages": st.cost_messages_invalid}
        out = {"amount": st.cost_total, "basis": "estimated",
               "currency": "USD", "cost_source": "pi-local-pricing",
               "cost_complete": complete,
               "cost_missing_messages": st.cost_messages_missing,
               "cost_invalid_messages": st.cost_messages_invalid}
        if not complete:
            out["cost_note"] = ("部分 assistant 消息缺 cost/非法——amount 为"
                                "已知条小计，全程费用未知")
        return out

    def outcome(self) -> tuple[str, str]:
        """按 1.5.0 协议判会话终态：返回 (outcome, detail)。

        outcome ∈ completed / failed / aborted / incomplete。
        R6 纪律：解析错误/截断/矛盾或未知终态不默认成功（至少 incomplete）；
        stopReason=aborted 与 settled.aborted=false 矛盾 → aborted（失败）。
        """
        st = self.stats
        stop = st.stop_reason
        # 解析异常/截断：丢失记录可能含用量或致命错误——永不默认成功
        if st.parse_errors > 0:
            return ("incomplete",
                    f"事件流含 {st.parse_errors} 条无法解析的记录——丢失内容"
                    "未知，不默认成功")
        if st.trailing_partial:
            return ("incomplete", "事件流以未完整收尾的记录结束（截断）")
        if st.conflicting_duplicate_ends > 0:
            if stop == "error":
                return ("failed",
                        str(st.error_message or st.conflict_detail
                            or "同一 responseId 重复事件终态冲突(error)")[:500])
            if stop == "aborted":
                return ("aborted",
                        str(st.error_message or st.conflict_detail
                            or "同一 responseId 重复事件终态冲突(aborted)")[:500])
            return ("incomplete",
                    f"事件流含 {st.conflicting_duplicate_ends} 处同一 responseId"
                    f" 冲突的重复 message_end（{st.conflict_detail[:200]}）"
                    "——不一致去重拒绝判成功")
        if stop == "aborted" and not st.aborted:
            # 消息级 aborted 与 settled.aborted=false 矛盾——不默认成功
            return ("aborted",
                    f"assistant stopReason=aborted 但 agent_settled.aborted="
                    f"{st.aborted}（矛盾终态）")
        if st.aborted:
            return "aborted", (st.error_message or "agent_settled aborted=true")
        if not st.settled:
            return ("incomplete",
                    f"未见 agent_settled（agent_end={st.agent_end_count}，"
                    f"willRetry={st.will_retry}）——会话未确认完成")
        if stop == "error":
            return "failed", str(st.error_message
                                 or "assistant 最终消息 stopReason=error")[:500]
        if stop == "length":
            return "failed", "stopReason=length（长度截断，未完成）"
        if stop == "aborted":
            return "aborted", (st.error_message or "stopReason=aborted")
        if stop == "pending" or not st.stop_reason_known:
            return ("incomplete",
                    f"最终 assistant stopReason={stop!r}（未知/未完成终态）"
                    "——不默认成功")
        if st.messages_total == 0:
            return "incomplete", "会话 settled 但无任何 assistant 消息"
        return "completed", (st.error_message or "")


def classify_provider_error(*texts: str,
                            provider: str | None = None) -> str:
    """供应商错误分类（第二轮 R5：业务语义优先于通用 HTTP 状态）。

    **只**依据 Pi 协议结构化错误通道（assistant errorMessage /
    auto_retry finalError）——不扫描模型输出正文。

    - 业务语义（配额/余额/认证/模型不可用）先于 429/529 等通用状态；
    - 数字错误码按供应商命名空间解释（默认本机 glm→bigmodel 码表；
      其他/未知 provider 不用数字码，避免混用厂商同号错误）。

    返回 rate_limited / quota_exhausted / balance_exhausted /
    auth_failed / service_unavailable / model_unavailable / unknown。
    """
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return "unknown"
    # 1) 业务语义优先（含 429 前缀的组合如 "429 Token Plan usage limit"）
    if any(p in blob for p in ("token plan", "usage limit", "quota",
                               "配额", "用量上限", "usage cap",
                               "resource exhausted")):
        return "quota_exhausted"
    if any(p in blob for p in ("insufficient balance", "balance",
                               "余额不足", "欠费", "arrears")):
        return "balance_exhausted"
    if any(p in blob for p in ("401", "403", "unauthorized", "invalid api key",
                               "authentication", "认证失败", "invalid_api_key")):
        return "auth_failed"
    if any(p in blob for p in ("model not found", "no longer available",
                               "unknown model", "模型不存在", "已下线",
                               "not deployed")):
        return "model_unavailable"
    # 2) 供应商命名空间数字码（bigmodel 家族；仅当 provider 归属明确或
    #    缺省为本机 glm/bigmodel 时才解释，避免厂商同号混用）
    if provider is None or provider.lower() in ("glm", "bigmodel", "zai"):
        if "2056" in blob or "1113" in blob:
            return "quota_exhausted"
        if "1002" in blob:
            return "balance_exhausted"
        if "2013" in blob or "2069" in blob:
            return "model_unavailable"
    # 3) 服务暂时故障先于纯限流（overloaded 等既有 5 也有 529 前缀）
    if any(p in blob for p in ("timeout", "timed out", "econnreset",
                               "econnrefused", "enotfound", "eai_again",
                               "connection", "socket hang up", "fetch failed",
                               "network", "500", "502", "503", "504",
                               "服务暂不可用")):
        return "service_unavailable"
    # 4) 纯限流（通用 HTTP 状态最后判定）
    if any(p in blob for p in ("429", "529", "rate limit", "too many requests",
                               "限流", "请求过于频繁", "throttl")):
        return "rate_limited"
    if "overloaded" in blob:
        return "service_unavailable"
    return "unknown"


def parse_retry_after_hint(text: str) -> float | None:
    """从结构化错误文本提取 Retry-After 提示（秒）。

    支持：纯数字秒 / "retry-after: N" / RFC1123 HTTP 日期。
    非法（负数/零/不可解析/超过 86400s）→ None（调用方使用有上限退避）。"""
    import re
    import email.utils
    from datetime import datetime, timezone
    if not text:
        return None
    m = re.search(r"retry-?after[\s:=]*([A-Za-z]{3},[^\"'\r\n]{10,40})", text, re.I)
    if m:
        try:
            dt = email.utils.parsedate_to_datetime(m.group(1).strip())
            if dt is not None:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                delta = (dt - datetime.now(timezone.utc)).total_seconds()
                return delta if 0 < delta <= 86400 else None
        except (ValueError, TypeError):
            return None
    m = re.search(r"retry-?after[\s:=]*([+-]?\d+(?:\.\d+)?)", text, re.I)
    if m:
        v = float(m.group(1))
        return v if 0 < v <= 86400 else None
    m = re.search(r"(?<![\w+-])([+-]?\d+(?:\.\d+)?)\s*(?:s|sec|seconds|秒)(?:\b|$)", text, re.I)
    if m:
        v = float(m.group(1))
        return v if 0 < v <= 86400 else None
    return None
