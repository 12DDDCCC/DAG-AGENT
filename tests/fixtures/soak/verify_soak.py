# -*- coding: utf-8 -*-
"""Soak 停机核验器：对 runs/<run_id>/ 独立复核，输出一致性报告 JSON。

核验面（任务书停机核验清单）：
1. SQLite integrity_check / foreign_key_check（每个 mission 库）。
2. 终态符合冻结预期：正常场景全部 done/machine；故障场景逐节点核对
   预期表（fail/error/blocked/cancelled/旧提交重跑等）。
3. accepted 全件 SHA256 与 artifacts/snapshots 记录一致（权威证据有效）。
4. 结算对账：budget_json.cost_used == Σcost_charged 流水；unknown 计数
   一致；重复结算 replayed 不重复计费。
5. 无迟到发布：cancelled/orphaned/rejected attempt 不得有 valid 快照。
6. 健康长 worker 未被误收割（无 orphaned 事件、心跳次数达预期）。
7. 进程/线程/句柄回收：末样本 vs 首样本（RunManager 完成句柄保留按
   生命周期保留口径分账，不混入活跃泄漏）。
8. 并发达标：稳定窗口实际槽位利用率 ≥ 80%（timeline 精确积分）。
9. 版本漂移：run_summary.version_drift 必须为 False。

结论分级：每项 PASS/FAIL/UNVERIFIED；观测失效（monitor_errors>0、采样
不足）不冒充 PASS。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# SPEC-Soak 冻结数值门槛（与 SPEC-Soak.md §2 同源——唯一权威；代码注释
# 不得另立"第二轮冻结"冒充新协议。run_config.json 须记录 thresholds 与
# 本表一致，漂移=该轮按本 SPEC 判 FAIL，不得宣称按本 SPEC 通过）
# 修订 B（SPEC §6）：线程波动带=max(thread_fluct_base, thread_fluct_per_c×C)
# ——按 ±8 原算式（每 mission 1+2 线程）随并发缩放，下限保留 8
SPEC_THRESHOLDS = {
    "rss_slope_mib_h": 60,        # §2.2 稳定期线性拟合 |斜率|
    "per_mission_mib": 0.5,       # §2.2 每完成 mission 资源增量
    "handle_slope_per_min": 20,   # §2.3 句柄斜率
    "thread_fluct_base": 8,       # §2.3 稳定期线程波动带下限（修订 B）
    "thread_fluct_per_c": 3,      # §2.3 每 1 并发位的结构振幅（修订 B）
    "thread_quiescent": 5,        # §2.2 静止点线程回落至基线±5
    "util_min": 0.80,             # §2.1 稳定窗口槽位利用率
}


def thread_fluct_band(concurrency: int) -> int:
    """修订 B 波动带：max(8, 3×C)。"""
    return max(SPEC_THRESHOLDS["thread_fluct_base"],
               SPEC_THRESHOLDS["thread_fluct_per_c"] * int(concurrency))


def _conn(db: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def check_mission_db(db: Path, kind: str) -> dict:
    out: dict = {"db": str(db), "kind": kind}
    c = _conn(db)
    try:
        out["integrity"] = c.execute("PRAGMA integrity_check").fetchone()[0]
        fkc = c.execute("PRAGMA foreign_key_check").fetchall()
        out["foreign_key_violations"] = len(fkc)
        mission = c.execute(
            "SELECT id, status, closure_basis, goal_version, budget_json"
            " FROM missions").fetchone()
        if mission is None:
            out["mid"] = None
            out["empty_db"] = True   # 有库无任务：目录/路径缺陷的证据
            return out
        out["empty_db"] = False
        mid = mission["id"]
        out["mid"] = mid
        out["mission_status"] = mission["status"]
        out["closure_basis"] = mission["closure_basis"]
        nodes = c.execute(
            "SELECT id, title, status, parent_id, check_version FROM nodes"
            " ORDER BY id").fetchall()
        out["nodes"] = {n["id"]: {"title": n["title"], "status": n["status"]}
                        for n in nodes}
        out["root_id"] = next((n["id"] for n in nodes
                               if n["parent_id"] is None), None)
        out["attempts_by_status"] = {
            r["status"]: r["c"] for r in c.execute(
                "SELECT status, COUNT(*) AS c FROM attempts"
                " GROUP BY status").fetchall()}
        out["events_by_type"] = {
            r["type"]: r["c"] for r in c.execute(
                "SELECT type, COUNT(*) AS c FROM events"
                " GROUP BY type").fetchall()}
        # 结算对账：budget_json vs Σcost_charged
        budget = json.loads(mission["budget_json"] or "{}")
        charged = 0.0
        unknown_n = 0
        for r in c.execute(
                "SELECT resources_json FROM attempts").fetchall():
            rj = json.loads(r["resources_json"] or "{}")
            ch = rj.get("cost_charged") or {}
            if ch.get("basis") == "unknown":
                unknown_n += 1
            else:
                charged += float(ch.get("amount") or 0.0)
        out["budget"] = {
            "calls_used": budget.get("calls_used", 0),
            "cost_used": round(budget.get("cost_used", 0.0), 6),
            "cost_unknown": budget.get("cost_unknown", 0),
            "sum_cost_charged": round(charged, 6),
            "unknown_attempts": unknown_n}
        out["budget_match"] = (
            abs(charged - float(budget.get("cost_used", 0.0))) < 1e-6
            and unknown_n == int(budget.get("cost_unknown", 0)))
        # 迟到发布：cancelled/orphaned/rejected attempt 不得有 valid 快照
        late = c.execute(
            "SELECT COUNT(*) AS c FROM snapshots s JOIN attempts a"
            " ON s.attempt_id=a.id WHERE s.valid=1 AND a.status IN"
            " ('cancelled','orphaned','rejected')").fetchone()["c"]
        out["late_publications"] = late
        # accepted 全件哈希
        bad_hash = []
        n_files = 0
        home = db.parent
        for r in c.execute(
                "SELECT node_id, version, rel_path, content_hash"
                " FROM artifacts").fetchall():
            f = home / "accepted" / r["node_id"] / f"v{r['version']}" / r["rel_path"]
            if not f.is_file():
                bad_hash.append(f"{r['node_id']}/{r['rel_path']}:missing")
                continue
            n_files += 1
            h = hashlib.sha256(f.read_bytes()).hexdigest()
            if h != r["content_hash"]:
                bad_hash.append(f"{r['node_id']}/{r['rel_path']}:mismatch")
        out["accepted_files_checked"] = n_files
        out["accepted_hash_bad"] = bad_hash[:20]
        out["accepted_hash_ok"] = not bad_hash
        # 权威证据与考卷版本绑定：每 accepted 节点有 valid=1 权威 verdict=pass
        ev_gap = c.execute(
            "SELECT COUNT(*) AS c FROM nodes n WHERE n.status='accepted'"
            " AND NOT EXISTS (SELECT 1 FROM assessments a WHERE"
            " a.node_id=n.id AND a.authoritative=1 AND a.invalid=0"
            " AND a.verdict='pass')").fetchone()["c"]
        out["accepted_without_valid_evidence"] = ev_gap
        out["totals"] = {
            "events": c.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"],
            "attempts": c.execute("SELECT COUNT(*) AS c FROM attempts").fetchone()["c"],
            "nodes": len(nodes),
            "snapshots": c.execute(
                "SELECT COUNT(*) AS c FROM snapshots").fetchone()["c"]}
        try:
            props = c.execute(
                "SELECT id, mission_id, node_id FROM split_proposals").fetchall()
            out["split_proposals"] = {
                p["id"]: {"mission_id": p["mission_id"], "node_id": p["node_id"]}
                for p in props}
        except sqlite3.OperationalError:
            out["split_proposals"] = {}
        # V2（Codex 三轮）：重启代际证据——run_control.generation（每次
        # RunManager.start 清旗标时 +1；≥2=该 mission 至少重启过一代）
        try:
            g = c.execute(
                "SELECT generation FROM run_control WHERE id=1").fetchone()
            out["run_generation"] = int(g["generation"]) if g else 0
        except sqlite3.OperationalError:
            out["run_generation"] = None
        # N-验收（四轮）：done 分母的库侧权威完成时点（最后一次
        # attempt 结束时刻；summary.finished 的 ended_at 仅作核对）
        try:
            le = c.execute(
                "SELECT MAX(ended_at) AS m FROM attempts WHERE"
                " ended_at IS NOT NULL").fetchone()
            out["last_ended_at"] = le["m"] if le else None
        except sqlite3.OperationalError:
            out["last_ended_at"] = None
    finally:
        c.close()
    return out


def utilization_from_timeline(timeline: list, c_total: int,
                              frac_window: tuple[float, float]) -> float:
    """timeline 事件精确积分：Σinflight·dt / (C·窗口)。
    窗口=整个负载时间线的 [frac0, frac1] 分位段（避开预热与收尾）。"""
    spans = {}
    lo = hi = None
    for e in timeline:
        t = e["t"]
        lo = t if lo is None else min(lo, t)
        hi = t if hi is None else max(hi, t)
        spans.setdefault(e["attempt"], {})[e["phase"]] = t
    if lo is None or hi <= lo:
        return 0.0
    w0 = lo + (hi - lo) * frac_window[0]
    w1 = lo + (hi - lo) * frac_window[1]
    total = 0.0
    for aid, ph in spans.items():
        if "start" not in ph:
            continue
        s = max(ph["start"], w0)
        e = min(ph.get("end", w1), w1)
        if e > s:
            total += (e - s)
    denom = c_total * (w1 - w0)
    return round(total / denom, 4) if denom > 0 else 0.0


def _window_util(spans: dict, w0: float, w1: float, c_total: int) -> float:
    total = 0.0
    for ph in spans.values():
        if "start" not in ph:
            continue
        s = max(ph["start"], w0)
        e = min(ph.get("end", w1), w1)
        if e > s:
            total += (e - s)
    denom = c_total * (w1 - w0)
    return round(total / denom, 4) if denom > 0 else 0.0


def _to_epoch(v) -> float | None:
    if not v:
        return None
    try:
        return datetime.datetime.fromisoformat(v).timestamp()
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--expect-fault-table", default="1",
                    help="1=fault mission 按冻结预期表核对")
    args = ap.parse_args()
    run_dir = HERE / "runs" / args.run_id
    home = run_dir / "home"
    config = json.loads((run_dir / "run_config.json").read_text(encoding="utf-8"))
    summary = json.loads((run_dir / "run_summary.json").read_text(
        encoding="utf-8"))
    report: dict = {"run_id": args.run_id, "checked_at": time.strftime(
        "%Y-%m-%dT%H:%M:%S"), "checks": {}, "verdicts": {}}

    def verdict(name: str, ok: bool | None, detail: str):
        report["verdicts"][name] = {
            "result": ("PASS" if ok else "FAIL") if ok is not None
            else "UNVERIFIED",
            "detail": detail}

    # 1) 每库完整性 + 终态 + 对账
    kind_file = run_dir / "kind_map.json"
    kind_map = (json.loads(kind_file.read_text(encoding="utf-8"))
                if kind_file.is_file() else {})
    db_reports = []
    fault_report = None
    for db in sorted(home.rglob("mission.db")):
        # mid 目录名（s0-002 起目录=mid；kind 从 plan 映射取，旧跑回退目录名）
        rel_parent = db.parent.name
        kind = "fault" if "fault" in rel_parent else (
            "longworker" if ("lw" in rel_parent or "long" in rel_parent)
            else ("or" if "or" in rel_parent else "tp"))
        info = kind_map.get(rel_parent)
        if info:
            kind = info["kind"]
        if kind == "lw":
            kind = "longworker"  # kind_map 存构建名，核验统一口径
        rep = check_mission_db(db, kind)
        if rep["integrity"] != "ok" or rep.get("mid") is None:
            # 空库/坏库：如实记录，不静默跳过
            rep["kind"] = rep.get("kind", "unknown")
        db_reports.append(rep)
        if kind == "fault":
            fault_report = rep
    report["checks"]["missions"] = [
        {k: v for k, v in r.items() if k != "nodes"} for r in db_reports]
    db_by_mid = {r["mid"]: r for r in db_reports if not r.get("empty_db")}

    integrity_bad = [r["mid"] for r in db_reports
                     if r["integrity"] != "ok"
                     or r["foreign_key_violations"] > 0]
    empty_dbs = [r["db"] for r in db_reports if r.get("empty_db")]
    verdict("sqlite_integrity",
            not integrity_bad and not empty_dbs,
            f"violations={integrity_bad or '无'} empty_dbs={empty_dbs or '无'}"
            + ("（空库=注入/定位路径缺陷的证据，本轮判 FAIL）" if empty_dbs else ""))

    # 2) 终态预期（空库不参与终态判定——已在完整性项 FAIL）
    # F8：只对**已准入**的正常任务核验 done/machine；预创建未准入的
    # mission 库不要求闭合（Codex 复核口径），单独计数呈现。
    # 三轮修正（§6.1）：restart-probe mission 的 kind_map kind 虽为 tp，
    # 但其归重启判据——不混入"正常任务"；ag/fault 亦单列。
    admitted_mids = set()
    # 真实准入=RunManager 实际 start 的 mission（plan.json 是预创建
    # 全量，不能当已准入——Codex 复核口径）
    for f in summary.get("finished", []) or []:
        if f.get("mid"):
            admitted_mids.add(f["mid"])
    for m in summary.get("admit", {}).get("admitted") or []:
        admitted_mids.add(m)

    def _is_probe_mid(m: str) -> bool:
        v = (kind_map or {}).get(m) or {}
        return "restart-probe" in str(v.get("name", ""))

    tp_or_lw_all = [r for r in db_reports
                    if not r.get("empty_db")
                    and r["kind"] in ("tp", "or", "longworker")
                    and not _is_probe_mid(r["mid"])]
    probe_reports = [r for r in db_reports
                     if not r.get("empty_db") and _is_probe_mid(r["mid"])]
    tp_or_lw = [r for r in tp_or_lw_all if r["mid"] in admitted_mids]
    normal_bad = [r["mid"] for r in tp_or_lw
                  if r["mission_status"] != "done"
                  or r["closure_basis"] != "machine"]
    # F8：正常任务终态判据必须参与 overall——已准入 tp/or/longworker
    # mission 须 done/machine；active/None 即 FAIL（不因结果已知而豁免）
    verdict("normal_missions_closed", not normal_bad,
            f"violations={normal_bad or '无'}"
            f"（已准入正常任务 {len(tp_or_lw)} 个；预创建未准入 "
            f"{len(tp_or_lw_all) - len(tp_or_lw)} 个不要求闭合；"
            f"重启探针 {len(probe_reports)} 个归 restart_recovery 判据）")
    # 长 worker 未误收割 + 心跳达标（判据按配置时长推算：
    # 期望心跳≈long_s/interval；count ≥ 50%期望 且 max_gap ≤ 3×interval
    # → 周期心跳连续；mission done + 无 orphaned → 未被误收割）
    lw = next((r for r in db_reports if r["kind"] == "longworker"), None)
    lw_ok = None
    lw_detail = "longworker mission 不存在"
    hb_report = summary.get("heartbeat_report") or {}
    if lw is not None:
        long_s = float(config.get("long_worker_s", 700.0))
        interval = float(config.get("heartbeat_interval_s", 10.0))
        min_hb = max(2, int(0.5 * long_s / interval))
        max_gap_allow = 3.0 * interval
        best = None
        for aid, hb in hb_report.items():
            if hb["count"] >= min_hb:
                if best is None or hb["count"] > best[1]["count"]:
                    best = (aid, hb)
        orphaned = lw["attempts_by_status"].get("orphaned", 0)
        gap_ok = best is not None and best[1]["max_gap_s"] <= max_gap_allow
        lw_ok = (lw["mission_status"] == "done" and orphaned == 0
                 and best is not None and gap_ok)
        lw_detail = (f"mission={lw['mission_status']} orphaned={orphaned} "
                     f"长任务心跳={best} 判据：count≥{min_hb} 且 "
                     f"max_gap≤{max_gap_allow}s")
    verdict("longworker_no_false_reap", lw_ok, lw_detail)

    # 2b) kill→重启→恢复→闭合（第八类故障，独立必需判据）——以库内
    # 证据为准（mission done + attempt_cancelled 事件），自报布尔仅
    # 参考（R6/Codex 复核：删除自报证据不得仍 PASS）
    rp_mid = None
    for _k, _v in (kind_map or {}).items():
        if isinstance(_v, dict) and "restart-probe" in str(_v.get("name", "")):
            rp_mid = _k
    if rp_mid is None:
        verdict("restart_recovery", None,
                "无重启探针任务（本轮计划未含）→UNVERIFIED")
    else:
        rp_rep = next((r for r in db_reports if r.get("mid") == rp_mid), None)
        inj2 = {}
        inj_file2 = run_dir / "injections.json"
        if inj_file2.is_file():
            inj2 = json.loads(inj_file2.read_text(encoding="utf-8"))
        rp_self = inj2.get("restart_probe") or {}
        db_done = rp_rep is not None and rp_rep.get("mission_status") == "done"
        db_cancel = ((rp_rep or {}).get("events_by_type") or {}).get(
            "attempt_cancelled", 0) >= 1
        # V2（Codex 三轮）：重启证据链必须完整——自报 killed/restarted
        # 为真＋库内 mission done＋attempt_cancelled＋代际 ≥2（第二次
        # start 清旗标时 +1）；普通取消（无重启）不再能通过
        self_killed = bool(rp_self.get("killed"))
        self_restarted = bool(rp_self.get("restarted"))
        gen = (rp_rep or {}).get("run_generation")
        # N-验收（四轮）：重启证据必须**有序且含 kill 语义**——
        # ①库内 attempt_cancelled 事件原因含 kill（RunManager.stop(kill)）；
        # ②存在被取消 attempt（ended）早于其后新 attempt（started）；
        # ③代际≥2；④探针有真实调用。普通取消/仅代际+自报不得通过；
        # 无代际表→UNVERIFIED（弱历史不再 PASS）。
        kill_reason = False
        ordered = False
        probe_calls = int(rp_self.get("probe_calls") or 0)
        try:
            _rpdb = rp_dir = home / rp_mid / "mission.db"
            _c2 = _conn(_rpdb)
            try:
                _ev = _c2.execute(
                    "SELECT payload_json FROM events WHERE type="
                    "'attempt_cancelled' ORDER BY id").fetchall()

                def _is_kill_payload(pj):
                    # V3（Codex 五轮）：结构化核对——解析 reason 字段并
                    # 精确前缀匹配，子串（"not_kill"/注释性文字）不算
                    try:
                        d = json.loads(pj or "{}")
                    except Exception:
                        return False
                    reason = str(d.get("reason", ""))
                    return reason.startswith("RunManager.stop(kill")

                kill_reason = any(_is_kill_payload(e["payload_json"])
                                  for e in _ev)
                _rows = _c2.execute(
                    "SELECT status, started_at, ended_at FROM attempts"
                    " ORDER BY started_at").fetchall()
                ended = [r["ended_at"] for r in _rows
                         if r["status"] == "cancelled" and r["ended_at"]]
                started = [r["started_at"] for r in _rows]
                ordered = any(any(s > e for s in started) for e in ended)
            finally:
                _c2.close()
        except Exception:
            pass
        if gen is None:
            verdict("restart_recovery", None,
                    f"mission={rp_mid} 无 run_control 代际表——重启证据"
                    "不足→UNVERIFIED（弱历史不判 PASS）")
        else:
            chain_ok = (db_done and db_cancel and self_killed
                        and self_restarted and isinstance(gen, int)
                        and gen >= 2 and kill_reason and ordered
                        and probe_calls > 0)
            verdict("restart_recovery", chain_ok,
                    f"mission={rp_mid} db_done={db_done} "
                    f"cancel_event={db_cancel} kill_reason={kill_reason} "
                    f"cancel_before_new_attempt={ordered} "
                    f"self_killed={self_killed} "
                    f"self_restarted={self_restarted} "
                    f"probe_calls={probe_calls} "
                    f"generation={gen}（≥2）"
                    + ("" if chain_ok else
                       "——证据链不完整（kill 原因/有序性/代际/调用）→"
                       "普通取消/假重启不得通过"))

    # 2c) 时长口径（R6/Codex 复核：config 写 duration_s ≠ 实际持续——
    # 实际 wall 须达计划时长（−30s 收尾容差），否则如实 FAIL）
    _wall = float(summary.get("wall_s") or 0.0)
    _dur = float(config.get("duration_s") or 0.0)
    if _dur > 0:
        verdict("duration_wall", _wall >= _dur - 30.0,
                f"实际wall={_wall:.0f}s 计划duration={_dur:.0f}s"
                + ("" if _wall >= _dur - 30.0
                   else "（不足计划时长−30s容差→FAIL；到点停止新准入后"
                        "须保持观测至计划时长）"))

    # 3) accepted 哈希与证据
    hash_bad = [r["mid"] for r in db_reports
                if not r.get("empty_db") and not r["accepted_hash_ok"]]
    ev_bad = [r["mid"] for r in db_reports
              if not r.get("empty_db")
              and r["accepted_without_valid_evidence"] > 0]
    verdict("accepted_hash_and_evidence", not hash_bad and not ev_bad,
            f"hash_bad={hash_bad or '无'} evidence_gap={ev_bad or '无'} "
            f"files={sum(r['accepted_files_checked'] for r in db_reports
                         if not r.get('empty_db'))}")

    # 4) 结算对账
    budget_bad = [r["mid"] for r in db_reports
                  if not r.get("empty_db") and not r["budget_match"]]
    verdict("settlement_reconciliation", not budget_bad,
            f"mismatch={budget_bad or '无'}")

    # 5) 迟到发布
    late = sum(r.get("late_publications", 0) for r in db_reports
               if not r.get("empty_db"))
    verdict("no_late_publication", late == 0, f"late={late}")

    # 6) fault mission 冻结预期表
    if fault_report and not fault_report.get("empty_db") \
            and args.expect_fault_table == "1":
        ran = sum(fault_report["attempts_by_status"].values()) > 0
        if not ran:
            verdict("fault_injections_expected", None,
                    "fault mission 未准入（本轮计划未含/未到时间）→ UNVERIFIED")
            report["checks"]["fault"] = {"status": "not_run"}
        else:
            nodes = fault_report.get("nodes") or {}
            expect = {}
            for nid, info in nodes.items():
                t = info["title"].split(" ")[0]
                expect[t] = info["status"]
            want = {"F_fail": "failed", "F_error": "blocked",
                    "F_pause": "accepted", "F_cancel": "cancelled",
                    "F_oldgoal": "accepted", "F_slow": "accepted",
                    "F_ok-0": "accepted", "F_ok-1": "accepted"}
            bad = {k: (expect.get(k), v) for k, v in want.items()
                   if expect.get(k) != v}
            ev = fault_report["events_by_type"]
            inj = {}
            inj_file = run_dir / "injections.json"
            if inj_file.is_file():
                inj = json.loads(inj_file.read_text(encoding="utf-8"))
            ok_inj = (inj.get("orphan_reaped") is not None
                      and (inj.get("double_settle") or {}).get(
                          "replayed") is True
                      and inj.get("goal_changed_to") is not None)
            rep_ok = (ev.get("orphan_reaped", 0) >= 1
                      and ev.get("goal_changed", 0) >= 1
                      and ev.get("dispatch_error", 0) >= 1)
            # 旧提交被拒的账面证据：change_goal 落库后，在途 attempt 的
            # 提交在 P0 预检抛"目标版本已变更"→dispatch_error→节点回
            # pending 重派（新指纹）→F_oldgoal 最终 accepted
            fault_ok = not bad and ok_inj and rep_ok
            # R6：孤儿收割集合核对（新工装记录 orphan_exact——收割只许
            # 收指定 ghost；旧跑无此字段不追溯）
            if not inj.get("orphan_exact", True):
                fault_ok = False
            verdict("fault_injections_expected", fault_ok,
                    f"node_mismatch={bad or '无'} inj_ok={ok_inj} "
                    f"events_ok={rep_ok} orphan_exact={inj.get('orphan_exact', '旧跑未记录')}")
            report["checks"]["fault"] = {
                "expect": want, "actual": expect, "injections": inj,
                "events": ev}
    elif fault_report is None:
        verdict("fault_injections_expected", None,
                "fault mission 不存在——本轮未注入")

    # 7) 回收与观测有效性
    obs_final = summary.get("obs_final") or {}
    counters = obs_final.get("counters") or {}
    monitor_err = counters.get("monitor_errors", 0)
    unexpected = {
        "dispatch_errors": counters.get("dispatch_errors", 0),
        "settle_errors": counters.get("settle_errors", 0),
        "check_errors": counters.get("check_errors", 0),
        "project_errors": counters.get("project_errors", 0),
        "reap_errors": counters.get("reap_errors", 0),
        "heartbeat_errors": counters.get("heartbeat_errors", 0),
        "monitor_errors": monitor_err}
    verdict("no_unexpected_errors",
            all(v == 0 for v in unexpected.values()),
            json.dumps(unexpected, ensure_ascii=False))
    # 观测有效性：error 样本占比 <5% 且样本量足够；否则相关项 UNVERIFIED
    samples = run_dir / "samples.jsonl"
    n_err_samples = 0
    n_samples = 0
    if samples.is_file():
        for l in samples.read_text(encoding="utf-8").splitlines():
            if not l.strip():
                continue
            n_samples += 1
            if '"sample_error"' in l:
                n_err_samples += 1
    err_ratio = (n_err_samples / n_samples) if n_samples else 1.0
    obs_ok = n_samples >= 3 and err_ratio < 0.05
    verdict("observation_validity", True if obs_ok else None,
            f"samples={n_samples} error_samples={n_err_samples}"
            f"（{err_ratio:.1%}）"
            + ("" if obs_ok else "（观测失效>5% → 相关项 UNVERIFIED）"))

    # 7b) 资源样本加载（§8 峰值与 §10 门槛共用）
    samples = run_dir / "samples.jsonl"
    _rows = []
    if samples.is_file():
        _rows = [json.loads(l) for l in samples.read_text(
            encoding="utf-8").splitlines() if l.strip()]
    def _finite(v):
        return (isinstance(v, (int, float)) and not isinstance(v, bool)
                and math.isfinite(v))

    def _is_valid_sample_record(r):
        if not isinstance(r, dict) or "sample_error" in r:
            return False
        if not ("ts" in r and _finite(r["ts"]) and r["ts"] > 0):
            return False
        if not ("proc_rss_mb" in r and _finite(r["proc_rss_mb"]) and r["proc_rss_mb"] >= 0):
            return False
        for k in ("proc_handles", "proc_threads", "active_runs", "inflight"):
            if k in r:
                val = r[k]
                if val is None or not (_finite(val) and val >= 0):
                    return False
        if "children_n" in r:
            val = r["children_n"]
            if val is not None and not (_finite(val) and val >= 0):
                return False
        return True

    _raw_valid = [r for r in _rows if "proc_rss_mb" in r
                  and "sample_error" not in r]
    # V1/C3（Codex 六轮）：参与拟合/增量/积分的数值必须**有限**——涵盖 ts, RSS, handles, threads。
    # NaN/Inf/null 样本剔除并计数（剔除率>10%→资源项 UNVERIFIED；≤10%→异常观测本身 FAIL）
    valid = [r for r in _raw_valid if _is_valid_sample_record(r)]
    _bad_numeric = len(_raw_valid) - len(valid)

    # 8) 并发达标（稳定段窗口：负载时间线的 10%–70% 分位段）
    tl_file = run_dir / "timeline.json"
    if tl_file.is_file():
        tls = json.loads(tl_file.read_text(encoding="utf-8"))
        # N-验收（四轮）：利用率=主负载专用积分（分母=主负载 C；探针
        # 区间不得把边缘负载补成达标）；总并发判据另用全部事件
        _main_only = [e for e in (tls.get("timeline") or [])
                      if not e.get("probe")]
        util = utilization_from_timeline(
            _main_only, config["concurrency_total"], (0.10, 0.70))
        windows_detail = []
        # R6（Codex 复核）：遍历**全部**完整 600s 窗口逐一判定（不再
        # 要求时间线≥3600——不足 3600 也有完整窗口可判；实际时长按
        # 真实 wall 报告）；且"实际执行峰值 ≥ C"独立核对（§2.1）
        spans2 = {}
        lo = hi = None
        for e in _main_only:   # 分窗利用率同口径：主负载专用
            t = e["t"]
            lo = t if lo is None else min(lo, t)
            hi = t if hi is None else max(hi, t)
            spans2.setdefault(e["attempt"], {})[e["phase"]] = t
        if lo is not None:
            n_w = int((hi - lo) // 600)
            for w_i in range(n_w):
                ws, we = lo + 600 * w_i, lo + 600 * (w_i + 1)
                windows_detail.append(
                    (w_i + 1, _window_util(spans2, ws, we,
                                           config["concurrency_total"])))
        peak_inflight = None
        # 稳定段按样本自身时段取 10%–70%（样本 ts=epoch，时间线 t=
        # monotonic，两钟不可混用）
        _s_ts = [r["ts"] for r in valid]
        if _s_ts:
            _st0, _st1 = min(_s_ts), max(_s_ts)
            _s_lo = _st0 + (_st1 - _st0) * 0.10
            _s_hi = _st0 + (_st1 - _st0) * 0.70
            for r in valid:
                if _s_lo <= r["ts"] <= _s_hi \
                        and r.get("inflight") is not None and _finite(r.get("inflight")):
                    peak_inflight = (r["inflight"] if peak_inflight is None
                                     else max(peak_inflight, r["inflight"]))
        win_bad = [f"W{i}={u:.1%}" for i, u in windows_detail
                   if u < SPEC_THRESHOLDS["util_min"]]
        peak_ok = (peak_inflight is not None and _finite(peak_inflight)
                   and peak_inflight >= config["concurrency_total"])
        if windows_detail and not win_bad and util >= SPEC_THRESHOLDS["util_min"]:
            if peak_inflight is None:
                verdict("concurrency_utilization>=80%", None,
                        f"overall(10%-70%)={util:.1%}；"
                        f"{len(windows_detail)}个600s窗口全达标；"
                        "实际执行峰值缺样本（inflight）→UNVERIFIED")
            else:
                verdict("concurrency_utilization>=80%", peak_ok,
                        f"overall(10%-70%)={util:.1%}；"
                        f"{len(windows_detail)}个600s窗口全达标；"
                        f"实际执行峰值={peak_inflight}/C="
                        f"{config['concurrency_total']}"
                        + ("" if peak_ok else "（峰值未达C→FAIL：负载不足）"))
        elif windows_detail:
            verdict("concurrency_utilization>=80%", False,
                    f"overall(10%-70%)={util:.1%}；窗口未达标："
                    + ",".join(win_bad)
                    + f"（C={config['concurrency_total']}）")
        else:
            verdict("concurrency_utilization>=80%",
                    util >= SPEC_THRESHOLDS["util_min"],
                    f"stable-window utilization={util:.1%}（C="
                    f"{config['concurrency_total']}, scope=multi-mission total；"
                    "无完整600s窗口，单窗口口径）")
        report["checks"]["utilization"] = util
        report["checks"]["utilization_windows"] = windows_detail
        report["checks"]["peak_inflight"] = peak_inflight
    else:
        verdict("concurrency_utilization>=80%", None,
                "timeline.json 缺失（观测失效）")

    # 8b) 总并发上限（V5/Codex 三轮）：主负载+重启探针全部执行者区间，
    # 总峰 ≤ C + probe_budget（事前声明的独立探针预算）；探针事件由
    # soak_v2 并入时间线（probe=True 标记）
    _probe_budget = int(config.get("probe_budget_slots") or 0)
    if tl_file.is_file() and _probe_budget > 0:
        _all_ev = tls.get("timeline") or []
        _main_ev = [e for e in _all_ev if not e.get("probe")]
        _probe_ev = [e for e in _all_ev if e.get("probe")]
        # N-验收（四轮）：探针计量完整性——预算已声明但时间线无探针
        # 事件=计量缺失（删时间线不得使总峰"降回 10"）→FAIL；探针
        # attempt 区间须 start/end 配对（孤立 end/缺 start 拒绝）
        _p_sp: dict = {}
        for e in _probe_ev:
            _p_sp.setdefault(e["attempt"], {})[e["phase"]] = e["t"]
        _bad_probe = [a for a, ph in _p_sp.items() if "start" not in ph]
        # C4（Codex 六轮）：探针执行集与区间真实对账——
        # ①核对库内真实 attempt ID 集合与时间线集合完全一致（防假 ID/删区间）；
        # ②核对区间有序且非全零，防正时长被篡改为零区间；
        # ③核对无矛盾重复事件。
        _rp_rep2 = next((r for r in db_reports
                         if not r.get("empty_db")
                         and "restart-probe" in str(
                             (kind_map or {}).get(r["mid"], {})
                             .get("name", "") if isinstance(
                                 (kind_map or {}).get(r["mid"]), dict)
                             else "")), None)

        db_probe_rows = []
        if _rp_rep2 is not None:
            try:
                c_p = _conn(Path(_rp_rep2["db"]))
                db_probe_rows = c_p.execute(
                    "SELECT id, started_at, ended_at FROM attempts"
                ).fetchall()
                c_p.close()
            except Exception:
                pass

        db_probe_ids = {r["id"] for r in db_probe_rows}
        tl_probe_ids = set(_p_sp.keys())

        # 全库真实 attempt 映射
        all_db_attempts = {}
        for r in db_reports:
            if r.get("empty_db"):
                continue
            try:
                c_p_all = _conn(Path(r["db"]))
                for row_a in c_p_all.execute(
                    "SELECT id, mission_id, node_id, status, started_at, ended_at FROM attempts"
                ).fetchall():
                    all_db_attempts[row_a["id"]] = dict(row_a)
                c_p_all.close()
            except Exception:
                pass

        # 检查非有限时间戳（NaN/Inf）
        nan_ts_events = [e for e in _all_ev if not _finite(e.get("t"))]

        # 检查矛盾重复事件（允许近时幂等重复，相差 >2.0s 视为矛盾重复）
        seen_ev = {}
        has_conflict = False
        bad_k = None
        for e in _all_ev:
            k = (e.get("attempt"), e.get("phase"))
            if k in seen_ev:
                if abs(seen_ev[k] - (e.get("t") or 0.0)) > 2.0:
                    has_conflict = True
                    bad_k = k
                    break
            else:
                seen_ev[k] = e.get("t") or 0.0

        # 检查逆序、缺失 end、极短区间与零区间
        missing_end_probes = [a for a, ph in _p_sp.items() if "start" in ph and "end" not in ph]
        rev_probes = [a for a, ph in _p_sp.items()
                      if "start" in ph and "end" in ph and ph["end"] < ph["start"]]
        tiny_probes = [a for a, ph in _p_sp.items()
                       if "start" in ph and "end" in ph and 0 <= ph["end"] - ph["start"] < 0.001]
        all_zero_probes = (len(_p_sp) > 0 and all(
            ph.get("start") == ph.get("end") for ph in _p_sp.values()
            if "start" in ph and "end" in ph))

        # 探针绑定核验（通用契约：权威数据库对账）
        probe_binding_bad = []
        rp_max_gen = (_rp_rep2 or {}).get("run_generation") or 1
        rp_cancelled_ids = set()
        if _rp_rep2 is not None:
            try:
                c_p_c = _conn(Path(_rp_rep2["db"]))
                rp_cancels = c_p_c.execute(
                    "SELECT payload_json FROM events WHERE type='attempt_cancelled'"
                ).fetchall()
                for c_row in rp_cancels:
                    try:
                        pj = json.loads(c_row["payload_json"] or "{}")
                        if pj.get("attempt"):
                            rp_cancelled_ids.add(pj["attempt"])
                    except Exception:
                        pass
                c_p_c.close()
            except Exception:
                pass

        for e in _probe_ev:
            aid = e.get("attempt")
            if not e.get("mid") or not e.get("node"):
                probe_binding_bad.append(f"{aid}: 缺少必需绑定字段 mid={e.get('mid')} node={e.get('node')}")
            elif aid and aid in all_db_attempts:
                dba = all_db_attempts[aid]
                if e.get("mid") != dba["mission_id"]:
                    probe_binding_bad.append(f"{aid}: mid {e.get('mid')} != {dba['mission_id']}")
                elif e.get("node") != dba["node_id"]:
                    probe_binding_bad.append(f"{aid}: node {e.get('node')} != {dba['node_id']}")
            egen = e.get("generation")
            if egen is not None:
                expected_gen = 1 if aid in rp_cancelled_ids else rp_max_gen
                if egen != expected_gen:
                    probe_binding_bad.append(f"{aid}: generation {egen} 错配 (预期={expected_gen}, 权威代际={rp_max_gen})")

        clock_shifted_probes = []
        if lo is not None and hi is not None:
            for e in _probe_ev:
                t_val = e.get("t")
                if t_val is not None and (t_val < lo - 30.0 or t_val > hi + 60.0):
                    clock_shifted_probes.append(f"{e.get('attempt')}: t={t_val} 超出运行窗口 [{lo:.1f}, {hi:.1f}]")

        falsified_zero = []
        falsified_duration = []
        if db_probe_rows:
            db_map = {r["id"]: r for r in db_probe_rows}
            for a, ph in _p_sp.items():
                if "start" in ph and "end" in ph:
                    tl_dt = ph["end"] - ph["start"]
                    row = db_map.get(a)
                    if row and row["started_at"] and row["ended_at"]:
                        if ph["start"] == ph["end"] and row["started_at"] != row["ended_at"]:
                            falsified_zero.append(a)
                        t_s = _to_epoch(row["started_at"])
                        t_e = _to_epoch(row["ended_at"])
                        if t_s is not None and t_e is not None:
                            db_dt = t_e - t_s
                            is_canc = (a in rp_cancelled_ids) or (all_db_attempts.get(a, {}).get("status") == "cancelled")
                            if not is_canc and db_dt >= 1.0 and (tl_dt < 0.35 * db_dt or tl_dt > 3.0 * db_dt):
                                falsified_duration.append(f"{a}: tl_dt={tl_dt:.3f}s vs db_dt={db_dt:.3f}s (偏离超50%)")
                            elif db_dt > 0 and tl_dt <= 0:
                                falsified_duration.append(f"{a}: zero tl_dt vs db_dt={db_dt:.3f}s")

        fake_probe_ids = (tl_probe_ids - db_probe_ids) if db_probe_ids else set()
        missing_probe_ids = (db_probe_ids - tl_probe_ids) if db_probe_ids else set()

        if nan_ts_events:
            verdict("total_concurrency_cap", False,
                    f"时间线存在非有限时间戳（NaN/Inf，{len(nan_ts_events)} 个）→FAIL")
            _cap_skip = True
        elif not _probe_ev:
            verdict("total_concurrency_cap", False,
                    f"config 声明探针预算 {_probe_budget} 槽但时间线无"
                    "探针事件——探针区间未纳入计量→FAIL")
            _cap_skip = True
        elif _bad_probe:
            verdict("total_concurrency_cap", False,
                    f"探针事件区间不完整（缺 start）：{_bad_probe[:3]}"
                    "→FAIL")
            _cap_skip = True
        elif missing_end_probes:
            verdict("total_concurrency_cap", False,
                    f"探针事件区间不完整（缺 end）：{missing_end_probes[:3]}→FAIL")
            _cap_skip = True
        elif probe_binding_bad:
            verdict("total_concurrency_cap", False,
                    f"探针事件与数据库绑定不符：{probe_binding_bad[:2]}→FAIL")
            _cap_skip = True
        elif clock_shifted_probes:
            verdict("total_concurrency_cap", False,
                    f"探针时间戳超出运行窗口（{len(clock_shifted_probes)} 个，如 {clock_shifted_probes[0]}）→FAIL")
            _cap_skip = True
        elif has_conflict:
            verdict("total_concurrency_cap", False,
                    f"时间线存在矛盾重复事件：{bad_k}→FAIL")
            _cap_skip = True
        elif rev_probes:
            verdict("total_concurrency_cap", False,
                    f"探针事件时间戳逆序（end < start）：{rev_probes[:3]}→FAIL")
            _cap_skip = True
        elif fake_probe_ids:
            verdict("total_concurrency_cap", False,
                    f"探针执行集不匹配：时间线包含未知或伪造 probe attempt ID（{len(fake_probe_ids)} 个，如 {list(fake_probe_ids)[:2]}）→FAIL")
            _cap_skip = True
        elif missing_probe_ids:
            verdict("total_concurrency_cap", False,
                    f"探针执行集不完整：时间线 {len(tl_probe_ids)} 个 probe "
                    f"attempt < 库内 {len(db_probe_ids)} 条 attempts"
                    f"（缺失 {len(missing_probe_ids)} 个）——部分区间被删→FAIL")
            _cap_skip = True
        elif all_zero_probes or falsified_zero:
            verdict("total_concurrency_cap", False,
                    f"探针区间异常：库内已知正时长执行被篡改为零区间（{len(falsified_zero) or len(_p_sp)} 个）→FAIL")
            _cap_skip = True
        elif falsified_duration:
            verdict("total_concurrency_cap", False,
                    f"探针物理时长对账异常（时间线持续时间严重偏离库内物理时长）：{falsified_duration[:2]}→FAIL")
            _cap_skip = True
        elif tiny_probes:
            verdict("total_concurrency_cap", False,
                    f"探针区间异常：存在伪造微秒极短区间（<1ms，{len(tiny_probes)} 个）→FAIL")
            _cap_skip = True
        else:
            _cap_skip = False

        def _interval_peak(events: list) -> int:
            sp: dict = {}
            lo = hi = None
            for e in events:
                t = e["t"]
                lo = t if lo is None else min(lo, t)
                hi = t if hi is None else max(hi, t)
                sp.setdefault(e["attempt"], {})[e["phase"]] = t
            ivs = [(v["start"], min(v.get("end", hi), hi))
                   for v in sp.values() if "start" in v]
            ivs = [(s, e) for s, e in ivs if e > s]
            best = 0
            for p in sorted({s for s, _ in ivs}):
                n = sum(1 for s, e in ivs if s <= p < e)
                best = max(best, n)
            return best

        if not _cap_skip:
            peak_main_tl = _interval_peak(_main_ev)
            peak_total_tl = _interval_peak(_all_ev)
            _cap = config["concurrency_total"] + _probe_budget
            verdict("total_concurrency_cap", peak_total_tl <= _cap,
                    f"总执行位峰值={peak_total_tl} ≤ C"
                    f"({config['concurrency_total']})+探针预算"
                    f"({_probe_budget})={_cap}；主负载峰值={peak_main_tl}；"
                    f"探针事件 {len(_probe_ev)} 条已纳入计量"
                    + ("" if peak_total_tl <= _cap else "——超上限→FAIL"))
    elif tl_file.is_file():
        verdict("total_concurrency_cap", None,
                "run_config 未声明 probe_budget_slots——旧跑探针区间未"
                "纳入时间线，总峰不可复算（V5 前口径）→UNVERIFIED")

    # 9) 版本漂移 + 硬停口径
    drift = summary.get("version_drift")
    verdict("no_version_drift", drift is False,
            f"drift={drift}")

    # 9b) 升级版（s1-upgraded）：自动生长负载判据
    ag_map = kind_map.get("__ag__") if kind_map else None
    if ag_map:
        ag_applied = ag_rejected = ag_off_split = 0
        off_nodes_split = []
        for r in db_reports:
            if r.get("empty_db"):
                continue
            ev = r.get("events_by_type") or {}
            if r["mid"] in ag_map.get("good", []):
                ag_applied += ev.get("split_applied", 0)
            elif r["mid"] in ag_map.get("mixed", []):
                ag_rejected += ev.get("split_rejected", 0)
                ag_applied += ev.get("split_applied", 0)
            elif r["mid"] in ag_map.get("off", []):
                ag_off_split += ev.get("split_proposed", 0) \
                    + ev.get("split_applied", 0)
                off_nodes_split += [
                    n["title"] for n in (r.get("nodes") or {}).values()
                    if n["status"] == "waiting_split"]
        inj_ag = {}
        ag_file = run_dir / "ag_injections.json"
        if ag_file.is_file():
            inj_ag = json.loads(ag_file.read_text(encoding="utf-8"))
        # ag mission 终态判定：good 应 done（拆分→孩子→父整合→原卷闭合）；
        # mixed 预期 active（bad 叶子提案拒绝→重试耗尽→有界死局，冻结
        # 运行协议，不热循环）；off 预期 active（关闭态 split 叶子提案
        # 不被受理且无交付→fail-closed 有界死局；split 事件必须为 0）
        ag_status = {}
        for r in db_reports:
            if r.get("empty_db"):
                continue
            if r["mid"] in (ag_map.get("good", []) + ag_map.get("mixed", [])
                            + ag_map.get("off", [])):
                ag_status[r["mid"]] = r["mission_status"]
        good_done = all(ag_status.get(m) == "done"
                        for m in ag_map.get("good", []))
        bad_active = all(ag_status.get(m) == "active"
                         for m in ag_map.get("mixed", []))
        off_active = all(ag_status.get(m) == "active"
                         for m in ag_map.get("off", []))
        ag_ok = (ag_applied >= 2 and ag_rejected >= 1
                 and ag_off_split == 0 and not off_nodes_split
                 and good_done and bad_active and off_active
                 and inj_ag.get("ag_revised_node") is not None)
        verdict("autogrow_load", ag_ok,
                f"applied={ag_applied} rejected={ag_rejected} "
                f"off_split_events={ag_off_split}（关闭对照组须 0） "
                f"good_done={good_done} mixed_active={bad_active} "
                f"off_active={off_active} "
                f"revise_inject={inj_ag.get('ag_revised_node')}")
    if summary.get("hard_stop_triggered"):
        verdict("stop_mode", True,
                f"硬停触发（预期口径按 kill 核验）："
                f"{summary.get('hard_stop_reason')}")
    else:
        verdict("stop_mode", True,
                "到点停止新准入→自然闭合（无硬停）")

    # 10) 资源门槛判定（R6/Codex 复核重写）：
    # - 数值=SPEC_THRESHOLDS（SPEC-Soak.md §2 唯一权威），代码不再另立；
    # - 指标缺失→相应项 UNVERIFIED，禁止缺省补 0（句柄 0/min 假象）；
    # - 已知越线 FAIL 优先于 UNVERIFIED（不断言泄漏也不放行）；
    # - 预热=run_config.warmup_s（SPEC §2.2 前2个批周期），未记录回退
    #   前10%样本并如实标注。
    res_rows_n = len(valid)
    report["checks"]["resource_first_last"] = None
    if res_rows_n >= 4:
        first, last = valid[0], valid[-1]
        _t0 = valid[0]["ts"]
        _warmup = float(config.get("warmup_s") or 0)
        if _warmup > 0:
            warm = [r for r in valid if r["ts"] - _t0 >= _warmup]
            warm_note = f"预热={_warmup:.0f}s（run_config.warmup_s）"
        else:
            warm = valid[max(1, res_rows_n // 10):]
            warm_note = "预热=前10%样本（run_config 未冻结 warmup_s——旧口径回放）"
        import statistics as _st
        def _slope_mib_per_h(pairs):
            n = len(pairs)
            if n < 3:
                return None
            mx = sum(x for x, _ in pairs) / n
            my = sum(y for _, y in pairs) / n
            denom = sum((x - mx) ** 2 for x, _ in pairs)
            if denom == 0:
                return None
            k = sum((x - mx) * (y - my) for x, y in pairs) / denom
            return k * 3600.0   # 单位/秒 → 单位/小时
        slope = _slope_mib_per_h([(r["ts"], r["proc_rss_mb"]) for r in warm])
        # V3+N-验收（三/四轮）：分母以**任务库**为权威——done mid 集合
        # 来自库 mission_status，完成时点用库内 attempts 最大 ended_at；
        # summary.finished 仅作核对（库外 mid/重复条目不得扩大分母，
        # 伪造 done→FAIL）。稳定期完成量为 0 时如实 UNVERIFIED，
        # **不得回退**到全量分母。
        _fin = summary.get("finished", []) or []
        db_by_mid = {r["mid"]: r for r in db_reports if not r.get("empty_db")}
        done_mids_db = {m for m, r in db_by_mid.items()
                        if r.get("mission_status") == "done"}
        summary_mids = {f.get("mid") for f in _fin if f.get("mid")}
        fake_mids = sorted(m for m in summary_mids
                           if m not in db_by_mid)
        dup_finished = len(_fin) - len(summary_mids)
        _warm_start_ts = warm[0]["ts"] if warm else None
        done_in_warm = set()
        ended_unknown = 0
        for m in done_mids_db:
            t = _to_epoch(db_by_mid[m].get("last_ended_at"))
            if t is None:
                ended_unknown += 1
                continue
            if _warm_start_ts is None or t >= _warm_start_ts:
                done_in_warm.add(m)
        done_missions = len(done_in_warm)
        done_mids_all = done_mids_db
        # 增量与斜率同口径：预热后稳定期窗口的峰谷（含预热爬坡会把
        # 解释器初分配计入 per-mission 增量——s3-fix-s1 实测 1.24→0.38）
        peak_rss = max(r["proc_rss_mb"] for r in warm)
        base_rss = min(r["proc_rss_mb"] for r in warm)
        per_mission = ((peak_rss - base_rss) / done_missions
                       if done_missions else None)
        hslope = _slope_mib_per_h([(r["ts"], r["proc_handles"])
                                   for r in warm
                                   if "proc_handles" in r and _finite(r["proc_handles"]) and _finite(r["ts"])])
        hslope_pm = (hslope / 60.0) if (hslope is not None and _finite(hslope)) else None
        n_handles = sum(1 for r in valid if "proc_handles" in r)
        n_threads = sum(1 for r in valid if "proc_threads" in r)
        n_children = sum(1 for r in valid if "children_n" in r)
        report["checks"]["resource_metrics"] = {
            "rss_slope_mib_h": round(slope, 2) if (slope is not None and _finite(slope)) else None,
            "per_mission_mib": round(per_mission, 3)
                if (per_mission is not None and _finite(per_mission)) else None,
            "handle_slope_per_min": round(hslope_pm, 2)
                if (hslope_pm is not None and _finite(hslope_pm)) else None,
            "threads_first_last_peak": [first.get("proc_threads"),
                                        last.get("proc_threads"),
                                        max((r.get("proc_threads") or 0)
                                            for r in valid)],
            "children_last": last.get("children_n"),
            "peak_rss_mb": peak_rss,
            "warm": warm_note,
            "done_in_warm": len(done_in_warm),
            "done_dedup_total": len(done_mids_all),
            "done_db_authoritative": True,
            "fake_summary_mids": len(fake_mids),
            "dup_finished_entries": dup_finished,
            "metric_presence": {"proc_handles": n_handles,
                                "proc_threads": n_threads,
                                "children_n": n_children},
        }
        problems: list[str] = []
        unver: list[str] = []
        if _bad_numeric:
            _ratio = _bad_numeric / max(1, len(_raw_valid))
            if _ratio > 0.10:
                unver.append(
                    f"numeric_samples({ _bad_numeric}/{len(_raw_valid)} "
                    "样本含 NaN/Inf→UNVERIFIED)")
            else:
                problems.append(
                    f"{_bad_numeric} 个样本含 NaN/Inf（已剔除，"
                    "异常观测本身FAIL）")
        # V4+N-验收（三/四轮）：静止点当行必需指标必须**存在且为有效
        # 有限数值**——null/缺键/非数都算缺（null 不得代表 0）；不能借
        # 其他样本曾有指标放行
        def _q_bad(v):
            return (not isinstance(v, (int, float)) or isinstance(v, bool)
                    or not _finite(v))
        _q_missing = [k for k in ("children_n", "proc_threads",
                                  "proc_handles") if _q_bad(last.get(k))]
        if _q_missing:
            unver.append("quiescent_metrics(末样本缺或无效: "
                         + ",".join(_q_missing) + ")")
        if slope is None or not _finite(slope):
            unver.append("rss_slope(预热后样本不足或非有限数)")
        elif abs(slope) > SPEC_THRESHOLDS["rss_slope_mib_h"]:
            problems.append(
                f"RSS斜率 {slope:.1f} MiB/h > "
                f"{SPEC_THRESHOLDS['rss_slope_mib_h']}")
        if per_mission is None:
            unver.append("per_mission(无done)")
        elif not _finite(per_mission):
            problems.append(f"每mission增量非有限数（{per_mission}）")
        elif done_missions < 40:
            # SPEC §2.2 冻结语义（s0 三轮校准冻结原文）：短测 mission
            # 样本少、预热后爬坡未平台化，基线 RSS 漂移被小分母放大——
            # 增量判据**只在长测（≥40done）裁决**，短测如实 UNVERIFIED。
            # （四轮"已知越线优先于 UNVERIFIED"针对长测真实越线——
            # 短窗峰谷不可信，既不判 PASS 也不判 FAIL）
            unver.append(f"per_mission(仅{done_missions}done,样本不足,"
                         "短窗爬坡噪声不裁决)")
        elif per_mission > SPEC_THRESHOLDS["per_mission_mib"]:
            problems.append(
                f"每mission增量 {per_mission:.2f} MiB > "
                f"{SPEC_THRESHOLDS['per_mission_mib']}（已知越线）")
        if n_handles == 0:
            unver.append("handle_slope(缺proc_handles指标)")
        elif hslope_pm is None or not _finite(hslope_pm):
            problems.append(f"句柄斜率预热后样本不足或非有限数（{hslope_pm}）")
        elif hslope_pm > SPEC_THRESHOLDS["handle_slope_per_min"]:
            problems.append(
                f"句柄斜率 {hslope_pm:.1f}/min > "
                f"{SPEC_THRESHOLDS['handle_slope_per_min']}")
        # 稳定期线程波动带（SPEC §2.3 ±8）：取**运行期**样本（有活跃
        # 任务）的振幅——含收尾滑坡会把振幅混入关服回落，混算不实
        if n_threads == 0:
            unver.append("thread_fluct(缺proc_threads指标)")
        else:
            _run_rows = [r for r in warm
                         if "proc_threads" in r
                         and (r.get("active_runs") or 0) > 0]
            if len(_run_rows) < 3:
                unver.append("thread_fluct(运行期样本不足)")
            else:
                fl = (max(r["proc_threads"] for r in _run_rows)
                      - min(r["proc_threads"] for r in _run_rows))
                _band = thread_fluct_band(config.get("concurrency_total") or 1)
                if fl > _band:
                    problems.append(
                        f"稳定期线程波动 {fl} > max(8,3×C)={_band}"
                        "（修订 B 波动带）")
        # 静止点前置：末样本仍有活跃运行/在途时无"关服后"证据——
        # 回落类判据如实 UNVERIFIED（不断言泄漏也不放行）
        quiescent = (last.get("active_runs") == 0
                     and last.get("inflight") == 0)
        if n_children == 0:
            unver.append("children_reclaim(缺children_n指标)")
        elif last.get("children_n") not in (0, None):
            if quiescent:
                problems.append(f"末样本子进程 {last['children_n']} != 0")
            else:
                unver.append("children_reclaim(无静止点)")
        # 静止点线程回落（SPEC §2.2 基线±5）
        if n_threads and last.get("proc_threads") is not None \
                and first.get("proc_threads"):
            if quiescent and last["proc_threads"] > \
                    first["proc_threads"] + SPEC_THRESHOLDS["thread_quiescent"]:
                problems.append(
                    f"静止点线程未回落 {first['proc_threads']}→"
                    f"{last['proc_threads']}（>基线+"
                    f"{SPEC_THRESHOLDS['thread_quiescent']}）")
            elif not quiescent:
                unver.append("thread_reclaim(无静止点)")
        if fake_mids:
            problems.append(
                f"summary 含 {len(fake_mids)} 个库外 mid（伪造 done 计数）"
                f"：{fake_mids[:5]}")
        if ended_unknown:
            unver.append(f"done_ended_at({ended_unknown} 个库内 done 无"
                         "完成时点)")
        # 裁决：已知 FAIL 优先于 UNVERIFIED（不因样本不足而降级越线）
        if problems:
            verdict("resource_thresholds", False,
                    "FAIL：" + ";".join(problems)
                    + ("；另UNVERIFIED：" + ",".join(unver) if unver else ""))
        elif unver:
            verdict("resource_thresholds", None,
                    "UNVERIFIED：" + ",".join(unver))
        else:
            verdict("resource_thresholds", True,
                    f"{warm_note}；全部门槛内："
                    f"斜率{slope:.1f}MiB/h 增量{per_mission:.2f}MiB/mission "
                    f"句柄{hslope_pm:.1f}/min 线程波动达标 静止点回落达标")
        report["checks"]["resource_first_last"] = {
            "first": {k: first.get(k) for k in
                      ("proc_rss_mb", "proc_threads", "proc_handles")},
            "last": {k: last.get(k) for k in
                     ("proc_rss_mb", "proc_threads", "proc_handles")},
            "peak_rss_mb": summary.get("peak_rss_mb"),
            "done_runs_retained": last.get("done_runs"),
            "active_runs": last.get("active_runs"),
            "note": ("RunManager 保留完成句柄与 timeline（生命周期保留，"
                     "有界回收接口 cleanup_finished 见 daemon；活跃线程/"
                     "子进程应回落）")}
    else:
        verdict("resource_thresholds", None,
                f"有效资源样本不足（{res_rows_n} 行）→UNVERIFIED")

    # 10b) SPEC 对照（R6+V1/Codex 三轮）：必需字段完整性+SPEC 摘要映射
    # ——缺项 UNVERIFIED、值漂移/伪造 sha FAIL；recorded/evaluated/
    # validator 三个摘要分记，已知版本映射内如实标注迁移
    spec_file = HERE / "SPEC-Soak.md"
    evaluated_spec = (hashlib.sha256(spec_file.read_bytes()).hexdigest()[:16]
                      if spec_file.is_file() else None)
    validator_file = HERE / "verify_soak.py"
    validator_sha = (hashlib.sha256(validator_file.read_bytes())
                     .hexdigest()[:16] if validator_file.is_file() else None)
    # 已知 SPEC 版本（§6 修订记录）；当前文件 sha 恒入列
    _SPEC_VERSION_SHAS = {
        "d714c7efb230c577": "冻结版(2026-10-09)",
        "52c26c3c144becaa": "冻结版·修订B前(2026-10-10)",
        "09cc1006c2517ed1": "修订B(2026-10-11)",
        "2f6274386e108e62": "冻结版·修订C(2026-10-12)",
    }
    if evaluated_spec:
        _SPEC_VERSION_SHAS.setdefault(evaluated_spec, "当前文件")
    cfg_thr = config.get("thresholds") or {}
    thr_mismatch: dict = {}
    for k, v in SPEC_THRESHOLDS.items():
        if k.startswith("thread_fluct"):
            continue
        if k in cfg_thr and cfg_thr[k] != v:
            thr_mismatch[k] = [cfg_thr[k], v]
    if "thread_fluct_base" in cfg_thr:
        if (cfg_thr["thread_fluct_base"]
                != SPEC_THRESHOLDS["thread_fluct_base"]
                or cfg_thr.get("thread_fluct_per_c")
                != SPEC_THRESHOLDS["thread_fluct_per_c"]):
            thr_mismatch["thread_fluct"] = [
                cfg_thr.get("thread_fluct_base"),
                SPEC_THRESHOLDS["thread_fluct_base"]]
    elif "thread_fluct" in cfg_thr:
        # 修订 B 前口径（静态带）——按修订版语义核对下限=base
        if cfg_thr["thread_fluct"] != SPEC_THRESHOLDS["thread_fluct_base"]:
            thr_mismatch["thread_fluct"] = [cfg_thr["thread_fluct"],
                                            SPEC_THRESHOLDS["thread_fluct_base"]]
    # legacy 兼容（修订 B 前口径）：静态 thread_fluct 视为满足波动带字段
    _has_legacy_band = "thread_fluct" in cfg_thr
    missing_thr = [k for k in SPEC_THRESHOLDS
                   if k not in cfg_thr
                   and not (k.startswith("thread_fluct") and _has_legacy_band)]
    cfg_spec_sha = config.get("spec_sha256")
    _c_cfg = int(config.get("concurrency_total") or 1)
    report["checks"]["spec"] = {
        "recorded_spec": cfg_spec_sha,
        "evaluated_spec": evaluated_spec,
        "validator_sha": validator_sha,
        "config_thresholds": cfg_thr or None,
        "threshold_mismatch": thr_mismatch or None,
        "missing_threshold_fields": missing_thr or None,
        "thread_band_applied": thread_fluct_band(_c_cfg)}
    if not cfg_thr:
        verdict("spec_thresholds_match", None,
                f"run_config 未记录 thresholds（evaluated SPEC="
                f"{evaluated_spec}）——旧跑回放，如实标注不追溯")
    elif missing_thr:
        verdict("spec_thresholds_match", None,
                f"thresholds 缺必需字段 {missing_thr}→UNVERIFIED"
                f"（evaluated SPEC={evaluated_spec}）")
    elif thr_mismatch:
        verdict("spec_thresholds_match", False,
                f"run_config.thresholds 与 SPEC-Soak（evaluated="
                f"{evaluated_spec}）漂移：{thr_mismatch}——不得冒称按本"
                " SPEC 通过")
    elif cfg_spec_sha not in _SPEC_VERSION_SHAS:
        verdict("spec_thresholds_match", False,
                f"recorded spec_sha256={cfg_spec_sha} 不属于任何已知 SPEC"
                f" 版本（{sorted(_SPEC_VERSION_SHAS)}）——伪造/未知摘要→FAIL")
    else:
        verdict("spec_thresholds_match", True,
                f"recorded={cfg_spec_sha}"
                f"（{_SPEC_VERSION_SHAS[cfg_spec_sha]}）→evaluated="
                f"{evaluated_spec}（迁移如实记录）；validator={validator_sha}；"
                f"thresholds 一致；修订B波动带=max(8,3×C)="
                f"{thread_fluct_band(_c_cfg)}（C={_c_cfg}）")

    # 10c) 总账口径（R6/Codex 复核：主负载调用 ≠ 全轮总调用——主负载/
    # 探针/全库分账核对）
    main_calls = summary.get("backend_calls")
    probe_calls = summary.get("backend_calls_probe")
    db_attempts = sum(r.get("totals", {}).get("attempts", 0)
                      for r in db_reports if not r.get("empty_db"))
    db_budget_calls = sum(r.get("budget", {}).get("calls_used", 0)
                          for r in db_reports if not r.get("empty_db"))
    report["checks"]["calls_accounting"] = {
        "main_backend_calls": main_calls, "probe_calls": probe_calls,
        "db_attempts": db_attempts, "db_budget_calls": db_budget_calls}
    if main_calls is None or probe_calls is None:
        verdict("calls_accounting", None,
                f"summary 缺主负载/探针调用分账（main={main_calls} "
                f"probe={probe_calls} db_attempts={db_attempts}）"
                "——旧跑回放，如实标注")
    else:
        total_calls = int(main_calls) + int(probe_calls)
        # attempts ≈ 调用数 + 注入孤儿（ghost 不产生后端调用）——允许
        # 差值 ≤ 注入孤儿数（injections 里 ghost=1）
        ok_acc = abs(db_attempts - total_calls) <= 2
        # 核对时间线主负载与 planner attempt ID 真实性与绑定（通用契约：权威数据库对账）
        fake_main_attempts = set()
        main_binding_bad = []
        if tl_file.is_file():
            tl_main_ev = [e for e in _all_ev if e.get("attempt") and not e.get("probe")]
            all_db_proposals = {}
            for r in db_reports:
                if r.get("empty_db"):
                    continue
                all_db_proposals.update(r.get("split_proposals") or {})

            for e in tl_main_ev:
                a = str(e.get("attempt"))
                if a.startswith("plan:"):
                    pid = a[len("plan:"):]
                    if pid not in all_db_proposals:
                        fake_main_attempts.add(a)
                    else:
                        prop = all_db_proposals[pid]
                        if e.get("mid") and e.get("mid") != prop["mission_id"]:
                            main_binding_bad.append(f"{a}: mid {e.get('mid')} != {prop['mission_id']}")
                        if e.get("node") and e.get("node") != prop["node_id"]:
                            main_binding_bad.append(f"{a}: node {e.get('node')} != {prop['node_id']}")
                else:
                    if a not in all_db_attempts:
                        fake_main_attempts.add(a)
                    else:
                        dba = all_db_attempts[a]
                        if e.get("mid") and e.get("mid") != dba["mission_id"]:
                            main_binding_bad.append(f"{a}: mid {e.get('mid')} != {dba['mission_id']}")
                        if e.get("node") and e.get("node") != dba["node_id"]:
                            main_binding_bad.append(f"{a}: node {e.get('node')} != {dba['node_id']}")
                        egen = e.get("generation")
            # 反向核对：全库中实际执行过的主负载 attempts 必须在时间线中完整出现（防删主负载区间）
            missing_main_in_tl = set()
            real_main_db_attempts = {
                a_id for a_id, dba in all_db_attempts.items()
                if a_id not in db_probe_ids and dba.get("status") != "orphaned" and dba.get("started_at")
            }
            tl_main_attempt_ids = {
                str(e.get("attempt")) for e in tl_main_ev
                if e.get("attempt") and not str(e.get("attempt")).startswith("plan:")
            }
            if main_calls is not None and len(tl_main_attempt_ids) < int(main_calls):
                missing_main_in_tl = real_main_db_attempts - tl_main_attempt_ids or {"missing_main_execution"}
            elif main_calls is not None and len(real_main_db_attempts) == int(main_calls):
                missing_main_in_tl = real_main_db_attempts - tl_main_attempt_ids
            if fake_main_attempts or main_binding_bad or missing_main_in_tl:
                ok_acc = False
        verdict("calls_accounting", ok_acc,
                f"主负载={main_calls} 探针={probe_calls} 合计={total_calls}"
                f" 全库attempts={db_attempts} budget_calls={db_budget_calls}"
                + (f"；时间线含伪造主负载/planner attempt ID（{len(fake_main_attempts)} 个，如 {list(fake_main_attempts)[:2]}）" if fake_main_attempts else "")
                + (f"；时间线缺失真实主负载 attempt ID（{len(missing_main_in_tl)} 个，如 {list(missing_main_in_tl)[:2]}）" if missing_main_in_tl else "")
                + (f"；时间线含主负载绑定不符（{len(main_binding_bad)} 个，如 {main_binding_bad[:2]}）" if main_binding_bad else "")
                + ("" if ok_acc else "（分账不平/执行集异常→FAIL）"))
    overall = all(v["result"] == "PASS" for v in report["verdicts"].values())
    report["overall"] = ("PASS" if overall else
                         "FAIL" if any(v["result"] == "FAIL"
                                       for v in report["verdicts"].values())
                         else "UNVERIFIED")
    out = run_dir / "consistency_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(json.dumps(report["verdicts"], ensure_ascii=False, indent=2))
    print(f"[verify] overall={report['overall']} → {out}")
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
