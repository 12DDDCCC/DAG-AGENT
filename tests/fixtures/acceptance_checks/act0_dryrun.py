# -*- coding: utf-8 -*-
"""act0_dryrun.py — 零模型干跑：同 SPEC 拓扑 + 内嵌正确实现的后端。

目的：烧真钱前验证（1）任务图/依赖/OR 组正确；（2）可信考卷包 mod_checks
各子命令对【正确实现】判过、对【坏实现】判否；（3）整合/绑定/根 judge 20 例
全链闭合。后端实现为内嵌已知正确代码（等价于把标准答案交给执行者）。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dag_agent.assessment import default_checker3
from dag_agent.graph import Graph
from dag_agent.repository import Repository
from dag_agent.scheduler import Scheduler

PKG = Path(__file__).resolve().parent
CHECKS = PKG / "checks" / "mod_checks.py"
CHK = lambda n: f'python "{CHECKS}" {n}'  # noqa: E731
(PKG / "checks" / "data_ref.txt").write_text("v1", encoding="utf-8")

LOADER = '''
import csv

class InputError(ValueError):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)

_REQ = {"orders": ("order_id","region","currency","amount_cents","status"),
        "refunds": ("refund_id","order_id","currency","amount_cents")}

def _read(path, kind):
    with open(path, "r", encoding="utf-8-sig", newline="") as h:
        r = csv.DictReader(h, strict=True)
        fields = r.fieldnames or []
        if len(fields) != len(set(fields)) or not set(_REQ[kind]).issubset(fields):
            raise InputError("columns_missing")
        return [dict(row) for row in r]

def load_orders(path):
    return _read(path, "orders")

def load_refunds(path):
    return _read(path, "refunds")
'''

VALIDATION = '''
from loader import InputError
import re

_AMT = re.compile(r"[0-9]+\\Z")
_CUR = {"CNY", "USD"}
_ST = {"paid", "cancelled"}

def _bad(v):
    return v is None or str(v).strip() == ""

def validate(orders, refunds):
    seen_o, seen_r = set(), set()
    clean_o, clean_r = [], []
    canc = 0
    all_o = {}
    for o in orders:
        oid = (o.get("order_id") or "").strip()
        if _bad(oid):
            raise InputError("order_id_empty")
        if oid in seen_o:
            raise InputError("order_id_duplicate")
        seen_o.add(oid)
        region = (o.get("region") or "").strip()
        if _bad(region):
            raise InputError("region_empty")
        cur = (o.get("currency") or "").strip()
        if cur not in _CUR:
            raise InputError("currency_invalid")
        amt = o.get("amount_cents") or ""
        if not _AMT.match(amt):
            raise InputError("amount_invalid")
        st = (o.get("status") or "").strip()
        if st not in _ST:
            raise InputError("status_invalid")
        row = dict(o, order_id=oid, region=region, currency=cur,
                   amount_cents=int(amt), status=st)
        all_o[oid] = row
        if st == "paid":
            clean_o.append(row)
        else:
            canc += 1
    paid = {o["order_id"]: o for o in clean_o}
    by_order = {}
    for r in refunds:
        rid = (r.get("refund_id") or "").strip()
        if _bad(rid):
            raise InputError("refund_id_empty")
        if rid in seen_r:
            raise InputError("refund_id_duplicate")
        seen_r.add(rid)
        tgt = (r.get("order_id") or "").strip()
        if tgt not in all_o:
            raise InputError("unknown_order")
        if all_o[tgt]["status"] != "paid":
            raise InputError("refund_on_cancelled")
        cur = (r.get("currency") or "").strip()
        if cur != paid[tgt]["currency"]:
            raise InputError("currency_mismatch")
        amt = r.get("amount_cents") or ""
        if not _AMT.match(amt):
            raise InputError("amount_invalid")
        amt = int(amt)
        if amt > paid[tgt]["amount_cents"] - sum(
                x["amount_cents"] for x in by_order.get(tgt, [])):
            raise InputError("refund_exceeds_order")
        row = dict(r, refund_id=rid, order_id=tgt, currency=cur,
                   amount_cents=amt)
        by_order.setdefault(tgt, []).append(row)
        clean_r.append(row)
    return clean_o, clean_r, canc
'''

RECON = '''
def reconcile(orders, refunds, cancelled_orders=0):
    agg = {}
    for o in orders:
        k = (o["currency"], o["region"])
        a = agg.setdefault(k, [0, 0, 0])
        a[0] += 1
        a[1] += o["amount_cents"]
    for r in refunds:
        k = (r["currency"], next(o["region"] for o in orders
                                 if o["order_id"] == r["order_id"]))
        agg[k][2] += r["amount_cents"]
    totals = [{"region": reg, "currency": cur, "paid_orders": v[0],
               "gross_cents": v[1], "refund_cents": v[2],
               "net_cents": v[1] - v[2]}
              for (cur, reg), v in sorted(agg.items())]
    return {"schema_version": 1, "cancelled_orders": cancelled_orders,
            "totals": totals}
'''

RECON2 = RECON.replace("def reconcile", "def reconcile")  # 路线B：同接口不同写法

RENDER = '''
def render(summary):
    lines = ["对账汇总报告（schema %s）" % summary.get("schema_version"),
             "取消订单：%s" % summary.get("cancelled_orders")]
    for t in summary.get("totals", []):
        lines.append("{region}/{currency}: paid={paid_orders} "
                     "gross={gross_cents} refund={refund_cents} "
                     "net={net_cents}".format(**t))
    return "\\n".join(lines)
'''

MAIN = '''
import json, sys

def _run(argv):
    args = dict(zip(argv[1::2], argv[2::2]))
    try:
        import loader, validation, reconcile
        orders = loader.load_orders(args["--orders"])
        refunds = loader.load_refunds(args["--refunds"])
        co, cr, canc = validation.validate(orders, refunds)
        summary = reconcile.reconcile(co, cr, canc)
        with open(args["--out"], "w", encoding="utf-8") as h:
            json.dump(summary, h, ensure_ascii=False)
        return 0
    except loader.InputError as e:
        print(json.dumps({"error": getattr(e, "code", "unknown"),
                          "message": str(e)}, ensure_ascii=False))
        return 2

if __name__ == "__main__":
    sys.exit(_run(sys.argv))
'''

README = """# 订单-退款对账 CLI

