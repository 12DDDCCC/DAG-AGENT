# -*- coding: utf-8 -*-
"""run_act1.py — 幕一：真实多 agent 协同构建（单一真实 MCP 会话驱动）。

SPEC：outputs/多agent协同验收项目/SPEC.md；拓扑与 act0_dryrun 一致
（干跑已零模型闭合：根=judge 20/20 双跑通过）。
执行链：建图（12 节点含根，N04A/B 同组 OR）→ dag3_run_start(real=true,
并发2, 预算冻结 20 调用/$15) → 运行中 status 轮询 + 真实干预（set_priority）
→ dag3_board → 证据 JSON。"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = Path(__file__).resolve().parent
CHECKS = PKG / "checks" / "mod_checks.py"
EVID = PKG.parent / "多agent协同验收运行证据"
PY = sys.executable


class McpClient:
    def __init__(self):
        self._err_path = Path(tempfile.gettempdir()) / \
            f"dag_mcp_stderr_{id(self)}.log"
        self._err_file = open(self._err_path, "w", encoding="utf-8",
                              errors="replace")
        self.proc = subprocess.Popen(
            [PY, str(ROOT / "mcp_server_v3.py")], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=self._err_file, text=True,
            encoding="utf-8", cwd=str(ROOT))
        self._id = 0
        self.log: list[dict] = []
        self.call("initialize", {"protocolVersion": "2024-11-05",
                                 "capabilities": {},
                                 "clientInfo": {"name": "acceptance",
                                                "version": "1.0"}})
        self.notify("notifications/initialized")

    def _send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def notify(self, method):
        self._send({"jsonrpc": "2.0", "method": method})

    def call(self, tool, args=None, timeout=1800):
        self._id += 1
        rid = self._id
        self._send({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                    "params": {"name": tool, "arguments": args or {}}})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("MCP 连接中断")
            msg = json.loads(line)
            if msg.get("id") != rid:
                continue
            if "error" in msg:
                return {"ok": False, "error": str(msg["error"])}
            text = msg["result"]["content"][0]["text"]
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = {"raw": text}
            self.log.append({"tool": tool, "args": args, "out": parsed})
            return parsed
        raise TimeoutError(f"{tool} 超时")

    def close(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def CHK(name: str) -> str:
    return f'python "{CHECKS}" {name}'


BUNDLE = ["loader.py", "validation.py", "reconcile.py", "render.py",
          "main.py", "README.md", "data/orders.csv", "data/refunds.csv",
          "INTEGRATION.md"]
IN_A = ("loader.py", "validation.py", "data/orders.csv", "data/refunds.csv",
        "INTEGRATION.md")
IN_C = IN_A + ("reconcile.py",)

API = """【冻结模块接口（必须一字不差遵守）】
loader.py:  class InputError(ValueError)（InputError(code) 或 InputError(code, message)，e.code 可取码）；
  def load_orders(path:str)->list[dict]；def load_refunds(path:str)->list[dict]
  —— dict 键=CSV 列名原样；容忍 UTF-8 BOM、合法引号字段（含逗号）、额外命名列；
  表头重复或缺必需列 → raise InputError("columns_missing")。
validation.py: from loader import InputError；
  def validate(orders:list[dict], refunds:list[dict]) -> tuple[list,list,int]
  —— 返回 (paid订单列表, 合法退款列表, cancelled订单数)；金额转 int（支持超 int64）。
reconcile.py: def reconcile(orders:list[dict], refunds:list[dict], cancelled_orders:int=0) -> dict
  —— 输入为 validate 输出；按 (region,currency) 汇总 paid_orders/gross_cents/
  refund_cents/net_cents（净额=gross-refund，整数运算禁浮点）；不同币种不合并；
  返回 {"schema_version":1,"cancelled_orders":N,"totals":[…]}，totals 按
  (currency,region) 升序，字段与类型精确匹配。
render.py: def render(summary:dict) -> str —— 每分组一行的可读中文报告。
main.py: CLI `python main.py --orders O.csv --refunds R.csv --out summary.json`
  —— 有效输入退出 0 产出 UTF-8 JSON；非法输入退出 2、不产出 summary.json、
  stdout 为 {"error":"错误码","message":"可选"}；必须真实调用上述三模块
  （严禁预存答案）。全部只用 Python 标准库。
