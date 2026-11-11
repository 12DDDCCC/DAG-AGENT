# -*- coding: utf-8 -*-
"""act1b_repair.py v2 — 幂等续跑：复用已建节点，不重建同名；无预算上限。

幂等规则：从任务库读现状——N01/N02/N03 已 cancelled 则跳过取消；
N01b/N02b/N03b 已存在则跳过创建（同标题查重会拒）。预算：None=不设上限
（用户 2026-10-03 授权）。完成判定直读任务库（不信 run_status）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

PKG = Path(__file__).resolve().parent
ROOT = PKG.parents[1]
EVID = PKG.parent / "多agent协同验收运行证据"
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(ROOT))
from run_act1 import CHK, McpClient  # noqa: E402
from act1b_briefs import N01B, N02B, N03B  # noqa: E402


def mission_nodes(cli, mid) -> dict:
    st = cli.call("dag3_status", {"mid": mid})
    nodes = st.get("nodes") or {}
    return {n.get("title"): n for n in (nodes.values() if
            isinstance(nodes, dict) else nodes)}


def main() -> int:
    a1 = json.loads((EVID / "act1_result.json").read_text(encoding="utf-8"))
    mid = a1["mission"]
    cli = McpClient()
    ev: dict = {"act": "act1b_v2_idempotent", "mission": mid}
    try:
        st = cli.call("dag3_status", {"mid": mid})
        # dag3_status 返回节点清单；兼容两种形态
        raw = st.get("nodes") or st.get("detail") or {}
        title2id: dict[str, str] = {}
        if isinstance(raw, dict):
            items = raw.values() if all(
                isinstance(v, dict) for v in raw.values()) else []
            for it in items:
                if isinstance(it, dict) and it.get("title"):
                    title2id[it["title"]] = it.get("id") or it.get("node")
        if not title2id:  # 兜底：直接查库
            from dag_agent.repository import Repository
            repo = Repository(ROOT / "data" / "missions" / mid / "mission.db")
            for n in repo.list_nodes(mid):
                title2id[n.title] = n.id
            repo.close()
        ev["title2id"] = title2id

        def cancel_if_active(key, title):
            nid = title2id.get(title)
            if not nid:
                return None
            cur = cli.call("dag3_status", {"mid": mid})
            del cur
            from dag_agent.repository import Repository
            repo = Repository(ROOT / "data" / "missions" / mid / "mission.db")
            status = repo.get_node(nid).status
            repo.close()
            if status == "cancelled":
                return {"skipped": "already_cancelled"}
            r = cli.call("dag3_cancel", {"mid": mid, "node_id": nid,
                                         "reason": f"幕一分诊重建{key}"})
            assert r.get("ok"), (key, r)
            return r

        ev["cancel_N01"] = cancel_if_active("N01", "资料冻结")
        ev["cancel_N02"] = cancel_if_active("N02", "CSV导入")
        ev["cancel_N03"] = cancel_if_active("N03", "业务校验")

        def add_if_absent(key, title, stmt, parent_title, deps_titles, check,
                          arts):
            if title in title2id:
                ev[f"add_{key}"] = {"skipped": "already_exists",
                                    "node": title2id[title]}
                title2id[title] = title2id[title]
                return
            rr = cli.call("dag3_add_node", {
                "mid": mid, "title": title, "statement": stmt,
                "parent": title2id[parent_title],
                "deps": [title2id[t] for t in deps_titles if t in title2id],
                "artifacts": list(arts), "check_cmd": check})
            assert rr.get("ok"), (key, rr)
            title2id[title] = rr["node"]
            ev[f"add_{key}"] = rr

        add_if_absent("N01b", "资料冻结v2", N01B, "输入整合", [], CHK("n01"),
                      ("data/orders.csv", "data/refunds.csv"))
        add_if_absent("N02b", "CSV导入v2", N02B, "输入整合", [], CHK("loader"),
                      ("loader.py",))
        add_if_absent("N03b", "业务校验v2", N03B, "输入整合", ["CSV导入v2"],
                      CHK("validation"), ("validation.py",))
        ev["title2id_after"] = title2id

        run = cli.call("dag3_run_start", {
            "mid": mid, "max_concurrency": 2, "max_retries": 2,
            "max_calls": None, "max_cost_usd": None, "real": True,
            "node_cap": 20, "timeout": 2400})
        ev["run_start"] = run
        assert run.get("ok"), run
        t0 = time.time()
        while time.time() - t0 < 2400:
            stx = cli.call("dag3_run_status", {"mid": mid})
            if not stx.get("running"):
                break
            time.sleep(8)
        # 完成判定直读任务库
        from dag_agent.repository import Repository
        repo = Repository(ROOT / "data" / "missions" / mid / "mission.db")
        ms = repo.get_mission(mid)
        fin = {n.title: n.status for n in repo.list_nodes(mid)}
        budget = json.loads(ms.budget_json or "{}")
        repo.close()
        ev["final"] = {"mission": ms.status, "closure": ms.closure_basis,
                       "nodes": fin, "budget": budget}
        ev["board"] = cli.call("dag3_board", {"mid": mid})
        done = ms.status == "done"
        (EVID / "act1b_result.json").write_text(
            json.dumps(ev, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        (EVID / "act1b_mcp_log.json").write_text(
            json.dumps(cli.log, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        print(json.dumps({"mission_done": done, "final": ev["final"]["nodes"],
                          "budget": budget}, ensure_ascii=False))
        return 0 if done else 1
    finally:
        cli.close()


if __name__ == "__main__":
    sys.exit(main())
