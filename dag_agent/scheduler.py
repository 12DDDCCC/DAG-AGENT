"""v3 步骤4 → v3.2：调度器——事件推进、并发上限、预算熔断、整合播种、动态入队。

调度器是程序：不做模型调用判断；每轮 tick 只做"谁就绪→认领→派发"。
v3.2 变更（协议02/03）：
- 统一播种：叶子与整合尝试都按 attempt.input_binding_json 的绑定快照取完整输入，
  播种前逐文件校验摘要；缺件/不符 → 尝试终止 error、节点转 blocked（不热循环）。
- 执行期周期心跳（线程）+ 收割接入主循环（protected=本服务在途集合）。
- 预算持久：calls/cost 落 missions.budget_json，重启不重置。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .coordinator import Proposal, review_proposal
from .graph import GraphError
from .recovery import RetryPolicy, heartbeat
from .repository import now as _now


@dataclass
class AutogrowConfig:
    """自动生长护栏（A-G1 §7：默认开启；计数持久于 split_proposals，重启不清零）。"""
    enabled: bool = True
    max_splits_per_mission: int = 5
    max_depth: int = 5
    max_pending_per_node: int = 3   # 同节点有限待审/拒绝提案数


@dataclass
class Budget:
    max_calls: int | None = None
    max_cost_usd: float | None = None
    calls_used: int = 0
    cost_used: float = 0.0
    cost_unknown: int = 0   # 费用未知的结算笔数（不按 0 冒充已确认）

    def allow(self) -> tuple[bool, str]:
        if self.max_calls is not None and self.calls_used >= self.max_calls:
            return False, f"调用次数熔断（{self.calls_used}/{self.max_calls}）"
        if self.max_cost_usd is not None and self.cost_used >= self.max_cost_usd:
            return False, f"费用熔断（${self.cost_used:.2f}/${self.max_cost_usd:.2f}）"
        return True, "ok"

    def record(self, resources: dict):
        """费用记账（阶段 1A 口径）：结构化 cost 只对 reported/estimated 加
        金额；unknown 只计数（缺失不按已确认零费用展示）。旧 flat
        cost_usd（stub 回执）兼容。"""
        cost = resources.get("cost")
        if isinstance(cost, dict):
            basis = cost.get("basis")
            amount = cost.get("amount")
            if amount is not None and basis in ("reported", "estimated"):
                self.cost_used += float(amount)
            elif basis == "unknown":
                self.cost_unknown += 1
            return
        self.cost_used += float(resources.get("cost_usd") or 0.0)


@dataclass
class Timeline:
    """并行证据：dispatch/finish 时刻（monotonic 秒）。"""
    events: list = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, nid, aid, phase):
        with self.lock:
            self.events.append((nid, aid, phase, time.monotonic()))

    def overlap_pairs(self) -> int:
        """存在执行区间重叠的尝试对数（并行证据）。"""
        spans = {}
        for nid, aid, phase, t in self.events:
            spans.setdefault(aid, {})[phase] = t
        done = [(v["start"], v["end"]) for v in spans.values()
                if "start" in v and "end" in v]
        n = 0
        for i in range(len(done)):
            for j in range(i + 1, len(done)):
                a, b = done[i], done[j]
                if a[0] < b[1] and b[0] < a[1]:
                    n += 1
        return n


class _HeartbeatLoop:
    """执行期周期心跳（协议03 配套）：长任务不被误收割。"""

    def __init__(self, repo, aid: str, interval: float):
        self._repo = repo
        self._aid = aid
        self._interval = max(float(interval), 0.5)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _loop(self):
        while not self._stop.wait(self._interval):
            try:
                heartbeat(self._repo, self._aid)
            except Exception:
                pass  # 心跳失败不杀执行；收割端事务内核验兜底

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)


class Scheduler:
    def __init__(self, repo, graph, backend, max_concurrency: int = 2,
                 budget: Budget | None = None,
                 retry_policy: RetryPolicy | None = None,
                 heartbeat_interval: float = 10.0,
                 reap_after: float = 300.0,
                 reap_enabled: bool = True,
                 node_cap: int | None = None,
                 auto_split: AutogrowConfig | None = None,
                 planner=None):
        self.repo = repo
        self.graph = graph
        self._brief_warned: set[str] = set()  # A3' 简报拒已告警节点（防每 tick 刷屏）
        self.backend = backend
        self.max_concurrency = max_concurrency
        self.budget = budget or Budget()
        self.retry_policy = retry_policy or RetryPolicy(max_retries=0)
        self.heartbeat_interval = heartbeat_interval
        self.reap_after = reap_after
        self.reap_enabled = reap_enabled
        self.node_cap = node_cap          # v3.2：节点数熔断（防分解失控繁殖）
        # A-G1：自动生长（默认关闭——enabled=False 时不检测不落图）
        self.auto_split = auto_split or AutogrowConfig()
        from .autogrow import DeterministicPlannerStub
        self.planner = planner or DeterministicPlannerStub()
        self.stop_requested = threading.Event()  # v3.2：服务停止（drain）
        # Codex R5：kill 专属硬停——已认领但未开始执行的尝试在执行起点
        # 即取消（与 drain"在途有界完成"语义分离）
        self.hard_stop = threading.Event()
        self.timeline = Timeline()
        self.counters: dict[str, int] = {"project_errors": 0}
        self._pool: ThreadPoolExecutor | None = None
        self._futures: dict[str, object] = {}
        self._cancel_events: dict[str, threading.Event] = {}  # 1B：attempt→取消事件
        self._inflight_lock = threading.Lock()
        self._budget_loaded: set[str] = set()
        self.halt_reason: str | None = None
        # D1（2026-11-08 独立复查 P2）：供应商错误分类停派——
        # 临时限流=指数退避（不耗节点重试预算）；周期额度/余额/认证类=
        # 停止派发待人工确认（provider_pause 持久化，重启不重置）。
        # 分类输入只来自后端回执的结构化 error_class 字段（协议错误
        # 通道），不扫描模型输出正文。
        self.provider_cooldown_until: float = 0.0   # monotonic 退避截止
        self.provider_pause: dict | None = None     # 人工确认暂停状态
        self._rate_streak: int = 0                  # 连续限流计数
        self.RATE_LIMIT_MAX_STREAK = 8              # 连续限流转人工阈值
        self._provider_backoff_base_s = 60          # 首次退避秒数
        self._provider_backoff_cap_s = 900          # 退避上限（15 分钟）

    # ---- 预算持久（协议03§6：重启不重置） ----

    def _budget_load(self, mid: str):
        """内存预算=持久账权威（Codex N1：budget_json 是唯一真源，
        内存只是缓存——禁止 max 式合并掩盖持久账）。"""
        if mid in self._budget_loaded:
            return
        self._budget_loaded.add(mid)
        try:
            data = json.loads(self.repo.get_mission(mid).budget_json or "{}")
        except Exception:
            return
        self.budget.calls_used = int(data.get("calls_used", 0))
        self.budget.cost_used = float(data.get("cost_used", 0.0))
        self.budget.cost_unknown = int(data.get("cost_unknown", 0))

    def _budget_reserve_call(self, mid: str):
        """派发预留：经统一持久预算接口增量记账（Codex N1——禁止内存
        全量覆写 budget_json），并同步内存缓存。"""
        from .executor_contract import apply_budget_delta
        with self.repo.tx() as cur:
            apply_budget_delta(cur, mid, calls_delta=1)
        self.budget.calls_used += 1

    # ---- 停止权限（R2/Codex 复核：撤销应用权必须可串行化） ----

    def request_stop(self, mode: str = "drain", reason: str = "") -> None:
        """停止请求的**权威**通道：旗标持久化到 run_control（经 repo.tx
        写库——与 apply 事务共用 SQLite 写锁，构成线性化点：stop 的旗标
        写要么先于 apply 的 gate 读提交（gate 拒绝），要么等 apply 提交后
        落地（撤销前已完成的合法应用）。Event 仍在库写之后置位，只作
        tick/worker 唤醒的快速通道。"""
        sr = 1 if mode in ("drain", "kill") else 0
        hs = 1 if mode == "kill" else 0
        with self.repo.tx() as cur:
            cur.execute(
                "INSERT INTO run_control(id,stop_requested,hard_stop,"
                "generation,updated_at) VALUES(1,?,?,0,?)"
                " ON CONFLICT(id) DO UPDATE SET"
                " stop_requested=MAX(stop_requested,excluded.stop_requested),"
                " hard_stop=MAX(hard_stop,excluded.hard_stop),"
                " updated_at=excluded.updated_at",
                (sr, hs, _now()))
        self.stop_requested.set()
        if mode == "kill":
            self.hard_stop.set()

    def clear_stop_flags(self) -> None:
        """新运行代际：清停止旗标、代际+1（RunManager.start 在恢复后
        调用——旧运行的撤销不约束新代际，恢复重放按新权限走）。"""
        with self.repo.tx() as cur:
            cur.execute(
                "INSERT INTO run_control(id,stop_requested,hard_stop,"
                "generation,updated_at) VALUES(1,0,0,1,?)"
                " ON CONFLICT(id) DO UPDATE SET stop_requested=0,"
                " hard_stop=0, generation=generation+1,"
                " updated_at=excluded.updated_at", (_now(),))
        self.stop_requested.clear()
        self.hard_stop.clear()

    def _stop_revoked_db(self) -> bool:
        """库内停止旗标读取（apply gate 权威判据）。调用点在 apply 写
        事务内（同连接同事务可见性）；异常时退回 Event 快照（不比库内
        更宽松地放行——Event 只会更早置位）。"""
        try:
            rc = self.repo.get_run_control()
            return rc["stop_requested"] or rc["hard_stop"]
        except Exception:
            return self.stop_requested.is_set() or self.hard_stop.is_set()

    # ---- 派发 ----

    def tick_dispatch(self, mid: str) -> int:
        """把当前就绪节点派发出去；返回本次派发数。"""
        if self.stop_requested.is_set() or self.hard_stop.is_set():
            return 0  # Codex R5：停止信号在认领前检查（防快照后新认领）
        # D1：供应商暂停（额度/余额/认证）——不派发，halt 语义等待人工
        if self.provider_pause is not None:
            self.halt_reason = (f"供应商暂停（{self.provider_pause.get('error_class')}）："
                                f"{self.provider_pause.get('reason', '')[:120]}")
            return 0
        # D1：限流退避窗口内不派发（节点保持在待派，不消耗重试预算）
        if time.monotonic() < self.provider_cooldown_until:
            return 0
        self._budget_load(mid)
        if self.node_cap is not None:
            n_nodes = len(self.repo.list_nodes(mid))
            if n_nodes >= self.node_cap:
                self.halt_reason = f"节点数熔断（{n_nodes}/{self.node_cap}）"
                return 0
        with self._inflight_lock:
            inflight = len(self._futures)
        if inflight >= self.max_concurrency:
            return 0
        targets: list[tuple[str, bool, int]] = []
        with self.repo.tx() as cur:
            for n in self.repo.list_nodes(mid):
                if n.status != "failed" or not self.retry_policy.allows(
                        self.repo, n.id):
                    continue
                # v3.2：OR 组成员耗尽切换阈值后不再重试本支（换下一支）
                if n.group_id is not None and not self.graph._member_dispatchable(
                        cur, n):
                    continue
                targets.append((n.id, False, n.priority))  # 失败自动重试（最优先）
        for nid in self.graph.frontier(mid):
            n = self.repo.get_node(nid)
            targets.append((nid, False, n.priority))
        for nid in self.graph.integration_ready(mid):
            n = self.repo.get_node(nid)
            targets.append((nid, True, n.priority))
        # v3.1 优先级调度（Anthropic 原版"优先完成 Mazur"）：大者先派，同权按 id 稳定
        targets.sort(key=lambda t: (-t[2], t[0]))
        dispatched = 0
        for nid, integration, _prio in targets:
            ok, why = self.budget.allow()  # 预算按节点下沉检查
            if not ok:
                self.halt_reason = why
                break
            with self._inflight_lock:
                if len(self._futures) >= self.max_concurrency:
                    break
                if nid in self._futures:
                    continue
            try:
                aid = self.graph.start_attempt(mid, nid, backend=self.backend.name)
            except GraphError as e:
                # A3' 调度可见性（Codex 63bf8d2 复核）：缺段简报等结构性
                # 拒绝不能静默吞掉——首次落事件说明原因，避免节点永远
                # pending 而外界只看到"无进展"；同因不重复刷屏
                msg = str(e)
                if "简报五要素" in msg:
                    if nid not in self._brief_warned:
                        self._brief_warned.add(nid)
                        try:
                            with self.repo.tx() as cur:
                                self.repo.append_event(
                                    cur, mid, "brief_gate_rejected", nid,
                                    reason=msg[:300])
                        except Exception:
                            pass
                continue  # 状态性拒绝（被并发抢先等）下一轮再看
            except Exception:
                continue  # 被并发抢先或状态变化，下一轮再看
            self._budget_reserve_call(mid)  # 派发即预留（统一接口）
            fut = self._pool.submit(self._work, mid, nid, aid, integration)
            with self._inflight_lock:
                self._futures[aid] = fut
            self.timeline.add(nid, aid, "start")
            dispatched += 1
            if integration:
                break  # 整合一次只派一个（它是汇聚点）
        # A-G1：待规划提案派规划任务（与执行共享执行位；规划受统一
        # 并发/停止管理，不占执行 worker 同步等待）
        dispatched += self._dispatch_planning(mid)
        return dispatched

    # ---- v3.2 统一播种（协议02：叶子与整合同规则） ----

    def _seed_bound(self, aid: str, workdir: Path):
        """播种（协议02）：按认领时的**不可变绑定契约**核对后取完整输入。
        F3（Codex 三轮）：认领后、播种前快照可能被损坏（如 files_json 被
        清空）——有 dep_bindings 契约的来源必须再次核对整体 manifest 与
        逐文件摘要；集合变化/缺件/空清单一律 fail-closed（尝试终止
        error、节点 blocked，不派执行者）。"""
        import hashlib as _hl
        from .artifacts import manifest_hash_of as _mho
        att = self.repo.get_attempt(aid)
        contracts: dict[str, dict] = {}
        try:
            for c in self.repo._conn.execute(
                    "SELECT depends_on, snapshot_id, manifest_hash,"
                    " files_json, files_meta_json FROM dep_bindings"
                    " WHERE node_id=?", (att.node_id,)).fetchall():
                contracts[c["depends_on"]] = dict(c)
        except Exception:
            contracts = {}
        for b in json.loads(att.input_binding_json or "[]"):
            sid = b.get("snapshot_id")
            src = b.get("source")
            if not sid:
                if src in contracts and json.loads(
                        contracts[src].get("files_meta_json") or "[]"):
                    raise ValueError(
                        f"输入契约要求文件但绑定无快照：{src}——拒绝播种"
                        "（空输入≠合法）")
                continue  # 来源无工件（空交付）——无需播种
            snap = self.repo.snapshot_by_id(sid)
            if snap is None or not snap.valid:
                raise ValueError(f"绑定快照缺失或已失效：{b['source']}(snap {sid})")
            # N3 通用核验（Codex 四轮，所有来源同规则）：当前快照清单
            # 规范化重算=认领账本 manifest_hash。账本值独立于 snapshots
            # 表——清单清空/内容改写/字段联合改写（同 ID 同文件名）都会
            # 使重算失配；空清单≠合法无输入。
            claimed_hash = b.get("manifest_hash") or ""
            meta = json.loads(snap.files_json or "[]")
            recomputed = _mho(meta) if meta else ""
            # B2（Codex 五轮）：身份三元组——内容之外，**来源节点与
            # 版本**也必须与账本一致（同内容改 node_id/artifact_version
            # 会让文件进错目录/读到旧版本）
            if str(snap.node_id) != str(src):
                raise ValueError(
                    f"播种身份核验失败：{src} 快照 {sid} 实际来源为 "
                    f"{snap.node_id}（来源被改写）——拒绝播种")
            claimed_ver = b.get("artifact_version")
            if (claimed_ver is not None
                    and snap.artifact_version is not None
                    and int(snap.artifact_version) != int(claimed_ver)):
                raise ValueError(
                    f"播种身份核验失败：{src} 快照 {sid} 版本 "
                    f"{snap.artifact_version} 与账本 {claimed_ver} 不符"
                    "（版本被改写）——拒绝播种")
            if not claimed_hash:
                raise ValueError(
                    f"认领绑定缺 manifest 基准：{src}——拒绝播种")
            if recomputed != claimed_hash:
                raise ValueError(
                    f"播种前核验失败：{src} 快照与认领账本 manifest 不符"
                    f"（快照 {sid}）——拒绝播种，人工处置")
            if not meta:
                raise ValueError(
                    f"播种前核验失败：{src} 快照文件清单为空（认领申报"
                    f"{len(b.get('files') or [])} 件）——空清单≠合法"
                    "无输入，拒绝播种")
            ctr = contracts.get(src)
            if ctr is not None:
                # N3 加强核验（自动孩子继承契约）：契约 manifest 复核
                if recomputed != (ctr["manifest_hash"] or ""):
                    raise ValueError(
                        f"播种前契约复核失败：{src} 快照 manifest 与认领"
                        f"契约不符（快照 {sid}）——拒绝播种，人工处置")
                if not json.loads(ctr["files_meta_json"] or "[]"):
                    raise ValueError(
                        f"播种前契约复核失败：{src} 契约无文件元信息但绑定"
                        "有快照——拒绝播种")
            self.graph.store.seed_from_snapshot(snap, workdir)
            if ctr is not None:
                # N3 加强：播种内容与继承契约逐文件 sha 比对（播种目录
                # 可带 inputs/<src>/ 前缀——按 rel 后缀匹配）
                for fm in json.loads(ctr["files_meta_json"] or "[]"):
                    hits = [q for q in workdir.rglob(Path(fm["rel_path"]).name)
                            if q.is_file() and q.relative_to(workdir)
                            .as_posix().endswith(fm["rel_path"])]
                    if not hits:
                        raise ValueError(
                            f"播种缺件：{src}/{fm['rel_path']}——按契约"
                            "核对比播种声明更严，拒绝")
                    if not any(_hl.sha256(q.read_bytes()).hexdigest()
                               == fm.get("sha256") for q in hits):
                        raise ValueError(
                            f"播种内容与认领契约不符：{src}/"
                            f"{fm['rel_path']}（sha 不一致）——拒绝")

    def _abort_seed_error(self, mid: str, nid: str, aid: str, why: str):
        """播种失败（协议02§3）：尝试终止 error、节点转 blocked（防热循环）。"""
        ts = _now()
        with self.repo.tx() as cur:
            cur.execute(
                "UPDATE attempts SET status='error', category='error', ended_at=?"
                " WHERE id=? AND status='running'", (ts, aid))
            cur.execute("UPDATE nodes SET status='blocked', updated_at=? WHERE id=?",
                        (ts, nid))
            self.repo.append_event(cur, mid, "seed_invalid", nid, attempt=aid,
                                   reason=why[:200],
                                   hint="输入快照缺件/损坏/失效：拒绝改绑，"
                                        "人工处置后重派")

    # ---- 阶段 1B：取消接到具体执行进程 ----

    def cancel_attempt(self, aid: str, reason: str = "cancelled") -> dict:
        """取消在途执行：通知后端停进程（10s 内退出）+ 撤销提交权
        （attempt running→cancelled）+ 节点回待派（非 failed/accepted）。
        与 graph.cancel_node（永久取消节点）语义不同：本方法只取消执行，
        节点可重派。迟到结果不发布（_work_inner 提交前双检）。"""
        with self._inflight_lock:
            ev = self._cancel_events.get(aid)
        ts = _now()
        with self.repo.tx() as cur:
            row = cur.execute(
                "SELECT node_id, mission_id, status FROM attempts WHERE id=?",
                (aid,)).fetchone()
            if row is None:
                raise GraphError(f"attempt 不存在：{aid}")
            if row["status"] == "running":
                cur.execute(
                    "UPDATE attempts SET status='cancelled',"
                    " category='cancel', ended_at=? WHERE id=?", (ts, aid))
                self.repo.append_event(
                    cur, row["mission_id"], "attempt_cancelled",
                    row["node_id"], attempt=aid, reason=reason[:200],
                    origin="scheduler.cancel_attempt")
            nd = cur.execute("SELECT status FROM nodes WHERE id=?",
                             (row["node_id"],)).fetchone()
            if nd and nd["status"] == "in_work":
                cur.execute(
                    "UPDATE nodes SET status='pending', updated_at=?"
                    " WHERE id=?", (ts, row["node_id"]))
                self.repo.append_event(
                    cur, row["mission_id"], "node_reset_pending",
                    row["node_id"], attempt=aid,
                    reason="取消执行：节点回待派（非 failed/accepted）")
        if ev is not None:
            ev.set()  # 通知后端停止进程树（cc_backend 10s 内确认退出）
        return {"cancelled": aid}

    # ---- D1：供应商错误分类停派（2026-11-08；R5 第二轮强化） ----

    def _persist_provider_state(self, mid: str, *, include_pause: bool) -> None:
        """供应商保护状态持久化（R5：重启不能清零——冷却截止、连续
        限流次数、暂停原因都进 run_control.params_json；冷却用 UTC
        绝对时刻存取，monotonic 只在内存换算）。UPSERT 兼容无行库。"""
        with self.repo.tx() as cur:
            row = cur.execute(
                "SELECT params_json FROM run_control WHERE id=1").fetchone()
            p = json.loads(row["params_json"] or "{}") if row else {}
            from datetime import datetime, timezone
            wall_until = None
            if self.provider_cooldown_until > time.monotonic():
                remain = self.provider_cooldown_until - time.monotonic()
                wall_until = (datetime.now(timezone.utc)
                              .isoformat(timespec="seconds"))
                wall_until = (datetime.fromisoformat(wall_until)
                              .timestamp() + remain)
                from datetime import datetime as _dt
                wall_until = _dt.fromtimestamp(
                    wall_until, tz=timezone.utc).isoformat()
            p["provider_cooldown"] = {"until_utc": wall_until,
                                      "rate_streak": self._rate_streak}
            if include_pause:
                p["provider_pause"] = self.provider_pause
            cur.execute(
                "INSERT INTO run_control(id, stop_requested, hard_stop,"
                " generation, params_json, updated_at)"
                " VALUES(1, 0, 0, 0, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET"
                " params_json=excluded.params_json,"
                " updated_at=excluded.updated_at",
                (json.dumps(p, ensure_ascii=False), _now()))

    # 兼容旧调用名（pause 场景）
    def _persist_provider_pause(self, mid: str) -> None:
        self._persist_provider_state(mid, include_pause=True)

    def restore_provider_state(self, params: dict) -> None:
        """R5：新进程/新 Scheduler 从持久化配置恢复供应商保护状态——
        冷却截止（UTC→monotonic 换算）、连续限流次数、暂停状态。
        RunManager.start 在创建调度器后调用；无记录时为空操作。"""
        pc = (params or {}).get("provider_cooldown") or {}
        from datetime import datetime, timezone
        until_utc = pc.get("until_utc")
        if until_utc:
            try:
                deadline = datetime.fromisoformat(until_utc)
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
                remain = (deadline - datetime.now(timezone.utc)).total_seconds()
                if remain > 0:
                    self.provider_cooldown_until = time.monotonic() + remain
            except (ValueError, TypeError):
                pass
        streak = pc.get("rate_streak")
        if isinstance(streak, int) and not isinstance(streak, bool) \
                and streak > self._rate_streak:
            self._rate_streak = streak
        pp = (params or {}).get("provider_pause")
        if pp:
            self.provider_pause = dict(pp)
            self.halt_reason = (f"供应商暂停（{pp.get('error_class')}）："
                                f"{str(pp.get('reason', ''))[:120]}")

    def _set_provider_pause(self, mid: str, nid: str, aid: str,
                            err_class: str, why: str) -> None:
        ts = _now()
        self.provider_pause = {"error_class": err_class, "reason": why[:200],
                               "attempt": aid, "node": nid, "at": ts}
        self.halt_reason = f"供应商暂停（{err_class}）：{why[:120]}"
        with self.repo.tx() as cur:
            self.repo.append_event(
                cur, mid, "provider_paused", nid, attempt=aid,
                error_class=err_class, reason=why[:200],
                hint="额度/余额/认证类错误：停止派发，人工确认后以 "
                     "provider_pause_ack=True 恢复（重启不自动重置）")
        self._persist_provider_pause(mid)
        # 事务外取消本 attempt（节点回待派，不耗重试预算）——
        # cancel_attempt 自带事务，不可在 repo.tx 内嵌套调用
        try:
            self.cancel_attempt(aid, reason=f"provider {err_class}：暂停派发")
        except Exception:
            pass

    def _handle_provider_error(self, mid: str, nid: str, aid: str,
                               err_class: str, run1: dict) -> str:
        """结构化供应商错误分级响应。返回 'skip_submit'（本 attempt 不再
        提交判卷，账本已收尾）或 'submit'（按普通失败走判卷/重试预算）。
        分类输入=后端回执的 error_class 字段（协议错误通道），模型输出
        正文中的错误码字样不构成触发条件。"""
        if err_class == "rate_limited":
            self._rate_streak += 1
            if self._rate_streak >= self.RATE_LIMIT_MAX_STREAK:
                self._set_provider_pause(
                    mid, nid, aid, err_class,
                    f"连续限流 {self._rate_streak} 次（退避未缓解）——转人工处置")
                return "skip_submit"
            # R5 / A5：有效 Retry-After 优先（0 < hint <= 86400s 时严格按
            # 服务端指定时长等待，不因超过本地指数退避 cap 900s 而回退到
            # 更短退避导致提前请求；若超过 86400s 则转暂停）
            hint = (run1.get("resources") or {}).get("retry_after_s")
            if isinstance(hint, (int, float)) and not isinstance(hint, bool) \
                    and hint > 86400:
                self._set_provider_pause(
                    mid, nid, aid, err_class,
                    f"服务端 Retry-After={hint}s 超长——暂停至人工确认")
                return "skip_submit"
            if isinstance(hint, (int, float)) and not isinstance(hint, bool) \
                    and 0 < hint <= 86400:
                backoff = float(hint)
                hint_used = True
            else:
                backoff = min(self._provider_backoff_base_s
                              * (2 ** (self._rate_streak - 1)),
                              self._provider_backoff_cap_s)
                hint_used = False
            self.provider_cooldown_until = time.monotonic() + backoff
            with self.repo.tx() as cur:
                self.repo.append_event(
                    cur, mid, "provider_cooldown", nid, attempt=aid,
                    error_class=err_class, streak=self._rate_streak,
                    backoff_s=backoff, retry_after_used=hint_used,
                    hint="临时限流：退避；节点回待派，不耗重试预算")
            self._persist_provider_state(mid, include_pause=False)
            try:
                self.cancel_attempt(
                    aid, reason=f"provider rate_limited：退避 {backoff}s")
            except Exception:
                pass
            return "skip_submit"
        if err_class in ("quota_exhausted", "balance_exhausted", "auth_failed"):
            self._set_provider_pause(
                mid, nid, aid, err_class,
                run1.get("summary") or f"供应商报告 {err_class}")
            return "skip_submit"
        return "submit"   # service_unavailable / model_unavailable / unknown

    def _work(self, mid: str, nid: str, aid: str, integration: bool):
        # R2：整个执行包 try/except/finally——后端抛错也必须结算并释放执行位，
        # 不留 protected 僵尸；finally 保证 _futures 必弹出。
        ev = threading.Event()
        with self._inflight_lock:
            self._cancel_events[aid] = ev
        try:
            self._work_inner(mid, nid, aid, integration, ev)
        except Exception as e:  # backend 崩溃/未知异常：按环境类 error 结算
            ts = _now()
            try:
                with self.repo.tx() as cur:
                    cur.execute(
                        "UPDATE attempts SET status='error', category='error',"
                        " ended_at=? WHERE id=? AND status='running'",
                        (ts, aid))
                    cur.execute(
                        "UPDATE nodes SET status='blocked', updated_at=?"
                        " WHERE id=? AND status='in_work'", (ts, nid))
                    self.repo.append_event(
                        cur, mid, "worker_error", nid, attempt=aid,
                        error=f"{type(e).__name__}: {str(e)[:180]}",
                        hint="执行器异常：unblock/修复后可重派")
            except Exception:
                pass
        finally:
            self.timeline.add(nid, aid, "end")
            with self._inflight_lock:
                self._futures.pop(aid, None)
                self._cancel_events.pop(aid, None)

    def _backend_run(self, statement, workdir, check_cmd, title,
                     cancel_event):
        """调用后端执行：新签名后端（ClaudeCliBackend）传 cancel_event；
        旧签名后端（测试桩/自定义）兼容直调——不接取消事件的后端，取消
        只撤销提交权（DB 双检不发布），进程靠超时兜底（如实口径）。"""
        run = self.backend.run
        if cancel_event is not None:
            import inspect
            try:
                sig = inspect.signature(run)
                accepts = any(
                    p.kind == inspect.Parameter.VAR_KEYWORD
                    or p.name == "cancel_event"
                    for p in sig.parameters.values())
            except (TypeError, ValueError):
                accepts = False
            if accepts:
                return run(statement, workdir, check_cmd, title,
                           cancel_event=cancel_event)
        return run(statement, workdir, check_cmd, title)

    def _work_inner(self, mid: str, nid: str, aid: str, integration: bool,
                    cancel_event: threading.Event | None = None):
        node = self.repo.get_node(nid)
        workdir = Path(self.repo.get_attempt(aid).workdir)
        # 1B 启动前窗口：认领后、进程启动前已被取消（事件或 DB 已撤销）→
        # 不启动进程
        if cancel_event is not None and cancel_event.is_set():
            return
        if self.hard_stop.is_set():
            # Codex R5：kill 与认领交界——已认领未执行的尝试在执行起点
            # 取消（账本+事件），不放行执行
            ts = _now()
            with self.repo.tx() as cur:
                cur.execute(
                    "UPDATE attempts SET status='cancelled',"
                    " category='cancel', ended_at=? WHERE id=?"
                    " AND status='running'", (ts, aid))
                cur.execute(
                    "UPDATE nodes SET status='pending', updated_at=?"
                    " WHERE id=? AND status='in_work'", (ts, nid))
                self.repo.append_event(
                    cur, mid, "attempt_cancelled", nid, attempt=aid,
                    reason="RunManager.stop(kill)：认领与停止交界")
            return
        if self.repo.get_attempt(aid).status != "running":
            return
        try:
            self._seed_bound(aid, workdir)
        except ValueError as e:
            self._abort_seed_error(mid, nid, aid, str(e))
            return
        declared = json.loads(node.declared_artifacts_json or "[]")
        # Codex N5：播种期间可能已被取消（kill 与认领/播种的交界）——
        # 调用后端前复查；已取消则账本收尾（事件在案），不放行执行
        if (self.hard_stop.is_set()
                or (cancel_event is not None and cancel_event.is_set())
                or self.repo.get_attempt(aid).status != "running"):
            ts = _now()
            with self.repo.tx() as cur:
                cur.execute(
                    "UPDATE attempts SET status='cancelled',"
                    " category='cancel', ended_at=? WHERE id=?"
                    " AND status='running'", (ts, aid))
                cur.execute(
                    "UPDATE nodes SET status='pending', updated_at=?"
                    " WHERE id=? AND status='in_work'", (ts, nid))
                self.repo.append_event(
                    cur, mid, "attempt_cancelled", nid, attempt=aid,
                    reason="播种后复查：执行请求在启动前已被取消")
            return
        statement = node.statement
        if integration:
            statement += ("（当前目录 inputs/ 下是全部孩子已验收产物的副本，"
                          "以此为输入做整合交付。交付物文件必须严格包含验收命令中"
                          "出现的全部关键词原文，逐字一致，不要改写或概括。）")
        heartbeat(self.repo, aid)
        with _HeartbeatLoop(self.repo, aid, self.heartbeat_interval):
            run1 = self._backend_run(statement, workdir, node.check_cmd,
                                     node.title, cancel_event)
        heartbeat(self.repo, aid)
        # 阶段 1A：统一回执结算入口（归属校验+契约校验+幂等）——重复结算
        # 不重复计费；Codex 验收修正：计费回调传入锁内执行，消除
        # "回执已落库、费用未累计"的进程内崩溃窗口
        from .executor_contract import ContractError, record_execution

        def _charge(delta: float, basis: str):
            # Codex R1/N1：持久化已并入 record_execution 结算事务
            # （apply_budget_delta 读-改-写）；本回调只同步内存缓存
            if basis in ("reported", "estimated"):
                self.budget.cost_used += delta
            elif basis == "unknown":
                self.budget.cost_unknown += 1

        try:
            rec = record_execution(self.repo, aid, run1, mid=mid, nid=nid,
                                   on_charge=_charge)
        except ContractError as e:
            ts = _now()
            with self.repo.tx() as cur:
                cur.execute(
                    "UPDATE attempts SET status='error', category='error',"
                    " ended_at=? WHERE id=? AND status='running'", (ts, aid))
                cur.execute(
                    "UPDATE nodes SET status='blocked', updated_at=?"
                    " WHERE id=? AND status='in_work'", (ts, nid))
                self.repo.append_event(
                    cur, mid, "worker_error", nid, attempt=aid,
                    error=f"ContractError: {str(e)[:180]}",
                    hint="回执违反契约：资源账保持不变，人工处置后重派")
            return
        # 计费已在 record_execution 锁内经 _charge 完成（replayed 不进）；
        # 1B 退出与提交交界：提交权已撤销（事件置位或 DB 已 cancelled）→
        # 迟到结果不发布（资源证据已结算保留），节点不因迟到结果改判
        if cancel_event is not None and cancel_event.is_set():
            return
        if self.repo.get_attempt(aid).status != "running":
            return
        # A-G1 检测点：执行返回→资源结算（已完成）→检测固定提案工件。
        # F4/R4（Codex 复核）：只处理**平台结算认可**的会话——受理判据
        # = record_execution 的 contract_status=="valid"（平台规范化/
        # 校验后的结果），不再采信执行者自报的 receipt_status（自报
        # valid 而缺核心/时序字段时平台判 legacy_incomplete，不得激活
        # 提案）；ok=False（超时/非零退出/取消）一律不检测。资源证据与
        # 真实费用照常结算保留。
        # 只解析 .dag/proposal.json（第三方内容中的 PROPOSE_SPLIT 不算提案）；
        # 整合会话不受理拆分（父已有孩子——整合就是拆分的终点，再拆无意义，
        # 且"提案即释放执行位"不得打断整合 attempt——负例实证）。
        session_ok = (run1.get("ok") is True
                      and isinstance(rec, dict)
                      and rec.get("contract_status") == "valid")
        # D1：供应商错误分级（结构化 error_class）——在提交判卷之前：
        # 限流→退避取消（不耗重试）；额度/余额/认证→暂停待人工。
        # R5：并发成功不清除另一请求触发的活跃冷却——仅当冷却已自然
        # 过期时才重置连续计数（冷却期内成功不改动任何保护状态）。
        if run1.get("ok") is True:
            if time.monotonic() >= self.provider_cooldown_until:
                self._rate_streak = 0
        else:
            err_class = (run1.get("resources") or {}).get("error_class")
            if err_class:
                verdict = self._handle_provider_error(mid, nid, aid,
                                                      err_class, run1)
                if verdict == "skip_submit":
                    self.timeline.add(nid, aid, "end")
                    with self._inflight_lock:
                        self._futures.pop(aid, None)
                        self._cancel_events.pop(aid, None)
                    return
        if self.auto_split.enabled and not integration and session_ok:
            handled = self._detect_split_proposal(mid, nid, aid, node,
                                                  workdir)
            if handled == "activated":
                # 无交付的拆解请求：原执行位已释放，跳过普通工件提交，
                # 交由父整合完成原目标（生长成功才跳过提交）
                self.timeline.add(nid, aid, "end")
                with self._inflight_lock:
                    self._futures.pop(aid, None)
                return
            # "queued_keep"：有交付→提案在案，继续按原考卷判卷；
        try:
            result = self.graph.submit_attempt(mid, nid, aid,
                                               artifact_paths=list(declared))
            if self.auto_split.enabled:
                self._after_verdict(mid, nid, aid,
                                    result.get("verdict"))
        except Exception as e:
            # 补偿：提交链路异常不得留下僵尸在途
            with self.repo.tx() as cur:
                att = cur.execute("SELECT status FROM attempts WHERE id=?",
                                  (aid,)).fetchone()
                if att and att["status"] == "running":
                    cur.execute(
                        "UPDATE attempts SET status='failed', category='fail',"
                        " ended_at=? WHERE id=?", (_now(), aid))
                nd = cur.execute("SELECT status FROM nodes WHERE id=?",
                                 (nid,)).fetchone()
                if nd and nd["status"] == "in_work":
                    cur.execute(
                        "UPDATE nodes SET status='failed', updated_at=? WHERE id=?",
                        (_now(), nid))
                self.repo.append_event(cur, mid, "dispatch_error", nid,
                                       error=str(e)[:200])
        self.timeline.add(nid, aid, "end")
        with self._inflight_lock:
            self._futures.pop(aid, None)

    # ---- 拆解提案 ----

    def propose_decomposition(self, mid: str, p: Proposal) -> list[str]:
        """v3.1 生长闸门：结构裁决 → 考卷体检（事务外）→ 单事务落地。"""
        ok, why = review_proposal(p)
        if not ok:
            raise ValueError(f"提案被拒：{why}")
        from .acceptance import preflight as _preflight
        for i, c in enumerate(p.children):
            pf_ok, pf_why, _ = _preflight(check_cmd=c.get("check_cmd"))
            if not pf_ok:
                raise ValueError(f"孩子{i}({c.get('title')}) 考卷体检未过：{pf_why}")
        if p.integration_check:
            pf_ok, pf_why, _ = _preflight(check_cmd=p.integration_check)
            if not pf_ok:
                raise ValueError(f"整合考卷体检未过：{pf_why}")
        return self.graph.decompose(mid, p.node_id,
                                    children=p.children,
                                    integration_check=p.integration_check)

    # ---- A-G1：自动生长（提案检测 / 异步规划 / 权威应用） ----

    def _detect_split_proposal(self, mid: str, nid: str, aid: str,
                               node, workdir) -> str | None:
        """检测并受理拆分提案。返回 None（无提案/违规→继续正常提交）、
        "queued_keep"（有交付：提案在案，按原卷判卷后分诊）或
        "activated"（无交付：激活拆解，原执行位释放）。"""
        import json as _json
        from .autogrow import (ProposalError, parse_proposal, proposal_path)
        pf = proposal_path(workdir)
        if not pf.is_file():
            return None
        try:
            raw = pf.read_bytes()
        except OSError as e:
            self._split_event(mid, nid, "split_rejected",
                              reason=f"提案文件不可读：{e}", kind="violation")
            return None
        fp = _json.loads(self.repo.get_attempt(aid).claim_fingerprint_json
                         or "{}")
        try:
            parsed = parse_proposal(raw, fp, node.id, aid)
        except ProposalError as e:
            self._split_event(mid, nid, "split_rejected",
                              reason=str(e)[:300], kind="violation",
                              proposal_id=None, attempt=aid)
            return None
        # 同源 attempt 唯一（A0 §9）：同内容幂等返回既有，异内容冲突拒绝
        prior = self.repo.split_proposal_by_attempt(aid)
        if prior is not None:
            if prior["content_sha256"] == parsed.content_sha256:
                self._split_event(mid, nid, "split_duplicate",
                                  proposal_id=prior["id"],
                                  reason="同源同内容提案幂等返回既有结果")
            else:
                self._split_event(mid, nid, "split_rejected",
                                  proposal_id=prior["id"],
                                  reason="同源 attempt 异内容提案冲突",
                                  kind="violation")
            return None
        # 有限待审/重试（防拒绝后反复触发规划）
        queued_n = self.repo._conn.execute(
            "SELECT COUNT(*) AS c FROM split_proposals WHERE node_id=?"
            " AND status='queued'", (nid,)).fetchone()["c"]
        rejected_n = self.repo._conn.execute(
            "SELECT COUNT(*) AS c FROM split_proposals WHERE node_id=?"
            " AND status='rejected'", (nid,)).fetchone()["c"]
        if queued_n >= 2:
            self._split_event(mid, nid, "split_rejected",
                              reason=f"待审提案已达上限（{queued_n}）",
                              kind="violation", proposal_id=None)
            return None
        if rejected_n >= 5:
            self._split_event(mid, nid, "split_rejected",
                              reason=f"被拒提案已达上限（{rejected_n}）——"
                                     "停止受理（防热循环）",
                              kind="violation", proposal_id=None)
            return None
        pid = f"sp-{uuid.uuid4().hex[:12]}"
        ts = _now()
        has_delivery = any(
            (workdir / rel).is_file()
            and (workdir / rel).stat().st_size > 0
            for rel in json.loads(node.declared_artifacts_json or "[]"))
        with self.repo.tx() as cur:
            try:
                self.repo.insert_split_proposal({
                    "id": pid, "mission_id": mid, "node_id": nid,
                    "source_attempt_id": aid,
                    "raw_sha256": parsed.raw_sha256,
                    "content_sha256": parsed.content_sha256,
                    "raw_json": raw.decode("utf-8", errors="replace"),
                    "fingerprint_json": _json.dumps(
                        fp, ensure_ascii=False),
                    "status": "queued",
                    "has_delivery": 1 if has_delivery else 0,
                    "created_at": ts}, cur)
            except Exception:
                # UNIQUE 冲突（并发同源）：回滚后按幂等处理
                self._split_event(mid, nid, "split_duplicate",
                                  reason="并发同源提案（唯一约束保留首份）")
                return None
            self.repo.append_event(
                cur, mid, "split_proposed", nid, proposal=pid,
                source_attempt=aid, children=len(parsed.children),
                has_delivery=has_delivery,
                **{"summary": parsed.summary()})
        if has_delivery:
            # 有交付：照原考卷判卷（提案不豁免产物责任），
            # 判卷后 _after_verdict 分诊
            return "queued_keep"
        self._activate_split(mid, nid, aid, pid,
                             reason="无交付的拆解请求：原执行位释放，"
                                    "交独立出题与父整合")
        return "activated"

    def _activate_split(self, mid: str, nid: str, aid: str, pid: str,
                        reason: str):
        """激活拆解：attempt→split 终态（明确非 FAIL/ERROR）+ 父→
        waiting_split（持久等待态，不被 frontier 认领）。"""
        ts = _now()
        with self.repo.tx() as cur:
            cur.execute(
                "UPDATE attempts SET status='split', category='split',"
                " ended_at=? WHERE id=? AND status='running'", (ts, aid))
            cur.execute(
                "UPDATE nodes SET status='waiting_split', updated_at=?"
                " WHERE id=? AND status='in_work'", (ts, nid))
            self.repo.append_event(cur, mid, "split_activated", nid,
                                   proposal=pid, attempt=aid,
                                   reason=reason[:300])

    def _after_verdict(self, mid: str, nid: str, aid: str, verdict: str):
        """有交付提案的判卷分诊：pass→提案作废（父已过卷）；
        fail→激活拆解（产物 FAIL 事实保留）；error/rejected→提案留待审。"""
        row = self.repo.split_proposal_by_attempt(aid)
        if row is None or row["status"] != "queued":
            return
        ts = _now()
        try:
            if verdict == "pass":
                with self.repo.tx() as cur:
                    cur.execute(
                        "UPDATE split_proposals SET status='discarded',"
                        " decided_at=?, reject_reason=? WHERE id=?"
                        " AND status='queued'",
                        (ts, "父原考卷通过：无需拆分", row["id"]))
                    self.repo.append_event(cur, mid, "split_discarded", nid,
                                           proposal=row["id"],
                                           reason="parent_passed")
            elif verdict == "fail":
                with self.repo.tx() as cur:
                    cur.execute(
                        "UPDATE nodes SET status='waiting_split',"
                        " updated_at=? WHERE id=? AND status='failed'",
                        (ts, nid))
                    self.repo.append_event(cur, mid, "split_activated", nid,
                                           proposal=row["id"],
                                           reason="deliverable_failed_"
                                                  "original_exam")
            # error/rejected：提案保持 queued（证据保留，人工处置）
        except Exception:
            pass  # 分诊失败不掩盖原判卷结果；提案仍可人工处置

    def _split_event(self, mid: str, nid: str, etype: str,
                     reason: str, kind: str = "", proposal_id=None,
                     **extra):
        try:
            with self.repo.tx() as cur:
                self.repo.append_event(cur, mid, etype, nid,
                                       proposal=proposal_id, kind=kind,
                                       reason=reason[:300], **extra)
        except Exception:
            pass

    def _reject_proposal(self, mid: str, pid: str, nid: str, reason: str):
        """提案拒绝：split_rejected（提案违规类，≠产物 FAIL）+ 父回
        pending（可重派或人工处置；无半图残留）。"""
        ts = _now()
        with self.repo.tx() as cur:
            cur.execute(
                "UPDATE split_proposals SET status='rejected',"
                " reject_reason=?, decided_at=? WHERE id=?"
                " AND status='queued'", (reason[:500], ts, pid))
            cur.execute(
                "UPDATE nodes SET status='pending', updated_at=?"
                " WHERE id=? AND status='waiting_split'", (ts, nid))
            self.repo.append_event(cur, mid, "split_rejected", nid,
                                   proposal=pid, kind="proposal",
                                   reason=reason[:300])

    def _dispatch_planning(self, mid: str) -> int:
        """派发规划任务（与执行共享执行位与停止语义——规划也受统一
        并发/取消/记账管理；不在执行 worker 里同步等待规划会话）。"""
        if not self.auto_split.enabled or self._pool is None:
            return 0
        dispatched = 0
        for prow in self.repo.queued_split_proposals(mid):
            key = f"plan:{prow['id']}"
            with self._inflight_lock:
                if len(self._futures) >= self.max_concurrency:
                    break
                if key in self._futures:
                    continue
            fut = self._pool.submit(self._plan_work, mid, prow["id"])
            with self._inflight_lock:
                self._futures[key] = fut
            self.timeline.add(prow["node_id"], key, "start")
            dispatched += 1
        return dispatched

    def _plan_work(self, mid: str, pid: str):
        """规划 worker：出题分离（模板/独立出题）→ 结构闸门与体检
        （事务外）→ graph.apply_auto_split 权威事务。失败分类：
        提案/出题违规→rejected+父回 pending；服务异常→留 queued
        （plan_attempts 有界，下轮重试）。"""
        from . import autogrow
        key = f"plan:{pid}"
        row = self.repo.get_split_proposal(pid)
        try:
            if row is None or row["status"] != "queued":
                return
            if self.hard_stop.is_set() or self.stop_requested.is_set():
                return  # 提案留 queued：恢复后重新规划（不占位等待）
            node = self.repo.get_node(row["node_id"])
            if node.status != "waiting_split":
                if row["has_delivery"]:
                    # 有交付：提案在案、判卷分诊未完（pass→作废/
                    # fail→激活）——留队等待，不拒绝不规划
                    return
                # 迟到/失效提案：父已被人工处置（修订/维修/取消/恢复
                # 执行）且无交付可判——旧提案明确拒绝（不静默挂起、
                # 不误伤新状态）
                self._reject_proposal(mid, pid, row["node_id"],
                                      f"父节点已非 waiting_split"
                                      f"（现 {node.status}）：旧提案拒绝")
                return
            plan_n = int(row["plan_attempts"] or 0) + 1
            if plan_n > autogrow.MAX_PLAN_ATTEMPTS:
                self._reject_proposal(mid, pid, row["node_id"],
                                      "规划重试耗尽（有限重试护栏）")
                return
            self.repo.update_split_proposal(pid, plan_attempts=plan_n)
            fp = json.loads(row["fingerprint_json"] or "{}")
            doc = json.loads(row["raw_json"] or "{}")
            # ---- 任务书 2026-11-06 批结构校验钩子：可信模板可定义
            # validate_batch(children, parent_title) 约束整批孩子集合
            # （逐孩子 generate 看不到兄弟，批约束只能在此处执行）；
            # 无该方法的模板保持原语义。违规=TemplateViolation→整案拒绝。
            _children = doc.get("children") or []
            if _children:
                _tpl0 = autogrow.SPLIT_TEMPLATES.get(
                    (_children[0] or {}).get("template_id") or "")
                if _tpl0 is not None and hasattr(_tpl0, "validate_batch"):
                    _tpl0.validate_batch(_children, node.title)
            # ---- 出题（模板路径优先，独立出题兜底；check_hint 仅建议）----
            specs = []
            for ch in doc.get("children", []):
                tpl_id = ch.get("template_id")
                if tpl_id:
                    tpl = autogrow.SPLIT_TEMPLATES.get(tpl_id)
                    if tpl is None:
                        raise autogrow.TemplateViolation(
                            f"模板越权：未授权 template_id={tpl_id}"
                            "（执行者不能自行批准子考卷）")
                    spec = tpl.generate(ch, node.title)
                    arts = spec.get("declared_artifacts") or [""]
                    if not autogrow.safe_artifact_name(arts[0]):
                        raise autogrow.TemplateViolation(
                            f"模板交付名不合法：{arts[0]}")
                    specs.append(spec)
                else:
                    out = self.planner.plan({
                        "parent_title": node.title, "children": [ch],
                        "parent_node_id": row["node_id"],
                        "mission_id": mid})
                    specs.extend(out["children"])
            # ---- 结构闸门 + 体检（事务外）----
            # R1（Codex 复核）：冻结必须先于体检——体检前后各算一次摘要，
            # 两次一致才认可冻结内容（体检验证的就是将被冻结的字节，
            # "体检过→冻结前"窗口关闭）；入口识别用 business_entry_paths
            # （不按扩展名猜：.pyw/无扩展名/目录都算入口），命令引用的
            # 全部本地文件入口必须显式列入 exam_files。
            from .acceptance import preflight as _preflight, \
                compute_check_digest as _ccd, \
                business_entry_paths as _bep, \
                verify_exam_files_cover as _vef, \
                AcceptanceError as _AccErr
            for i, spec in enumerate(specs):
                if not (spec.get("statement") or "").strip():
                    raise autogrow.ProposalError(
                        f"出题产物 {i} 缺陈述（三件套不齐）")
                if not spec.get("check_cmd") and not spec.get("acceptance"):
                    raise autogrow.ProposalError(
                        f"出题产物 {i} 缺考卷（fail-closed 会堵死整合）")
                exam_files_i = tuple(spec.get("exam_files") or ())
                try:
                    entries = _bep(check_cmd=spec.get("check_cmd"),
                                   acceptance=spec.get("acceptance"))
                except _AccErr as e:
                    raise autogrow.ProposalError(
                        f"出题产物 {i} 考卷入口解析失败：{e}")
                if entries and not exam_files_i:
                    raise autogrow.ProposalError(
                        f"出题产物 {i} 考卷引用本地文件入口 "
                        f"{[str(e) for e in entries[:3]]} 但未声明"
                        " exam_files（考卷包必须显式冻结）")
                for rel in exam_files_i:
                    if not Path(rel).is_file():
                        raise autogrow.ProposalError(
                            f"出题产物 {i} 考卷包清单文件缺失：{rel}")
                try:
                    ok_cov, missing = _vef(spec.get("check_cmd"),
                                           spec.get("acceptance"),
                                           exam_files_i)
                except _AccErr as e:
                    raise autogrow.ProposalError(
                        f"出题产物 {i} 考卷覆盖校验失败：{e}")
                if not ok_cov:
                    raise autogrow.ProposalError(
                        f"出题产物 {i} 考卷包清单未覆盖实际执行入口："
                        f"{missing}")
                # 冻结①（体检前）
                try:
                    frozen = _ccd(check_cmd=spec.get("check_cmd"),
                                  acceptance=spec.get("acceptance"),
                                  exam_files=exam_files_i)
                except _AccErr as e:
                    raise autogrow.ProposalError(
                        f"出题产物 {i} 考卷摘要计算失败：{e}")
                pf_ok, pf_why, _ = _preflight(
                    check_cmd=spec.get("check_cmd"),
                    acceptance=spec.get("acceptance"))
                if not pf_ok:
                    raise autogrow.ProposalError(
                        f"出题产物 {i} 考卷体检未过：{pf_why}")
                # 冻结②（体检后）：与①不一致=体检期间内容漂移——体检
                # 验证的不再是将冻结的内容，拒绝（反例：体检过 STRICT 后
                # 改永绿再冻结=废卷绕过"有牙齿"关）
                try:
                    frozen2 = _ccd(check_cmd=spec.get("check_cmd"),
                                   acceptance=spec.get("acceptance"),
                                   exam_files=exam_files_i)
                except _AccErr as e:
                    raise autogrow.ProposalError(
                        f"出题产物 {i} 考卷摘要计算失败：{e}")
                if frozen2 != frozen:
                    raise autogrow.ProposalError(
                        f"出题产物 {i} 考卷在体检期间漂移（前后摘要"
                        "不一致）：拒绝——体检未验证冻结内容")
                spec["_frozen_digest"] = frozen
            # ---- 出题来源与结果留痕（可追溯）----
            self.repo.update_split_proposal(
                pid, plan_backend=self.planner.name,
                plan_resources_json=json.dumps(
                    {"calls": 0, "cost": {"amount": 0.0,
                                          "basis": "reported"},
                     "note": "零模型桩如实记账（真实出题会话按实际结算）"},
                    ensure_ascii=False),
                plan_output_json=json.dumps(specs, ensure_ascii=False))
            # ---- 权威事务应用（原子；失败无半图/半计数）----
            # R2：应用权 gate 读库内 run_control 旗标（在 apply 事务写锁
            # 内）——request_stop 经 repo.tx 写同一旗标，两侧共用写锁
            # 构成线性化点："gate 读真→kill→仍落图"不再可能（kill 的旗标
            # 写必须等 apply 提交或先行提交）。Event 保留为直用 Scheduler
            # 路径的收紧判据（只会更多拒绝，不构成放行依据）。
            src_att = self.repo.get_attempt(row["source_attempt_id"])
            self.graph.apply_auto_split(
                mid, row["node_id"], row["source_attempt_id"], pid, specs,
                fp,
                max_splits_per_mission=self.auto_split.max_splits_per_mission,
                max_depth=self.auto_split.max_depth,
                node_cap=self.node_cap,
                apply_gate=lambda: not (self._stop_revoked_db()
                                        or self.hard_stop.is_set()
                                        or self.stop_requested.is_set()),
                source_binding_json=src_att.input_binding_json)
        except (autogrow.ProposalError, GraphError) as e:
            self._reject_proposal(mid, pid,
                                  row["node_id"] if row is not None else "?",
                                  str(e))
        except Exception as e:
            # 服务类异常（非提案责任）：不立即放弃也不无限重试——
            # 事件留痕，plan_attempts 已递增，超限由下轮拒绝
            self._split_event(mid, row["node_id"] if row is not None
                              else None, "split_plan_error",
                              reason=f"{type(e).__name__}: {str(e)[:200]}",
                              proposal_id=pid)
        finally:
            if row is not None:
                self.timeline.add(row["node_id"], key, "end")
            with self._inflight_lock:
                self._futures.pop(key, None)

    def enqueue(self, mid: str, **node_kwargs) -> str:
        """动态入队（执行中新增节点），进同一调度入口。"""
        return self.graph.add_node(mid, **node_kwargs)

    # ---- 主循环 ----

    def run_until_done(self, mid: str, root_id: str, timeout: float = 1800,
                       poll: float = 0.2, max_idle: int = 100,
                       on_tick=None) -> dict:
        from .recovery import reap_orphans
        self._pool = ThreadPoolExecutor(max_workers=self.max_concurrency)
        t0 = time.monotonic()
        idle = 0
        try:
            while time.monotonic() - t0 < timeout:
                if self.stop_requested.is_set():
                    self.halt_reason = "服务停止（drain：在途有界结束）"
                    break
                if self.repo.get_mission(mid).status == "done":
                    break
                dispatched = self.tick_dispatch(mid)
                if self.reap_enabled:
                    try:
                        with self._inflight_lock:
                            inflight = set(self._futures.keys())
                        reap_orphans(self.repo, self.graph, mid,
                                     stale_seconds=int(self.reap_after),
                                     protected=inflight)
                    except Exception:
                        pass
                if on_tick is not None:
                    try:
                        on_tick()
                    except Exception:
                        pass
                if not self._futures:
                    nodes = self.repo.list_nodes(mid)
                    failed = [n.id for n in nodes if n.status == "failed"
                              and not self.retry_policy.allows(self.repo, n.id)]
                    blocked = [n.id for n in nodes if n.status == "blocked"]
                    if failed or blocked:
                        self.halt_reason = f"死局：failed={failed} blocked={blocked}"
                        break
                    if self.halt_reason:  # 预算熔断等
                        break
                    if dispatched == 0:
                        # D1：限流退避窗口——等待而不是计入"无进展"停机
                        if time.monotonic() < self.provider_cooldown_until:
                            time.sleep(min(poll, 0.5))
                            continue
                        idle += 1
                        if idle >= max_idle:
                            self.halt_reason = "无进展（无就绪/无在途/无失败）"
                            break
                    else:
                        idle = 0
                else:
                    idle = 0
                time.sleep(poll)
            else:
                self.halt_reason = f"超时（{timeout}s）"
        finally:
            self._pool.shutdown(wait=True)
        root = self.repo.get_node(root_id)
        return {
            "mission_status": self.repo.get_mission(mid).status,
            "root_status": root.status,
            "halt_reason": self.halt_reason,
            "overlap_pairs": self.timeline.overlap_pairs(),
            "budget": {"calls": self.budget.calls_used,
                       "cost_usd": round(self.budget.cost_used, 4)},
            "timeline": [
                {"node": nid, "attempt": aid, "phase": ph, "t": round(t, 3)}
                for nid, aid, ph, t in self.timeline.events],
        }