用法：
    python main.py --orders orders.csv --refunds refunds.csv --out summary.json

输入列：orders=order_id,region,currency,amount_cents,status；
refunds=refund_id,order_id,currency,amount_cents。

失败行为：非法输入退出码 2，不产出 summary.json，stdout 为
{"error":"错误码","message":"说明"}。错误码（13）：
order_id_duplicate, refund_id_duplicate, unknown_order,
refund_on_cancelled, currency_mismatch, amount_invalid,
refund_exceeds_order, status_invalid, columns_missing,
currency_invalid, order_id_empty, refund_id_empty, region_empty。

示例：python main.py --orders fixtures/v1/orders.csv \
--refunds fixtures/v1/refunds.csv --out summary.json
（合法输入退出 0，summary.json 按 (currency,region) 升序给出分组汇总。）
"""

PLAN: dict[str, dict[str, str]] = {
    "资料冻结": {"data/orders.csv": None, "data/refunds.csv": None},
    "CSV导入": {"loader.py": LOADER},
    "业务校验": {"validation.py": VALIDATION},
    "汇总路线A": {"reconcile.py": RECON},
    "汇总路线B": {"reconcile.py": RECON2},
    "CLI": {"main.py": MAIN},
    "报告": {"render.py": RENDER,
             "render.md": "# 示例报告\nnorth/CNY paid=1 gross=100 refund=0 net=100\n"},
    "使用说明": {"README.md": README},
}


class BundleBackend:
    """按标题写内嵌正确实现；整合节点复制 inputs 全量并写说明。"""
    name = "dryrun"

    def run(self, statement, workdir, check_cmd, title=""):
        import shutil
        wd = Path(workdir)
        if title in PLAN:
            spec = PLAN[title]
            if title == "资料冻结":
                src = PKG / "fixtures" / "v1"
                (wd / "data").mkdir(parents=True, exist_ok=True)
                for n in ("orders.csv", "refunds.csv"):
                    shutil.copy2(src / n, wd / "data" / n)
            else:
                for n, c in spec.items():
                    p = wd / n
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(c.strip() + "\n", encoding="utf-8")
        elif "整合交付" in statement:
            ins = wd / "inputs"
            if ins.is_dir():
                for f in sorted(ins.rglob("*")):
                    if f.is_file():
                        rel = f.relative_to(ins)
                        dest = wd / rel.relative_to(rel.parts[0])
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(f, dest)
            (wd / "INTEGRATION.md").write_text(
                "# 整合说明\n来源：inputs/ 各已验收节点快照。\n",
                encoding="utf-8")
        return {"ok": True, "summary": "DONE", "session_id": f"dry-{title}",
                "resources": {"cost_usd": 0.0}}


def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="act0_"))
    repo = Repository(home / "mission" / "mission.db")
    g = Graph(repo, checker3=default_checker3)
    BUNDLE = ["loader.py", "validation.py", "reconcile.py", "render.py",
              "main.py", "README.md", "data/orders.csv", "data/refunds.csv",
              "INTEGRATION.md"]
    IN_A = ("loader.py", "validation.py", "data/orders.csv", "data/refunds.csv",
            "INTEGRATION.md")
    IN_C = IN_A + ("reconcile.py",)
    mid, root = g.create_mission(
        "干跑", root_check=CHK("root"), root_declared_artifacts=tuple(BUNDLE))

    def integ_stmt():
        return ("整合交付：把 inputs/ 下全部来源文件按原相对路径合并复制到交付目录"
                "（保留来源节点目录名下一级之外的相对结构：即 inputs/<src>/<rel> "
                "→ <rel>），同路径冲突报错；另写 INTEGRATION.md。")
    p_in = g.add_node(mid, "P_input", integ_stmt(), check_cmd=CHK("integration"), declared_artifacts=IN_A, parent_id=root)
    n01 = g.add_node(mid, "资料冻结",
                     "复制冻结数据（见任务书）", parent_id=p_in,
                     check_cmd=CHK("n01"),
                     declared_artifacts=("data/orders.csv", "data/refunds.csv"))
    n02 = g.add_node(mid, "CSV导入", "loader", parent_id=p_in,
                     check_cmd=CHK("loader"), declared_artifacts=("loader.py",))
    n03 = g.add_node(mid, "业务校验", "validation", parent_id=p_in,
                     deps=(n02,), check_cmd=CHK("validation"),
                     declared_artifacts=("validation.py",))
    # P_input 需要依赖 N01/N02/N03（执行依赖）——补充依赖边
    from dag_agent.repository import now as _now
    with repo.tx() as cur:
        for d in (n01, n02, n03):
            cur.execute("INSERT OR IGNORE INTO dependencies(node_id,depends_on,"
                        "created_at) VALUES(?,?,?)", (p_in, d, _now()))
    p_co = g.add_node(mid, "P_compute", integ_stmt(), check_cmd=CHK("integration"), declared_artifacts=IN_C, parent_id=root,
                      deps=(p_in,))
    a = g.add_node(mid, "汇总路线A", "reconcile A", parent_id=p_co,
                   deps=(p_in,), check_cmd=CHK("recon"),
                   declared_artifacts=("reconcile.py",),
                   group_id="g1", group_order=0, switch_after=1)
    b = g.add_node(mid, "汇总路线B", "reconcile B", parent_id=p_co,
                   deps=(p_in,), check_cmd=CHK("recon"),
                   declared_artifacts=("reconcile.py",),
                   group_id="g1", group_order=1, switch_after=1)
    p_dl = g.add_node(mid, "P_delivery", integ_stmt(), deps=(p_in, p_co), check_cmd=CHK("integration"), declared_artifacts=tuple(BUNDLE), parent_id=root)
    n05 = g.add_node(mid, "CLI", "main", parent_id=p_dl,
                     deps=(p_in, p_co), check_cmd=CHK("cli"),
                     declared_artifacts=("main.py",))
    n06 = g.add_node(mid, "报告", "render", parent_id=p_dl, deps=(p_in,),
                     check_cmd=CHK("render"),
                     declared_artifacts=("render.md", "render.py"))
    n07 = g.add_node(mid, "使用说明", "README", parent_id=p_dl,
                     check_cmd=CHK("readme"), declared_artifacts=("README.md",))
    with repo.tx() as cur:
        for d in (n05, n06, n07):
            cur.execute("INSERT OR IGNORE INTO dependencies(node_id,depends_on,"
                        "created_at) VALUES(?,?,?)", (p_dl, d, _now()))
    s = Scheduler(repo, g, BundleBackend(), max_concurrency=2,
                  retry_policy=None if False else
                  __import__("dag_agent.recovery", fromlist=["RetryPolicy"])
                  .RetryPolicy(max_retries=1), reap_enabled=False)
    summary = s.run_until_done(mid, root, timeout=300, poll=0.1)
    print({k: summary[k] for k in ("mission_status", "halt_reason",
                                   "budget")})
    for n in repo.list_nodes(mid):
        if n.status != "accepted":
            print("未闭合:", n.id, n.title, n.status)
            for a_ in repo.assessments_of(n.id):
                print("   ", a_.verdict, a_.evidence[:160])
    ok = summary["mission_status"] == "done"
    repo.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
