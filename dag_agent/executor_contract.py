# -*- coding: utf-8 -*-
"""执行器契约 v1.1（T4'，Codex t-review 后升级）：身份约束的回执合并。

v0（六执行者协作轮）：origin / agent_id / tokens / duration_ms / cost_currency。
v1（T 梯队轮）：+ executor_started_at / executor_ended_at——执行器自报起止，
替代宿主转录的 attempt 认领-提交窗口（该窗口含播种与判卷等待，非执行区间）。
v1.1（本轮）：从"字段存储"升级为"有身份约束的回执合并"——
- 事务内核对 attempt 归属（mission/node 错配直接失败，不得改别人的账）；
- 数值校验：tokens / duration_ms 必须是非负数（0 合法，存在性不得用
  bool(value) 判定）；
- 时间校验：起止可解析（fromisoformat，容 Z）；带时区仅提示不拒；
  结束早于开始=逆序，拒绝；
- 平台控制字段（integration / goal_version 等）禁止被回执覆盖；
- 返回 contract_status：valid / legacy_incomplete（缺时序可保存+告警）；
  非法值直接抛 ContractError（不是"字段齐全无告警"）。
"""
from __future__ import annotations

import json
from datetime import datetime

from .repository import now

# 契约 v1 字段全集
CONTRACT_V1 = ("origin", "agent_id", "tokens", "duration_ms",
               "cost_currency", "executor_started_at", "executor_ended_at")

# 缺失时仅告警（不拒收）的时序字段
TIMING_FIELDS = ("executor_started_at", "executor_ended_at")

# 平台控制字段——回执不得覆盖（宿主记账与执行器回执的边界）
PROTECTED_FIELDS = ("integration", "goal_version", "sources", "origin_note")


class ContractError(ValueError):
    """回执违反契约：归属错配或字段非法——数据不变，明确报错。"""


# 平台结算状态字段（Codex 三轮"两处状态保护"）：外部回执补丁不得直接
# 覆盖——只允许 record_execution 内部结算路径经 _internal 写入
PROTECTED_SETTLE_FIELDS = ("settled_valid", "settled_status",
                           "cost_charged", "execution_final")


def apply_budget_delta(cur, mid: str, *, calls_delta: int = 0,
                       cost_delta: float = 0.0,
                       unknown_delta: int = 0) -> dict:
    """N1 唯一持久预算写入口：事务内读-改-写（增量语义）。

    所有预算写（结算费用/unknown 计数/派发预留）都必须经此函数在同一
    事务内完成——禁止任何路径以内存对象全量覆写 budget_json
    （Codex N1：派发 _budget_persist 全量覆写曾把已提交费用清零）。
    必须已在 repo.tx() 事务内调用。返回写后的预算 dict。
    """
    row = cur.execute("SELECT budget_json FROM missions WHERE id=?",
                      (mid,)).fetchone()
    b = json.loads(row["budget_json"] or "{}") if row else {}
    b["calls_used"] = int(b.get("calls_used", 0)) + int(calls_delta)
    b["cost_used"] = round(float(b.get("cost_used", 0.0))
                           + float(cost_delta), 9)
    b["cost_unknown"] = int(b.get("cost_unknown", 0)) + int(unknown_delta)
    cur.execute("UPDATE missions SET budget_json=?, updated_at=? WHERE id=?",
                (json.dumps(b, ensure_ascii=False), now(), mid))
    return b


def _parse_ts(value):
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"时间字段须为非空 ISO 字符串：{value!r}")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError as e:
        raise ContractError(f"时间字段不可解析（{value!r}）：{e}") from e


