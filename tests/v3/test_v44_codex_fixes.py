# -*- coding: utf-8 -*-
"""v3.13 回归：Codex 独立验收三缺口的修复锁定。

① RunManager.stop(kill) 接通进程级取消（cancel_attempt：事件+停进程+节点回
   pending）——不再裸 UPDATE 账本让子进程跑到超时；
② record_execution 原子化：并发重放只计一次费；incomplete 可补齐升级
   （upgraded，补记费用），valid 已结算才 replayed；
③ slow_deliver 垫片修复后 drain 测试补回执 valid 断言（见 test_v42 补充）。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from dag_agent.cc_backend import ClaudeCliBackend
from dag_agent.daemon import RunManager
from dag_agent.executor_contract import record_execution
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


# ---- ① kill 入口 ----

def test_runmanager_stop_kill_cancels_process_and_logs_event(tmp_path):
    """stop(kill)：在途 CLI 进程被取消（事件在案、回执 cancelled、
    不等到 600s 超时）。"""
    os.environ["FAKE_MODE"] = "sleep"
    try:
        mdir = tmp_path / "mk"
        mdir.mkdir()
        repo = Repository(mdir / "mission.db")
        g = Graph(repo)
        mid, root = g.create_mission("kill入口", brief=ROOT_BRIEF,
                                     root_check=f'python "{CHECKER}" hello.txt DAG-OK',
                                     root_declared_artifacts=("hello.txt",))
        nid = g.add_node(mid, "L", BRIEF, parent_id=root,
                         check_cmd=f'python "{CHECKER}" hello.txt DAG-OK',
                         declared_artifacts=("hello.txt",))
        repo.close()
        envf = tmp_path / "env.local"
        envf.write_text("ANTHROPIC_BASE_URL=https://fake\n"
                        "ANTHROPIC_AUTH_TOKEN=d\n", encoding="utf-8")
        rm = RunManager(home=tmp_path)
        r = rm.start(mid, max_concurrency=1, real=True, timeout_s=600,
                     backend_factory=lambda: ClaudeCliBackend(
                         cli_path=SHIM, env_file=envf, timeout=600))
        assert r.get("ok"), r
        t0 = time.monotonic()
        aid = None
        while time.monotonic() - t0 < 30:
            repo = Repository(mdir / "mission.db")
            row = repo._conn.execute(
                "SELECT id FROM attempts WHERE status='running'").fetchone()
            repo.close()
            if row:
                aid = row["id"]
                break
            time.sleep(0.2)
        assert aid, "在途未出现"
        time.sleep(1.0)  # 进程真正起来
        t_kill = time.monotonic()
        st = rm.stop(mid, mode="kill")
        dt = time.monotonic() - t_kill
        assert st.get("ok") and st.get("cancelled_attempts") == [aid]
        assert dt < 20, f"kill 后 {dt:.0f}s 才停（进程未被通知取消）"
        repo = Repository(mdir / "mission.db")
        att = repo.get_attempt(aid)
        assert att.status == "cancelled"
        evs = [e.type for e in repo.events_of(mid)]
        assert "attempt_cancelled" in evs
        assert repo.get_node(nid).status == "pending"
        repo.close()
    finally:
        os.environ.pop("FAKE_MODE", None)
        RunManager.get()._runs.pop(mid, None)


# ---- ② 结算原子性与补齐 ----

def test_concurrent_settlement_charges_once(tmp_path):
    """并发结算同一 attempt：只有一次可计费增量（加锁原子化）。"""
    from dag_agent.executor_contract import ContractError
    repo = Repository(tmp_path / "m" / "mission.db")
    try:
        g = Graph(repo)
        mid, root = g.create_mission("并发结算")
        nid = g.add_node(mid, "L", BRIEF, parent_id=root)
        aid = g.start_attempt(mid, nid, backend="stub")
        charges = []
        lock = threading.Lock()

        def charge(delta, basis):
            with lock:
                charges.append((delta, basis))

        barrier = threading.Barrier(4)
        results = []

        def worker():
            barrier.wait()
            r = record_execution(repo, aid, {
                "resources": {"origin": "claude-cli", "agent_id": "a1",
                              "tokens": 5, "duration_ms": 1,
                              "cost_currency": "USD",
                              "executor_started_at":
                                  "2026-10-08T10:00:00+00:00",
                              "executor_ended_at":
                                  "2026-10-08T10:00:01+00:00",
                              "receipt_status": "valid",
                              "cost": {"amount": 0.5,
                                       "basis": "reported"}}},
                mid=mid, nid=nid, on_charge=charge)
            results.append(r)

        ts_ = [threading.Thread(target=worker) for _ in range(4)]
        for t in ts_:
            t.start()
        for t in ts_:
            t.join(timeout=30)
        assert len(results) == 4
        assert sum(1 for r in results if r.get("replayed")) == 3
        assert charges == [(0.5, "reported")]  # 只计一次
    finally:
        repo.close()


def test_incomplete_receipt_can_be_upgraded(tmp_path):
    """incomplete 首结不 final → 完整回执补齐升级（upgraded + 补记费用）；"""
    repo = Repository(tmp_path / "m" / "mission.db")
    try:
        g = Graph(repo)
        mid, root = g.create_mission("补齐")
        nid = g.add_node(mid, "L", BRIEF, parent_id=root)
        aid = g.start_attempt(mid, nid, backend="stub")
        charges = []
        r1 = record_execution(repo, aid, {"resources": {
            "origin": "claude-cli", "backend": "claude-cli",
            "receipt_status": "incomplete",
            "executor_started_at": "2026-10-07T10:00:00+00:00",
            "executor_ended_at": "2026-10-07T10:01:00+00:00",
            "duration_ms": 1000,
            "cost": {"amount": None, "basis": "unknown"}}},
            mid=mid, nid=nid, on_charge=lambda d, b: charges.append((d, b)))
        assert r1["ok"] and not r1["replayed"] and r1["upgraded"] is False
        rj = json.loads(repo.get_attempt(aid).resources_json)
        assert not rj.get("settled_valid")  # 未终局——可补齐
        # 完整回执补齐
        r2 = record_execution(repo, aid, {"resources": {
            "origin": "claude-cli", "agent_id": "sess-9",
            "tokens": 700, "receipt_status": "valid",
            "cost_currency": "USD",
            "cost": {"amount": 0.8, "basis": "reported"}}},
            mid=mid, nid=nid, on_charge=lambda d, b: charges.append((d, b)))
        assert r2["upgraded"] is True and r2["contract_status"] == "valid"
        assert charges == [(0.0, "unknown"), (0.8, "reported")]
        # 首结 unknown 计数 + 补齐补记真实费用
        rj = json.loads(repo.get_attempt(aid).resources_json)
        assert rj["settled_valid"] is True and rj["tokens"] == 700
        # 已终局再来同额 → replayed 不再计费；金额不同=修正总额（R2 规则）
        r3 = record_execution(repo, aid, {"resources": {
            "origin": "claude-cli", "tokens": 9,
            "cost": {"amount": 0.8, "basis": "reported"}}},
            mid=mid, nid=nid, on_charge=lambda d, b: charges.append((d, b)))
        assert r3["replayed"] is True
        assert charges == [(0.0, "unknown"), (0.8, "reported")]  # 不多计
    finally:
        repo.close()


# ---- Codex 二轮复核（2026-10-08）关闭条件回归 ----

def _mk_ok_receipt(amount=0.5, basis="reported", extra=None):
    r = {"origin": "claude-cli", "agent_id": "a1", "tokens": 5,
         "duration_ms": 1, "cost_currency": "USD",
         "executor_started_at": "2026-10-08T10:00:00+00:00",
         "executor_ended_at": "2026-10-08T10:00:01+00:00",
         "receipt_status": "valid",
         "cost": {"amount": amount, "basis": basis}}
    if extra:
        r.update(extra)
    return {"resources": r}


def test_r1_crash_between_receipt_and_charge_no_loss(tmp_path):
    """R1：费用回调处崩溃注入 → 重开库累计正确（回执/流水/累计同事务），
    重放 delta=0 不重复补账。"""
    repo = Repository(tmp_path / "m" / "mission.db")
    g = Graph(repo)
    mid, root = g.create_mission("R1崩溃注入")
    nid = g.add_node(mid, "L", BRIEF, parent_id=root)
    aid = g.start_attempt(mid, nid, backend="stub")

    def boom(delta, basis):
        raise RuntimeError("注入：持久边界后回调崩溃")

    with pytest.raises(RuntimeError):
        record_execution(repo, aid, _mk_ok_receipt(2.0),
                         mid=mid, nid=nid, on_charge=boom)
    db_path = repo.db_path
    repo.close()
    repo2 = Repository(db_path)
    try:
        bj = json.loads(repo2.get_mission(mid).budget_json)
        assert abs(bj.get("cost_used", 0) - 2.0) < 1e-9, \
            "崩溃后累计费用丢失（回执/费用未同事务）"
        rj = json.loads(repo2.get_attempt(aid).resources_json)
        assert rj.get("cost_charged", {}).get("amount") == 2.0
        r = record_execution(repo2, aid, _mk_ok_receipt(2.0),
                             mid=mid, nid=nid)
        assert r["cost_delta"] == 0.0
        bj2 = json.loads(repo2.get_mission(mid).budget_json)
        assert abs(bj2["cost_used"] - 2.0) < 1e-9  # 不重复补账
    finally:
        repo2.close()


def test_r2_upgrade_delta_rules(tmp_path):
    """R2 差额规则：incomplete+known 2 → 补同额 2（增量 0，合计 2）；
    修正总额 5（增量 3）；已终局重放（delta 0）。修复前曾累计 4。"""
    repo = Repository(tmp_path / "m" / "mission.db")
    g = Graph(repo)
    mid, root = g.create_mission("R2差额")
    nid = g.add_node(mid, "L", BRIEF, parent_id=root)
    aid = g.start_attempt(mid, nid, backend="stub")
    charges = []

    def charge(delta, basis):
        charges.append((delta, basis))

    part = _mk_ok_receipt(2.0)
    part["resources"]["receipt_status"] = "incomplete"
    del part["resources"]["executor_ended_at"]
    r1 = record_execution(repo, aid, part, mid=mid, nid=nid,
                          on_charge=charge)
    assert r1["cost_delta"] == 2.0  # 首结 incomplete+known 照实入账
    full = _mk_ok_receipt(2.0)
    r2 = record_execution(repo, aid, full, mid=mid, nid=nid,
                          on_charge=charge)
    assert r2["upgraded"] is True and r2["cost_delta"] == 0.0, \
        "同额补齐不得重复计费（Codex 实测曾累计 4）"
    fixed = _mk_ok_receipt(5.0)
    r3 = record_execution(repo, aid, fixed, mid=mid, nid=nid,
                          on_charge=charge)
    assert r3["cost_delta"] == 3.0  # 修正总额补差
    again = record_execution(repo, aid, fixed, mid=mid, nid=nid,
                             on_charge=charge)
    assert again["replayed"] is True  # 已终局重放
    bj = json.loads(repo.get_mission(mid).budget_json)
    assert abs(bj["cost_used"] - 5.0) < 1e-9  # 合计=修正总额，非累加
    repo.close()


def test_r3_selfreport_valid_missing_fields_not_final(tmp_path):
    """R3 三步：自报 valid 缺字段 → legacy_incomplete、settled_valid 不打；
    完整补证升级；重复完整回执 replayed 返回真实状态。错误归属的重放
    在幂等之前拒绝。"""
    repo = Repository(tmp_path / "m" / "mission.db")
    g = Graph(repo)
    mid, root = g.create_mission("R3")
    nid = g.add_node(mid, "L", BRIEF, parent_id=root)
    aid = g.start_attempt(mid, nid, backend="stub")
    other = Repository(tmp_path / "o" / "mission.db")
    try:
        g2 = Graph(other)
        mid2, root2 = g2.create_mission("别的任务")
        g2.add_node(mid2, "L2", BRIEF, parent_id=root2)
        bad = {"resources": {"origin": "claude-cli", "tokens": 5,
                             "receipt_status": "valid",
                             "cost": {"amount": 1.0, "basis": "reported"}}}
        r1 = record_execution(repo, aid, bad, mid=mid, nid=nid)
        assert r1["contract_status"] == "legacy_incomplete"
        rj = json.loads(repo.get_attempt(aid).resources_json)
        assert not rj.get("settled_valid"), \
            "自报 valid 不得提前封死（缺字段时）"
        with pytest.raises(Exception):
            record_execution(repo, aid, bad, mid=mid2, nid=nid)  # 错归属
        full = _mk_ok_receipt(1.0)
        r2 = record_execution(repo, aid, full, mid=mid, nid=nid)
        assert r2["upgraded"] is True and r2["contract_status"] == "valid"
        r3 = record_execution(repo, aid, full, mid=mid, nid=nid)
        assert r3["replayed"] is True
        assert r3["contract_status"] == "valid"  # 库内真实状态
        with pytest.raises(Exception):
            record_execution(repo, aid, full, mid=mid2, nid=nid)  # 终局后
    finally:
        other.close()
        repo.close()


def test_r4_flat_cost_usd_budget_cap_still_works(tmp_path):
    """R4：旧平铺 cost_usd 后端在显式限额下正常入账并熔断
    （修复前 cost_used=0 → 限额被打穿派完全图）。"""
    os.environ["FAKE_MODE"] = "deliver"
    try:
        envf = tmp_path / "env.local"
        envf.write_text("ANTHROPIC_BASE_URL=https://fake\n"
                        "ANTHROPIC_AUTH_TOKEN=d\n", encoding="utf-8")

        class OldBackend:
            """旧签名+旧平铺费用口径的后端（承诺兼容的对象）。"""

            name = "stub"

            def run(self, statement, workdir, check_cmd, title=""):
                (Path(workdir) / "hello.txt").write_text(
                    "DAG-OK", encoding="utf-8")
                return {"ok": True, "summary": "old", "session_id": "o1",
                        "resources": {"cost_usd": 0.01}}

        repo = Repository(tmp_path / "m" / "mission.db")
        g = Graph(repo)
        mid, root = g.create_mission(
            "R4限额", brief=ROOT_BRIEF,
            root_check=f'python "{CHECKER}" hello.txt DAG-OK',
            root_declared_artifacts=("hello.txt",))
        for i in range(3):
            g.add_node(mid, f"L{i}", BRIEF, parent_id=root,
                       check_cmd=f'python "{CHECKER}" hello.txt DAG-OK',
                       declared_artifacts=("hello.txt",))
        from dag_agent.scheduler import Budget
        s = Scheduler(repo, g, OldBackend(), max_concurrency=1,
                      reap_enabled=False, budget=Budget(max_cost_usd=0.005))
        summary = s.run_until_done(mid, root, timeout=60, poll=0.05)
        bj = json.loads(repo.get_mission(mid).budget_json)
        assert abs(bj.get("cost_used", 0) - 0.01) < 1e-9, \
            f"平铺费用未入账：{bj}"
        assert "熔断" in (summary["halt_reason"] or ""), \
            f"限额未生效（修复前打穿）：{summary['halt_reason']}"
        repo.close()
    finally:
        os.environ.pop("FAKE_MODE", None)


def test_r5_kill_covers_claim_window(tmp_path, monkeypatch):
    """R5 探针（Codex 同款）：挡住 start_attempt 返回制造"快照后认领"
    窗口 → 公开 kill 仍须清掉该 attempt（退出复查+事件在案）。"""
    os.environ["FAKE_MODE"] = "deliver"
    try:
        mdir = tmp_path / "mk"
        mdir.mkdir()
        repo = Repository(mdir / "mission.db")
        g = Graph(repo)
        mid, root = g.create_mission(
            "R5交界", brief=ROOT_BRIEF,
            root_check=f'python "{CHECKER}" hello.txt DAG-OK',
            root_declared_artifacts=("hello.txt",))
        g.add_node(mid, "L", BRIEF, parent_id=root,
                   check_cmd=f'python "{CHECKER}" hello.txt DAG-OK',
                   declared_artifacts=("hello.txt",))
        repo.close()
        envf = tmp_path / "env.local"
        envf.write_text("ANTHROPIC_BASE_URL=https://fake\n"
                        "ANTHROPIC_AUTH_TOKEN=d\n", encoding="utf-8")
        gate = threading.Event()
        claimed = threading.Event()
        orig_start = Graph.start_attempt

        def gated_start(self, m, n, backend="stub"):
            claimed.set()
            gate.wait(timeout=30)
            return orig_start(self, m, n, backend=backend)

        monkeypatch.setattr(Graph, "start_attempt", gated_start)
        rm = RunManager(home=tmp_path)
        r = rm.start(mid, max_concurrency=1, real=True, timeout_s=600,
                     backend_factory=lambda: ClaudeCliBackend(
                         cli_path=SHIM, env_file=envf, timeout=600))
        assert r.get("ok"), r
        assert claimed.wait(timeout=30), "认领未发生"
        # start_attempt 被挡在认领返回前——kill 快照看不到它
        st = rm.stop(mid, mode="kill")
        gate.set()  # 放行认领（快照之后=修复前的漏派窗口）
        time.sleep(3)
        repo = Repository(mdir / "mission.db")
        try:
            running_left = [r2["id"] for r2 in repo._conn.execute(
                "SELECT id,status FROM attempts").fetchall()
                if r2["status"] == "running"]
            assert not running_left, \
                f"kill 后仍有 running 残留：{running_left}"
            evs = [e.type for e in repo.events_of(mid)]
            # 关闭条件=取消事件在案+无 running 残留；cancelled_attempts
            # 清单只含快照轮/复查轮命中的（执行起点取消的不入清单，如实）
            assert evs.count("attempt_cancelled") >= 1, \
                "交界认领的 attempt 未被取消"
        finally:
            repo.close()
    finally:
        os.environ.pop("FAKE_MODE", None)
        rm._runs.pop(mid, None)


# ---- Codex 三轮复核（2026-10-08）N1-N5 关闭条件回归 ----

def test_n1_dispatch_reserve_cannot_overwrite_submitted_cost(tmp_path):
    """N1：结算提交 $2 后，派发预留（统一接口）不得把累计覆盖回 0；
    budget_json 与 cost_charged 流水一致。"""
    repo = Repository(tmp_path / "m" / "mission.db")
    g = Graph(repo)
    mid, root = g.create_mission("N1覆盖")
    nid = g.add_node(mid, "L", BRIEF, parent_id=root)
    aid = g.start_attempt(mid, nid, backend="stub")
    # 结算已提交 $2；此刻同库做一次派发预留（统一接口增量语义）
    from dag_agent.executor_contract import apply_budget_delta
    try:
        record_execution(repo, aid, _mk_ok_receipt(2.0), mid=mid, nid=nid)
        with repo.tx() as cur:
            apply_budget_delta(cur, mid, calls_delta=1)
        bj = json.loads(repo.get_mission(mid).budget_json)
        assert abs(bj.get("cost_used", 0) - 2.0) < 1e-9, \
            f"派发路径覆盖了已提交费用：{bj}"
        assert bj.get("calls_used") == 1
        rj = json.loads(repo.get_attempt(aid).resources_json)
        assert abs(rj["cost_charged"]["amount"] - 2.0) < 1e-9  # 流水一致
    finally:
        repo.close()


def test_n2_legacy_final_receipt_migrated_not_recharged(tmp_path):
    """N2 历史迁移：旧协议 execution_final（无 cost_charged/settled_valid）
    的回执重放——信任历史累计，补流水零增量（修复前双计 2→4）。"""
    repo = Repository(tmp_path / "m" / "mission.db")
    g = Graph(repo)
    mid, root = g.create_mission("N2历史")
    nid = g.add_node(mid, "L", BRIEF, parent_id=root)
    aid = g.start_attempt(mid, nid, backend="stub")
    # 造旧格式样本：历史已计 $2（budget_json 已含），回执只有旧标记
    with repo.tx() as cur:
        cur.execute(
            "UPDATE attempts SET resources_json=? WHERE id=?",
            (json.dumps({"execution_final": True, "origin": "claude-cli",
                         "tokens": 5, "receipt_status": "valid",
                         "cost": {"amount": 2.0, "basis": "reported"}}),
             aid))
        cur.execute("UPDATE missions SET budget_json=? WHERE id=?",
                    (json.dumps({"calls_used": 1, "cost_used": 2.0}), mid))
    r = record_execution(repo, aid, {"resources": {
        "origin": "claude-cli", "tokens": 5, "receipt_status": "valid",
        "cost": {"amount": 2.0, "basis": "reported"}}}, mid=mid, nid=nid)
    assert r["contract_status"] == "legacy_migrated" and \
        r["cost_delta"] == 0.0
    bj = json.loads(repo.get_mission(mid).budget_json)
    assert abs(bj["cost_used"] - 2.0) < 1e-9, "历史已计回执被重新收费"
    rj = json.loads(repo.get_attempt(aid).resources_json)
    assert rj["cost_charged"]["amount"] == 2.0  # 迁移流水=回执金额
    repo.close()


def test_n3_duplicate_unknown_counted_once(tmp_path):
    """N3：同一 unknown 回执处理两次 → unknown 计数只 +1（重复补丁不计数）。"""
    repo = Repository(tmp_path / "m" / "mission.db")
    g = Graph(repo)
    mid, root = g.create_mission("N3unknown")
    nid = g.add_node(mid, "L", BRIEF, parent_id=root)
    aid = g.start_attempt(mid, nid, backend="stub")
    unknown_receipt = {"resources": {
        "origin": "claude-cli", "backend": "claude-cli",
        "receipt_status": "incomplete", "duration_ms": 100,
        "executor_started_at": "2026-10-08T11:00:00+00:00",
        "executor_ended_at": "2026-10-08T11:00:01+00:00",
        "cost": {"amount": None, "basis": "unknown"}}}
    record_execution(repo, aid, unknown_receipt, mid=mid, nid=nid)
    record_execution(repo, aid, unknown_receipt, mid=mid, nid=nid)
    bj = json.loads(repo.get_mission(mid).budget_json)
    assert bj.get("cost_unknown", 0) == 1, \
        f"重复 unknown 补丁重复计数：{bj}"
    repo.close()


def test_n4_flat_correction_after_final(tmp_path):
    """N4：已终局回执用平铺 cost_usd 修正总额 2→5 → 走差额补 3
    （修复前被幂等误判 replayed 忽略）。"""
    repo = Repository(tmp_path / "m" / "mission.db")
    g = Graph(repo)
    mid, root = g.create_mission("N4平铺更正")
    nid = g.add_node(mid, "L", BRIEF, parent_id=root)
    aid = g.start_attempt(mid, nid, backend="stub")
    try:
        record_execution(repo, aid, _mk_ok_receipt(2.0), mid=mid, nid=nid)
        flat_fix = {"resources": {"origin": "claude-cli", "cost_usd": 5.0,
                                  "receipt_status": "valid",
                                  "cost_currency": "USD",
                                  "executor_started_at":
                                      "2026-10-08T12:00:00+00:00",
                                  "executor_ended_at":
                                      "2026-10-08T12:00:01+00:00"}}
        r = record_execution(repo, aid, flat_fix, mid=mid, nid=nid)
        assert r["cost_delta"] == 3.0, f"平铺更正被忽略：{r}"
        bj = json.loads(repo.get_mission(mid).budget_json)
        assert abs(bj["cost_used"] - 5.0) < 1e-9
    finally:
        repo.close()


def test_n5_pre_cancel_and_seed_window(tmp_path):
    """N5：①CLI 启动入口拒绝预取消（无子进程、无产物、pre_cancel 标记）；
    ②播种后复查——hard_stop 期间认领的执行在播种后被取消，不调后端。"""
    os.environ["FAKE_MODE"] = "deliver"
    try:
        envf = tmp_path / "env.local"
        envf.write_text("ANTHROPIC_BASE_URL=https://fake\n"
                        "ANTHROPIC_AUTH_TOKEN=d\n", encoding="utf-8")
        be = ClaudeCliBackend(cli_path=SHIM, env_file=envf, timeout=30)
        ev = threading.Event()
        ev.set()  # 调用前已置位
        wd = tmp_path / "wd_pre"
        r = be.run("x", wd, None, "预取消", cancel_event=ev)
        assert r["resources"].get("pre_cancel") is True
        assert r["resources"]["receipt_status"] == "incomplete"
        assert not (wd / "hello.txt").is_file()  # 未启动进程、无产物
        # ② 播种后复查：hard_stop 期间认领的尝试在播种点被取消
        repo = Repository(tmp_path / "m" / "mission.db")
        g = Graph(repo)
        mid, root = g.create_mission("N5播种窗", brief=BRIEF,
                                     root_check=f'python "{CHECKER}" hello.txt DAG-OK',
                                     root_declared_artifacts=("hello.txt",))
        nid = g.add_node(mid, "L", BRIEF, parent_id=root,
                         check_cmd=f'python "{CHECKER}" hello.txt DAG-OK',
                         declared_artifacts=("hello.txt",))
        s = Scheduler(repo, g, be, max_concurrency=1, reap_enabled=False)
        s.hard_stop.set()
        aid = g.start_attempt(mid, nid, backend="stub")
        ev2 = threading.Event()
        s._work(mid, nid, aid, False)  # 直接走执行包（播种后复查命中）
        att = repo.get_attempt(aid)
        assert att.status == "cancelled"
        evs = [e.type for e in repo.events_of(mid)]
        assert "attempt_cancelled" in evs
        assert not (Path(repo.get_attempt(aid).workdir)
                    / "hello.txt").is_file()
        repo.close()
    finally:
        os.environ.pop("FAKE_MODE", None)


def test_settlement_fields_protected_from_external_patch(tmp_path):
    """状态保护：外部回执补丁不得覆盖 settled_valid/cost_charged 等平台
    结算字段（_internal 结算路径豁免）。"""
    repo = Repository(tmp_path / "m" / "mission.db")
    g = Graph(repo)
    mid, root = g.create_mission("保护")
    nid = g.add_node(mid, "L", BRIEF, parent_id=root)
    aid = g.start_attempt(mid, nid, backend="stub")
    from dag_agent.executor_contract import (
        merge_executor_resources, ContractError)
    with pytest.raises(ContractError):
        merge_executor_resources(repo, mid, nid, aid, {
            "settled_valid": True, "cost_charged": {"amount": 99.0}})
    # 内部路径豁免（对照）
    record_execution(repo, aid, _mk_ok_receipt(1.0), mid=mid, nid=nid)
    rj = json.loads(repo.get_attempt(aid).resources_json)
    assert rj["settled_valid"] is True and \
        rj["cost_charged"]["amount"] == 1.0
    repo.close()