【13 错误码】order_id_duplicate/refund_id_duplicate/unknown_order/
refund_on_cancelled/currency_mismatch/amount_invalid/refund_exceeds_order/
status_invalid/columns_missing/currency_invalid/order_id_empty/
refund_id_empty/region_empty。"""

RULES = ("\n【业务规则】orders 列=order_id,region,currency,amount_cents,status；"
         "refunds 列=refund_id,order_id,currency,amount_cents。空白-only=空值；"
         "currency 仅 CNY/USD；status 仅 paid/cancelled；amount=ASCII 非负十进制"
         "整数（允许前导零）；ID 非空且各自唯一；region 非空；退款须引用 paid "
         "订单且币种一致、同订单累计不超金额；cancelled 不计收入；不同币种不换算。")

N01_S = ("在交付目录创建 data/orders.csv 与 data/refunds.csv：内容与可信考卷"
         "包 checks/mod_checks.py 同目录 data_ref.txt 指针版本（读该文件得 v1 "
         "或 v2）对应的 fixtures/<版本>/ 同名文件逐字节一致（验收校验 sha256，"
         "禁止手写数据）。")

INTEG_S = ("整合交付：把 inputs/ 下全部来源文件合并复制到交付目录"
           "（inputs/<源>/<相对路径> → ./<相对路径>，同名冲突报错），"
           "另写 INTEGRATION.md 记录每件来源与用途；只复制不改动内容。")


def main() -> int:
    EVID.mkdir(parents=True, exist_ok=True)
    (PKG / "checks" / "data_ref.txt").write_text("v1", encoding="utf-8")
    cli = McpClient()
    ev: dict = {"act": "act1"}
    try:
        r = cli.call("dag3_create_mission", {
            "name": "订单-退款对账CLI-协同验收",
            "brief": "SPEC E1：多会话协同开发 bundle；根验收=独立judge 20例",
            "root_check": CHK("root"), "root_artifacts": BUNDLE,
            "anchor": "SPEC.md E1；judge 20 例为独立功能判卷"})
        assert r.get("ok"), r
        mid, root = r["mission_id"], r["root"]
        ev["mission"], ev["root"] = mid, root
        P: dict[str, str] = {}

        def add(key, title, stmt, parent, deps, check=None, arts=(),
                group=None, order=None, switch=None):
            args = {"mid": mid, "title": title, "statement": stmt,
                    "parent": parent, "deps": list(deps),
                    "artifacts": list(arts)}
            if check:
                args["check_cmd"] = check
            if group:
                args.update(group_id=group, group_order=order,
                            switch_after=switch)
            rr = cli.call("dag3_add_node", args)
            assert rr.get("ok"), (key, rr)
            P[key] = rr["node"]

        def seg(a, b):
            return API.split(a)[1].split(b)[0]

        add("P_input", "输入整合", INTEG_S, root, (), CHK("integration"), IN_A)
        add("N01", "资料冻结", N01_S, P["P_input"], (), CHK("n01"),
            ("data/orders.csv", "data/refunds.csv"))
        add("N02", "CSV导入",
            "实现 loader.py：\n" + seg("validation.py:", "@@") + RULES +
            "\n只交付 loader.py 一个文件；先用临时 CSV 自测（BOM/引号/缺列）。",
            P["P_input"], (), CHK("loader"), ("loader.py",))
        add("N03", "业务校验",
            "实现 validation.py：\n" + seg("loader.py:", "reconcile.py:") +
            RULES + "\n需要 loader.py（inputs/ 来源目录可直接 import）；只交付"
            " validation.py；至少自测三类非法码。",
            P["P_input"], [P["N02"]], CHK("validation"), ("validation.py",))
        add("P_compute", "计算整合", INTEG_S, root, [P["P_input"]],
            CHK("integration"), IN_C)
        recon_s = ("实现 reconcile.py：\n" +
                   seg("reconcile.py:", "render.py:") + RULES +
                   "\n需要 loader/validation（inputs/ 来源目录 import）；对 "
                   "fixtures/v1 数据自测通过后只交付 reconcile.py。")
        add("N04A", "汇总路线A", recon_s + "\n【路线A：普通字典聚合实现】",
            P["P_compute"], [P["P_input"]], CHK("recon"), ("reconcile.py",),
            group="g1", order=0, switch=1)
        add("N04B", "汇总路线B",
            recon_s + "\n【路线B：与 A 不同的数据结构/算法实现同接口】",
            P["P_compute"], [P["P_input"]], CHK("recon"), ("reconcile.py",),
            group="g1", order=1, switch=1)
        add("P_delivery", "交付整合", INTEG_S, root,
            [P["P_input"], P["P_compute"]], CHK("integration"), BUNDLE)
        add("N05", "CLI",
            "实现 main.py：\n" + seg("main.py:", "@@") + RULES +
            "\n三模块在 inputs/ 来源目录（把目录加进 sys.path 再 import）；"
            "只交付 main.py；用 fixtures/v1 与一个非法样例自测。",
            P["P_delivery"], [P["P_input"], P["P_compute"]], CHK("cli"),
            ("main.py",))
        add("N06", "报告",
            "实现 render.py 并附 render.md（一页示例报告）：\n" +
            seg("render.py:", "main.py:"),
            P["P_delivery"], [P["P_input"]], CHK("render"),
            ("render.md", "render.py"))
        add("N07", "使用说明",
            "写 README.md：bundle 用法（--orders/--refunds/--out）、输入列、"
            "13 个错误码全列表与失败行为（退出2+错误JSON）、一个可复制示例；"
            "依据冻结接口，不依赖具体数据结果。",
            P["P_delivery"], (), CHK("readme"), ("README.md",))
        ev["nodes"] = P

        run = cli.call("dag3_run_start", {
            "mid": mid, "max_concurrency": 2, "max_retries": 2,
            "max_calls": 20, "max_cost_usd": 15.0, "real": True,
            "node_cap": 20, "timeout": 1500})
        ev["run_start"] = run
        assert run.get("ok"), run
        t0 = time.time()
        last = {}
        while time.time() - t0 < 1500:
            st = cli.call("dag3_run_status", {"mid": mid})
            last = st
            if not st.get("running"):
                break
            if not ev.get("intervened") and st.get("inflight", 0) >= 1:
                ev["intervened"] = cli.call(
                    "dag3_set_priority",
                    {"mid": mid, "node_id": P["N07"], "priority": 5})
            time.sleep(5)
        ev["final_status"] = last
        ev["board"] = cli.call("dag3_board", {"mid": mid})
        ev["mcp_calls"] = len(cli.log)
        done = (last.get("mission_status") == "done")
        ev["act1"] = {"mission_done": done}
        (EVID / "act1_result.json").write_text(
            json.dumps(ev, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        (EVID / "act1_mcp_log.json").write_text(
            json.dumps(cli.log, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        print(json.dumps({"mission_done": done,
                          "status": {k: last.get(k) for k in
                                     ("running", "phase", "halt_reason",
                                      "budget", "mission_status")}},
                         ensure_ascii=False))
        return 0 if done else 1
    finally:
        cli.close()


if __name__ == "__main__":
    sys.exit(main())