def merge_executor_resources(repo, mid: str, nid: str, attempt_id: str,
                             resources: dict,
                             budget_deltas: dict | None = None,
                             _internal: bool = False) -> dict:
    """把执行器回执 merge 进 attempt.resources_json（身份约束+校验）。

    归属：attempt.mission_id==mid 且 attempt.node_id==nid，错配抛
    ContractError（数据不变，不向别的任务写告警）。
    校验：tokens/duration_ms 非负数值（0 合法）；起止可解析且不逆序
    （缺时区仅告警——本地执行器常用无时区本地时间，如实记录）。
    兼容 v0：缺起止可保存，落 executor_contract_warning，
    contract_status=legacy_incomplete；非法值（负数/非时间/逆序）抛错。
    返回 {ok, contract_status, missing_timing, warnings, contract_fields}。
    """
    with repo.tx() as cur:
        row = cur.execute(
            "SELECT mission_id, node_id, resources_json FROM attempts"
            " WHERE id=?", (attempt_id,)).fetchone()
        if row is None:
            raise ContractError(f"attempt 不存在：{attempt_id}")
        if row["mission_id"] != mid or row["node_id"] != nid:
            raise ContractError(
                f"回执归属错配：attempt {attempt_id} 属于 "
                f"{row['mission_id']}/{row['node_id']}，不是 {mid}/{nid}"
                "（不得跨任务/节点改写资源账）")
        rj = json.loads(row["resources_json"] or "{}")

        warnings: list[str] = []
        # 平台控制字段保护（含结算状态字段；_internal=record_execution
        # 内部结算路径豁免——外部回执补丁不得覆盖这些字段）
        protected = set(PROTECTED_FIELDS)
        if not _internal:
            protected |= set(PROTECTED_SETTLE_FIELDS)
        for k in protected:
            if k in resources and resources[k] != rj.get(k):
                raise ContractError(
                    f"回执不得覆盖平台控制字段：{k}（当前值由平台记账）")
        # F3（Codex t-fix-review）：校验对象=合并后的候选完整回执，而非
        # 本次补丁——分次更新可存入逆序时间/NaN 的缺口由此闭合
        candidate = dict(rj)
        candidate.update(resources)
        import math as _math
        # 数值校验（对最终值；0 合法；NaN/Inf 非有限拒绝）
        for k in ("tokens", "duration_ms"):
            if k in candidate:
                v = candidate[k]
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    raise ContractError(
                        f"字段 {k} 须为数值，收到 {v!r}")
                if not _math.isfinite(v):
                    raise ContractError(
                        f"字段 {k} 须为有限数值，收到 {v!r}（NaN/Infinity 拒绝）")
                if v < 0:
                    raise ContractError(
                        f"字段 {k} 须为非负数值，收到 {v!r}")
        if "tokens" in candidate:
            v = candidate["tokens"]
            if isinstance(v, float) and not v.is_integer():
                raise ContractError(
                    f"tokens 语义为整数调用数，收到非整值 {v!r}")
        # 必需标识（对最终值）
        for k in ("origin", "agent_id"):
            if k in candidate and (not isinstance(candidate[k], str)
                                   or not candidate[k].strip()):
                raise ContractError(f"字段 {k} 须为非空字符串")
        # 时间校验（对最终合并值；缺的走 legacy 兼容）
        ts_start = ts_end = None
        if candidate.get("executor_started_at"):
            ts_start = _parse_ts(candidate["executor_started_at"])
            if ts_start.tzinfo is None:
                warnings.append("executor_started_at 无时区（按本地时间记录）")
        if candidate.get("executor_ended_at"):
            ts_end = _parse_ts(candidate["executor_ended_at"])
            if ts_end.tzinfo is None:
                warnings.append("executor_ended_at 无时区（按本地时间记录）")
        if ts_start and ts_end and ts_end < ts_start:
            raise ContractError(
                f"执行区间逆序（合并后最终值）："
                f"start={candidate['executor_started_at']} "
                f"end={candidate['executor_ended_at']}")

        rj = candidate
        missing = [k for k in TIMING_FIELDS
                   if k not in rj or rj.get(k) in (None, "")]
        events_needed = []
        if missing:
            events_needed.append(("executor_contract_warning", {
                "missing": ",".join(missing),
                "hint": "执行器回执缺时序字段（契约 v1）：起止应由执行器自报，"
                        "宿主转录口径不能替代执行区间"}))
        # F3：完整性对最终值判断——仅时间字段而无 origin/agent_id 等核心
        # 字段的回执是 incomplete，不得标 valid
        core_missing = [k for k in ("origin", "agent_id", "tokens",
                                    "duration_ms", "cost_currency")
                        if k not in rj]
        if core_missing:
            events_needed.append(("executor_contract_warning", {
                "missing": ",".join(core_missing),
                "hint": "执行器回执缺核心字段（契约 v1 完整性按合并后"
                        "最终值判断）"}))
        # R6.6（第二轮）：适配器如实标记的回执不完整（usage/cost/终态
        # 未确认）——平台不得重新判 valid（否则自动拆分门禁会受理
        # 不完整执行证据）。方向性采信：该标记只影响适配器自身的回执
        # 等级，归属约束保证不可用于改写他人回执。
        adapter_incomplete = (
            rj.get("receipt_status") == "incomplete"
            or (isinstance(rj.get("usage_stats"), dict)
                and rj["usage_stats"].get("usage_complete") is False))
        if adapter_incomplete:
            warnings.append("适配器标记回执不完整（usage/cost/终态未确认）"
                            "——平台不判 valid")
        for w in warnings:
            events_needed.append(("executor_contract_warning",
                                  {"warning": w}))
        for etype, payload in events_needed:
            repo.append_event(cur, mid, etype, nid,
                              attempt=attempt_id, **payload)
        cur.execute("UPDATE attempts SET resources_json=? WHERE id=?",
                    (json.dumps(rj, ensure_ascii=False), attempt_id))
        if budget_deltas:
            # Codex R1/N1：预算增量与回执落库同事务，且经统一入口
            # 读-改-写（不与派发路径的全量覆写互踩）
            apply_budget_delta(cur, mid, **budget_deltas)
    if not missing and not core_missing and not warnings:
        status = "valid"
    elif missing or core_missing:
        status = "legacy_incomplete"
    elif adapter_incomplete:
        # 字段齐全但适配器标记不完整——按 legacy_incomplete 处理
        # （不判 valid，后续完整补证可经差额规则升级）
        status = "legacy_incomplete"
    else:
        status = "valid_with_warnings"
    return {
        "ok": True,
        "contract_status": status,
        "missing_timing": missing,
        "missing_core": core_missing,
        "warnings": warnings,
        "contract_fields": [k for k in CONTRACT_V1 if k in rj],
    }


