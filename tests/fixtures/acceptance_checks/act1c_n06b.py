# -*- coding: utf-8 -*-
"""act1c — N06 分诊重建（自足示例任务书）+ 续跑到闭合（无预算上限）。"""
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

N06B = """任务：交付 render.py 与 render.md。
render.py 必须逐字包含：
def render(summary):
    ...
输入 summary 是这样的 dict（示例，也是验收用的输入）：
{"schema_version": 1, "cancelled_orders": 1,
 "totals": [{"region": "north", "currency": "CNY", "paid_orders": 1,
             "gross_cents": 100, "refund_cents": 0, "net_cents": 100}]}
要求：返回中文字符串，totals 里每个分组必须单独成行，且每行包含该组的
region（如 north）、currency（如 CNY）与数字（gross/refund/net）。
以示例为输入时，输出必须包含 "north"、"CNY"、"100"。
render.md 写一页示例报告（用上面的示例输入跑 render 的效果）。
只用标准库；交付 render.py 与 render.md 两个文件。
自测：python -c "import render, json; t=render.render(json.load(open('sample.json'))); assert 'north' in t and '100' in t"
（sample.json 自己按示例临时造，不必交付）。"""


def db_repo(mid):
    from dag_agent.repository import Repository
    return Repository(ROOT / "data" / "missions" / mid / "mission.db")


def main() -> int:
    src = json.loads((EVID / "act1b_result.json").read_text(encoding="utf-8"))
    mid = src["mission"]
    t2i = src["title2id_after"]
    cli = McpClient()
    ev: dict = {"act": "act1c_n06b", "mission": mid}
    try:
        repo = db_repo(mid)
        n6 = [n for n in repo.list_nodes(mid) if n.title == "报告"][0]
        repo.close()
        r = cli.call("dag3_cancel", {"mid": mid, "node_id": n6.id,
                                     "reason": "幕一N06分诊：任务书缺输入示例"})
        ev["cancel_N06"] = r
        assert r.get("ok"), r
        t2i["报告"] = n6.id
        rr = cli.call("dag3_add_node", {
            "mid": mid, "title": "报告v2", "statement": N06B,
            "parent": t2i["交付整合"],
            "deps": [t2i["输入整合"]], "artifacts": ["render.md", "render.py"],
            "check_cmd": CHK("render")})
        assert rr.get("ok"), rr
        t2i["报告v2"] = rr["node"]
        ev["title2id_after"] = t2i
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
        budget = json.loads(ms.budget_json or "{}")
        root = [n for n in repo.list_nodes(mid)
                if n.parent_id is None][0]
        ev["final_root"] = root.id
        repo.close()
        ev["final"] = {"mission": ms.status, "closure": ms.closure_basis,
                       "nodes": fin, "budget": budget}
        ev["board"] = cli.call("dag3_board", {"mid": mid})
        done = ms.status == "done"
        (EVID / "act1c_result.json").write_text(
            json.dumps(ev, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        (EVID / "act1c_mcp_log.json").write_text(
            json.dumps(cli.log, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        print(json.dumps({"mission_done": done,
                          "final": {k: v for k, v in fin.items()
                                    if v != "cancelled"},
                          "budget": budget}, ensure_ascii=False))
        return 0 if done else 1
    finally:
        cli.close()


if __name__ == "__main__":
    sys.exit(main())
