# -*- coding: utf-8 -*-
"""act3_revise.py v2 — 幕三：v1→v2 修订增量重验（无预算上限）。

修正（对照 Codex 二轮）：修订目标=现役资料节点（资料冻结v2=N01b）；
根 bundle 用最新 valid 快照（按整数版本）；N07 执行次数增量=0 断言；
级联失效/旧证据作废事件断言；结束后独立 judge 全 20 例；完成判定直读任务库。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

PKG = Path(__file__).resolve().parent
ROOT = PKG.parents[1]
FIX = PKG / "fixtures"
JUDGE = PKG / "judge.py"
EVID = PKG.parent / "多agent协同验收运行证据"
PY = sys.executable
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(ROOT))
from run_act1 import McpClient  # noqa: E402


def db_repo(mid):
    from dag_agent.repository import Repository
    return Repository(ROOT / "data" / "missions" / mid / "mission.db")


def main() -> int:
    src = json.loads((EVID / ("act1c_result.json" if
                    (EVID / "act1c_result.json").exists() else
                    "act1b_result.json")).read_text(encoding="utf-8"))
    mid = src["mission"]
    t2i = src["title2id_after"]
    n01b, n07, root = t2i["资料冻结v2"], t2i["使用说明"], t2i.get("ROOT") or \
        next(t for t, i in t2i.items() if t.startswith("订单-退款"))
    root_id = src.get("final_root") or t2i.get(root)
    cli = McpClient()
    ev: dict = {"act": "act3_v2"}
    try:
        repo = db_repo(mid)
        n07_before = repo._conn.execute(
            "SELECT COUNT(*) c FROM attempts WHERE node_id=?", (n07,)
        ).fetchone()["c"]
        root_before = repo.get_node(root_id).accepted_artifact_version
        repo.close()
        # 数据指针 v1→v2 + 修订现役资料节点
        (PKG / "checks" / "data_ref.txt").write_text("v2", encoding="utf-8")
        r = cli.call("dag3_revise", {"mid": mid, "node_id": n01b,
                                     "reason": "SPEC 幕三：数据口径 v1→v2"})
        ev["revise"] = r
        assert r.get("ok"), r
        repo = db_repo(mid)
        ev["cascade"] = {
            "mission_reopened": repo.get_mission(mid).status == "active",
            "invalid_events": [e.node_id for e in repo.events_of(mid)
                               if e.type == "evidence_invalidated"],
            "reopened": repo.get_mission(mid).status}
        repo.close()
        run = cli.call("dag3_run_start", {
            "mid": mid, "max_concurrency": 2, "max_retries": 2,
            "max_calls": None, "max_cost_usd": None, "real": True,
            "node_cap": 20, "timeout": 2400})
        ev["run_start"] = run
        assert run.get("ok"), run
        t0 = time.time()
        while time.time() - t0 < 2400:
            if not cli.call("dag3_run_status", {"mid": mid}).get("running"):
                break
            time.sleep(8)
        repo = db_repo(mid)
        ms = repo.get_mission(mid)
        fin = {n.title: n.status for n in repo.list_nodes(mid)}
        n07_after = repo._conn.execute(
            "SELECT COUNT(*) c FROM attempts WHERE node_id=?", (n07,)
        ).fetchone()["c"]
        budget = json.loads(ms.budget_json or "{}")
        # 最新 valid 根快照
        snaps = [s for s in repo.snapshots_of(root_id) if s.valid]
        snap = max(snaps, key=lambda s: s.artifact_version) if snaps else None
        root_dir = None
        if snap:
            root_dir = (ROOT / "data" / "missions" / mid / "accepted" /
                        root_id / f"v{snap.artifact_version}")
        repo.close()
        ev["final"] = {"mission": ms.status, "closure": ms.closure_basis,
                       "nodes": fin, "budget": budget,
                       "root_v": snap.artifact_version if snap else None,
                       "root_before_v": root_before,
                       "n07_attempts_delta": n07_after - n07_before}
        # v2 净额可观测断言（SPEC：north/CNY 26000→27000）
        v2 = {"ok": False}
        if root_dir and (root_dir / "main.py").is_file():
            import os
            import tempfile
            with tempfile.TemporaryDirectory() as td:
                out = Path(td) / "s.json"
                r2 = subprocess.run(
                    [PY, str(root_dir / "main.py"),
                     "--orders", str(FIX / "v2" / "orders.csv"),
                     "--refunds", str(FIX / "v2" / "refunds.csv"),
                     "--out", str(out)], capture_output=True, text=True,
                    timeout=180, cwd=str(root_dir),
                    env={**os.environ, "PYTHONPATH": str(root_dir)})
                if out.is_file():
                    s = json.loads(out.read_text(encoding="utf-8"))
                    north = [t for t in s.get("totals", [])
                             if t.get("region") == "north"
                             and t.get("currency") == "CNY"]
                    v2 = {"ok": r2.returncode == 0 and bool(north) and
                          north[0].get("net_cents") == 27000,
                          "net": north[0].get("net_cents") if north else None}
        ev["v2_check"] = v2
        # 独立 judge 全 20 例（对最新根快照）
        judge = {"ok": False}
        if root_dir:
            import tempfile
            with tempfile.TemporaryDirectory() as td:
                jr = subprocess.run(
                    [PY, str(JUDGE), "--bundle", str(root_dir),
                     "--evidence", str(Path(td) / "e.json")],
                    capture_output=True, text=True, timeout=900,
                    cwd=str(JUDGE.parent))
            judge = {"ok": jr.returncode == 0,
                     "tail": (jr.stdout or "")[-300:]}
        ev["judge20"] = judge
        ev["board"] = cli.call("dag3_board", {"mid": mid})
        checks = [ms.status == "done", ms.closure_basis == "machine",
                  v2.get("ok"), judge.get("ok"), n07_after == n07_before,
                  snap is not None and snap.artifact_version > (root_before
                                                                or 0)]
        ev["result"] = "ACT3_OK" if all(checks) else f"ACT3_FAILED:{checks}"
        (EVID / "act3_result.json").write_text(
            json.dumps(ev, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        (EVID / "act3_mcp_log.json").write_text(
            json.dumps(cli.log, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        print(json.dumps({"result": ev["result"], "final": ev["final"],
                          "v2": v2, "judge20_ok": judge.get("ok")},
                         ensure_ascii=False))
        return 0 if ev["result"] == "ACT3_OK" else 1
    finally:
        cli.close()


if __name__ == "__main__":
    sys.exit(main())