# 结算序列专用锁（Codex 修正二版）：不可用 repo._lock——merge 内部
# repo.tx() 会再取同一把不可重入锁（同线程死锁）。独立锁与 repo._lock
# 只按单一方向嵌套（_SETTLE_LOCK → repo._lock），无环。
_SETTLE_LOCK = __import__("threading").Lock()


def record_execution(repo, attempt_id: str, run_result: dict,
                     mid: str | None = None, nid: str | None = None,
                     on_charge=None) -> dict:
    """统一结算入口（Codex 修正三版：持久结算流水为权威）。

    - 归属先行：显式传入的 mid/nid 与 attempt 不符 → ContractError，
      **在幂等检查之前**（已 final 的错误归属重放同样拒绝）；
    - 持久原子（R1）：回执落库、cost_charged 结算流水、费用累计
      （missions.budget_json）在**同一 SQLite 事务**内提交——进程在任意
      持久边界崩溃/异常后，重启累计正确且不重复；on_charge 只同步内存
      Budget（不再承担持久化，scheduler 不再二次 persist）；
    - 差额规则（R2）：持久 cost_charged={amount,basis} 为已计权威——
      新回执 known（reported/estimated）：candidate=amount，delta=candidate
      -charged.amount（unknown→known 补差、known→同额零差、known→修正
      总额补差）；新回执 unknown：金额维持 charged，delta=0，仅计数；
      重复补丁增量=0；
    - final 语义（R3）：settled_valid 只以**平台校验后**的
      contract_status=="valid" 打上（不自报 receipt_status）；自报 valid
      缺字段→legacy_incomplete→settled_valid 不打→后续完整补证可升级；
      重放返回库内真实 contract_status（不硬编码）；
    - 旧口径兼容（R4）：cost 结构缺失而 resources 含平铺 cost_usd →
      转结构化 {amount, basis:reported}（旧 Budget.record 同语义），
      不静默记 unknown。
    """
    with _SETTLE_LOCK:
        row = repo._conn.execute(
            "SELECT mission_id, node_id, resources_json FROM attempts"
            " WHERE id=?", (attempt_id,)).fetchone()
        if row is None:
            raise ContractError(f"attempt 不存在：{attempt_id}")
        # R3：归属核对先行（幂等/重放之前）
        if mid is not None and row["mission_id"] != mid:
            raise ContractError(
                f"回执归属错配：attempt {attempt_id} 属于 "
                f"{row['mission_id']}，不是 {mid}")
        if nid is not None and row["node_id"] != nid:
            raise ContractError(
                f"回执归属错配：attempt {attempt_id} 属于 "
                f"{row['node_id']}，不是 {nid}")
        mid = mid or row["mission_id"]
        nid = nid or row["node_id"]
        rj0 = json.loads(row["resources_json"] or "{}")
        resources = dict(run_result.get("resources") or {})
        # R4/N4：费用规范化（平铺 cost_usd → 结构化）必须在幂等/回放
        # 判定**之前**——否则已终局回执的平铺更正会被误当 unknown 重放
        cost = resources.get("cost")
        if cost is None and resources.get("cost_usd") is not None:
            cost = {"amount": float(resources["cost_usd"]),
                    "basis": "reported"}
            resources["cost"] = cost
        if cost is not None and not isinstance(cost, dict):
            raise ContractError(
                f"cost 须为结构化 {{amount,basis}}，收到 {cost!r}")
        basis = (cost or {}).get("basis", "unknown")
        amount = (cost or {}).get("amount")
        # 幂等重放（归属校验之后、费用规范化之后）：settled_valid=平台
        # 校验后的终局标记。同额（或 unknown）回执 → replayed（返回库内
        # 真实状态，不计费）；金额不同的 known 回执 = 修正总额（Codex R2
        # 规则）→ 不重放，走差额结算
        _new_amt = (cost or {}).get("amount")
        _new_basis = (cost or {}).get("basis", "unknown")
        if rj0.get("settled_valid") and (
                _new_basis == "unknown" or
                _new_amt is None or
                abs(float(_new_amt) - float(
                    (rj0.get("cost_charged") or {}).get("amount") or 0.0))
                < 1e-9):
            return {"ok": True, "replayed": True, "upgraded": False,
                    "contract_status": rj0.get("settled_status", "valid"),
                    "cost_delta": 0.0, "cost_basis": "replayed"}
        # N2：历史迁移——旧协议已结算（execution_final）但无 cost_charged
        # 流水：信任历史累计（budget_json 已含该笔），补流水/终局标记，
        # 不产生增量；后续更正以迁移基线走正常差额
        if (rj0.get("execution_final")
                and not rj0.get("cost_charged")
                and not rj0.get("settled_valid")):
            base_amt = float(amount) if (amount is not None and basis in
                                         ("reported", "estimated")) else 0.0
            resources["cost_charged"] = {"amount": base_amt, "basis": basis}
            resources["settled_valid"] = True
            resources["settled_status"] = "legacy_migrated"
            merged = merge_executor_resources(repo, mid, nid, attempt_id,
                                              resources, _internal=True)
            if on_charge is not None:
                on_charge(0.0, "migrated")
            return {"ok": True, "replayed": False, "upgraded": False,
                    "contract_status": "legacy_migrated",
                    "cost_delta": 0.0, "cost_basis": "migrated"}
        # R2：差额规则——cost_charged 为已计权威（持久于 resources）
        charged = rj0.get("cost_charged") or {"amount": 0.0,
                                              "basis": "unknown"}
        charged_amt = float(charged.get("amount") or 0.0)
        if basis in ("reported", "estimated"):
            candidate_amt = float(amount) if amount is not None else charged_amt
            delta = round(candidate_amt - charged_amt, 9)
            new_charged = {"amount": candidate_amt, "basis": basis}
        else:  # unknown：金额维持已计，仅计数
            candidate_amt = charged_amt
            delta = 0.0
            new_charged = {"amount": charged_amt, "basis": charged.get(
                "basis", "unknown")}
        resources["cost_charged"] = new_charged
        # N1/R1：费用增量与 unknown 计数（仅首次 unknown 结算——重复
        # unknown 补丁不重复计数，Codex N3）与回执落库同事务，
        # 经统一持久预算接口读-改-写（不与派发路径互踩）
        unknown_delta = 1 if (basis == "unknown"
                              and not rj0.get("cost_charged")) else 0
        merged = merge_executor_resources(repo, mid, nid, attempt_id,
                                          resources, _internal=True,
                                          budget_deltas={
                                              "cost_delta": delta,
                                              "unknown_delta": unknown_delta})
        status = merged["contract_status"]
        # 升级=此前未终局 + 本次达到平台校验 valid（不看中间态自报）
        upgraded = bool(not rj0.get("settled_valid") and status == "valid")
        # R3：settled_valid/settled_status 以平台校验后的状态打（第二小
        # 写库，锁内；未打的崩溃窗口由 cost_charged 差额幂等兜底——
        # 重放 delta=0）。终局后若更正回执退为 incomplete，同步清除
        # valid 终局标记（Codex 三轮"两处状态保护"）
        want_valid = (status == "valid")
        if bool(rj0.get("settled_valid")) != want_valid:
            with repo.tx() as cur:
                cur.execute(
                    "UPDATE attempts SET resources_json=? WHERE id=?",
                    (json.dumps({**json.loads(
                        repo.get_attempt(attempt_id).resources_json
                        or "{}"), "settled_valid": want_valid,
                        "settled_status": status},
                        ensure_ascii=False), attempt_id))
        if on_charge is not None and (delta != 0.0 or basis == "unknown"):
            on_charge(delta, basis)  # 仅同步内存 Budget（持久已同事务）
        return {"ok": True, "replayed": False, "upgraded": upgraded,
                "contract_status": status,
                "cost_delta": delta, "cost_basis": basis}
