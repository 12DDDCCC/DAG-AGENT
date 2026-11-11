# -*- coding: utf-8 -*-
"""可信考卷包（SPEC 冻结）：每节点行为检查，单文件多子命令。

用法：python mod_checks.py <子命令>
子命令在【交付目录】（候选/重放目录）内执行；fixtures 与本包在项目内绝对路径。
数据指针 data_ref.txt：v1 或 v2（幕三切换，修订级联的数据源版本）。
退出码：0 过 / 1 产物不合格 / 2 用法或环境故障（考卷侧 ERROR）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent          # .../outputs/多agent协同验收项目
FIX = BASE.parent / "fixtures"
JUDGE = BASE.parent / "judge.py"

# 冻结期望（oracle 预生成；north/CNY 为 SPEC 规定可观测点）
EXP = {
    "v1": {"north_CNY_net": 26000, "cancelled_orders": 3},
    "v2": {"north_CNY_net": 27000, "cancelled_orders": 3},
}


def _ref() -> str:
    t = (BASE / "data_ref.txt").read_text(encoding="utf-8").strip()
    if t not in EXP:
        print("data_ref 非法", file=sys.stderr)
        sys.exit(2)
    return t


def _py() -> str:
    return sys.executable


def _tmpdir(prefix: str) -> Path:
    import tempfile
    d = Path(tempfile.mkdtemp(prefix=prefix))
    return d


def _write_csv(p: Path, header: str, rows: list[str]):
    p.write_text("\n".join([header] + rows) + "\n", encoding="utf-8")


ORD_HDR = "order_id,region,currency,amount_cents,status"
REF_HDR = "refund_id,order_id,currency,amount_cents"



def _syspath():
    import sys as _s
    _s.path.insert(0, ".")
    ins = Path("inputs")
    if ins.is_dir():
        for d in sorted(ins.iterdir()):
            if d.is_dir():
                _s.path.insert(0, str(d))

def n01():
    """N01 资料冻结：交付 data/*.csv 与当前指针版本 fixtures 逐字节一致。"""
    import hashlib
    ref = _ref()
    for name in ("orders.csv", "refunds.csv"):
        src = FIX / ref / name
        dst = Path("data") / name
        if not dst.is_file():
            print(f"缺 {dst}", file=sys.stderr)
            return 1
        if hashlib.sha256(dst.read_bytes()).hexdigest() != \
                hashlib.sha256(src.read_bytes()).hexdigest():
            print(f"{dst} 与 {ref} 冻结数据不一致", file=sys.stderr)
            return 1
    print(f"n01-ok({ref})")
    return 0


def loader():
    """N02 CSV导入：BOM/引号/额外列/坏列行为。"""
    _syspath()
    try:
        import loader
    except Exception as e:
        print(f"import loader 失败：{e}", file=sys.stderr)
        return 1
    d = _tmpdir("ldr_")
    f1 = d / "a.csv"
    f1.write_bytes(("﻿" + ORD_HDR + "\no1,north,CNY,100,paid\n").encode("utf-8"))
    f2 = d / "b.csv"
    _write_csv(f2, "order_id,region,currency,amount_cents,status,extra",
               ['o2,"south,区",USD,"010",cancelled,x'])
    try:
        rows = loader.load_orders(str(f1)) + loader.load_orders(str(f2))
    except Exception as e:
        print(f"load_orders 异常：{e}", file=sys.stderr)
        return 1
    if len(rows) != 2 or rows[0].get("order_id") != "o1" \
            or rows[1].get("region") != "south,区" \
            or rows[1].get("amount_cents") not in ("010",):
        print(f"解析结果不符：{rows}", file=sys.stderr)
        return 1
    f3 = d / "c.csv"
    _write_csv(f3, "order_id,region", ["o3,north"])   # 缺必需列
    try:
        loader.load_orders(str(f3))
        print("缺必需列未被拒绝", file=sys.stderr)
        return 1
    except Exception as e:
        if getattr(e, "code", "") != "columns_missing":
            print(f"缺列错误码不符：{type(e).__name__}:{e}", file=sys.stderr)
            return 1
    print("loader-ok")
    return 0


def validation():
    """N03 业务校验：合法清洗 + 三类非法码。"""
    _syspath()
    try:
        import loader, validation
    except Exception as e:
        print(f"import 失败：{e}", file=sys.stderr)
        return 1
    IE = getattr(loader, "InputError", None)
    if IE is None:
        print("loader 未定义 InputError", file=sys.stderr)
        return 1
    orders = [
        {"order_id": "o1", "region": "north", "currency": "CNY",
         "amount_cents": "100", "status": "paid"},
        {"order_id": "o2", "region": "south", "currency": "USD",
         "amount_cents": "050", "status": "cancelled"},
    ]
    refunds = [{"refund_id": "r1", "order_id": "o1", "currency": "CNY",
                "amount_cents": "40"}]
    try:
        co, cr, canc = validation.validate(orders, refunds)
    except Exception as e:
        print(f"合法输入被拒：{e}", file=sys.stderr)
        return 1
    if len(co) != 1 or co[0]["amount_cents"] != 100 or len(cr) != 1 \
            or cr[0]["amount_cents"] != 40:
        print(f"清洗结果不符：{co},{cr}", file=sys.stderr)
        return 1
    for code, bad_orders, bad_refunds in (
        ("order_id_duplicate",
         [orders[0], dict(orders[0])], []),
        ("refund_on_cancelled",
         orders, [{"refund_id": "r9", "order_id": "o2", "currency": "USD",
                   "amount_cents": "1"}]),
        ("amount_invalid",
         [dict(orders[0], amount_cents="12x")], []),
    ):
        try:
            validation.validate(bad_orders, bad_refunds)
            print(f"{code} 未被拒绝", file=sys.stderr)
            return 1
        except IE as e:
            if getattr(e, "code", "") != code:
                print(f"错误码不符：期望{code} 得{getattr(e,'code','')}",
                      file=sys.stderr)
                return 1
        except Exception as e:
            print(f"{code} 抛非InputError：{e}", file=sys.stderr)
            return 1
    print("validation-ok")
    return 0


def recon():
    """N04A/B 组级强功能考卷（同一冻结输出）：对当前指针 fixtures 全量核算。"""
    _syspath()
    ref = _ref()
    try:
        import loader, validation, reconcile
    except Exception as e:
        print(f"import 失败：{e}", file=sys.stderr)
        return 1
    o = loader.load_orders(str(FIX / ref / "orders.csv"))
    r = loader.load_refunds(str(FIX / ref / "refunds.csv"))
    co, cr, canc = validation.validate(o, r)
    try:
        s = reconcile.reconcile(co, cr, canc)
    except Exception as e:
        print(f"reconcile 异常：{e}", file=sys.stderr)
        return 1
    north = [t for t in s.get("totals", [])
             if t.get("region") == "north" and t.get("currency") == "CNY"]
    if not north or north[0].get("net_cents") != EXP[ref]["north_CNY_net"] \
            or s.get("cancelled_orders") != EXP[ref]["cancelled_orders"] \
            or s.get("schema_version") != 1:
        print(f"汇总不符（{ref}）：{json.dumps(s, ensure_ascii=False)[:300]}",
              file=sys.stderr)
        return 1
    keys = [(t["currency"], t["region"]) for t in s["totals"]]
    if keys != sorted(keys):
        print("totals 未按(currency,region)升序", file=sys.stderr)
        return 1
    print(f"recon-ok({ref},north/CNY={EXP[ref]['north_CNY_net']})")
    return 0


def cli():
    """N05 CLI：真实调用 loader/validation/reconcile，产出与冻结期望一致。"""
    ref = _ref()
    import subprocess as sp
    with tempfile.TemporaryDirectory(prefix="cli_") as td:
        out = Path(td) / "summary.json"
        import os
        env = dict(os.environ)
        ins = Path("inputs")
        if ins.is_dir():
            env["PYTHONPATH"] = os.pathsep.join(
                str(d) for d in sorted(ins.iterdir()) if d.is_dir())
        r = sp.run([_py(), "main.py", "--orders", str(FIX / ref / "orders.csv"),
                    "--refunds", str(FIX / ref / "refunds.csv"),
                    "--out", str(out)], capture_output=True, text=True,
                   cwd=".", timeout=120, env=env)
        if r.returncode != 0 or not out.is_file():
            print(f"CLI 失败 rc={r.returncode} {r.stderr[:200]}", file=sys.stderr)
            return 1
        s = json.loads(out.read_text(encoding="utf-8"))
        north = [t for t in s.get("totals", [])
                 if t.get("region") == "north" and t.get("currency") == "CNY"]
        if not north or north[0].get("net_cents") != EXP[ref]["north_CNY_net"]:
            print(f"CLI 汇总不符：{json.dumps(s, ensure_ascii=False)[:300]}",
                  file=sys.stderr)
            return 1
    print(f"cli-ok({ref})")
    return 0


def render():
    """N06 报告：render(summary) 可读且覆盖全部分组。"""
    _syspath()
    try:
        import render
    except Exception as e:
        print(f"import render 失败：{e}", file=sys.stderr)
        return 1
    s = {"schema_version": 1, "cancelled_orders": 1,
         "totals": [{"region": "north", "currency": "CNY", "paid_orders": 1,
                     "gross_cents": 100, "refund_cents": 0, "net_cents": 100}]}
    try:
        text = render.render(s)
    except Exception as e:
        print(f"render 异常：{e}", file=sys.stderr)
        return 1
    if "north" not in text or "CNY" not in text or "100" not in text:
        print(f"报告内容缺分组：{str(text)[:200]}", file=sys.stderr)
        return 1
    print("render-ok")
    return 0


def readme():
    """N07 使用说明：接口/示例/失败行为。"""
    p = Path("README.md")
    if not p.is_file():
        print("缺 README.md", file=sys.stderr)
        return 1
    t = p.read_text(encoding="utf-8")
    if ("--orders" not in t or "--refunds" not in t
            or t.count("_") < 1 or len(t) < 300):
        print("README 不含用法/过短", file=sys.stderr)
        return 1
    missing = [c for c in ("order_id_duplicate", "refund_exceeds_order",
                           "columns_missing") if c not in t]
    if missing:
        print(f"README 缺错误码说明：{missing}", file=sys.stderr)
        return 1
    print("readme-ok")
    return 0


def integration(required: str = "") -> int:
    """各整合节点：申报集合逐件断言 + 发布形态冒烟（总账 A2 2026-10-03；
    申报集合绑定 2026-10-04 Codex 63bf8d2 复核补齐——验收集合=发布集合）。

    - 上游来源完整性：inputs/ 每个来源目录至少一件非空交付，
      每件 .py 可编译（空来源目录显式拒）；
    - 申报集合：优先读候选目录 _declared.json（平台 P1 冻结的实际发布
      清单）；无该文件（直接调用）时保守取候选根全部非 inputs 文件；
    - 逐件断言：申报集合内每件存在、非空、.py 可编译——缺件/坏件
      当场拒（候选全目录可见不等于发布集合，检查只认申报集合）；
    - 冒烟（点名入口时）：入口必须在申报集合内；冒烟目录=仅复制申报
      集合的临时副本（快照形态——发布的=检查的），断言 rc=0 且汇总
      达冻结期望。缺交付入口不再回退上游冒烟（上游可跑不构成交付
      依据）；不带点名参数时仅逐件断言（P_input/P_compute 型）。
    """
    import py_compile
    ins = Path("inputs")
    if not ins.is_dir():
        print("缺 inputs/", file=sys.stderr)
        return 1
    for sd in sorted(p for p in ins.iterdir() if p.is_dir()):
        got = [f for f in sd.rglob("*") if f.is_file()
               and f.stat().st_size > 0]
        if not got:
            print(f"空来源目录（无有效交付）：{sd}", file=sys.stderr)
            return 1
    files = [f.resolve() for f in ins.rglob("*") if f.is_file()]
    if len(files) < 2:
        print(f"inputs 文件过少：{len(files)}", file=sys.stderr)
        return 1
    src_dirs = sorted({f.parent for f in files})
    for sd in src_dirs:
        if not any(f.stat().st_size > 0 for f in files if f.parent == sd):
            print(f"来源目录无有效交付：{sd}", file=sys.stderr)
            return 1
    for f in files:
        if f.suffix == ".py":
            if f.stat().st_size == 0:
                print(f"空 .py 文件：{f}", file=sys.stderr)
                return 1
            try:
                py_compile.compile(str(f), doraise=True)
            except py_compile.PyCompileError as e:
                print(f".py 编译失败：{f} | {e}", file=sys.stderr)
                return 1
    # 本次申报集合：平台冻结清单优先，否则保守取候选根非 inputs 文件
    decl_file = Path("_declared.json")
    if decl_file.is_file():
        try:
            declared = sorted(json.loads(decl_file.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"申报清单不可解析：{e}", file=sys.stderr)
            return 1
        if not declared:
            print("申报清单为空", file=sys.stderr)
            return 1
    else:
        declared = sorted(
            f.as_posix() for f in Path(".").rglob("*")
            if f.is_file() and f.parts[:1] != ("inputs",))
    if required and required not in declared:
        print(f"点名入口不在申报清单：{required}（缺交付入口，"
              "上游冒烟不构成交付依据）", file=sys.stderr)
        return 1
    if not declared:
        print("候选根无申报产物（无 _declared.json 且无非 inputs 文件）",
              file=sys.stderr)
        return 1
    for rel in declared:
        p2 = Path(rel)
        if not p2.is_file():
            print(f"申报工件缺失：{rel}", file=sys.stderr)
            return 1
        if p2.stat().st_size == 0:
            print(f"申报工件为空：{rel}", file=sys.stderr)
            return 1
        if p2.suffix == ".py":
            try:
                py_compile.compile(str(p2.resolve()), doraise=True)
            except py_compile.PyCompileError as e:
                print(f"申报 .py 编译失败：{rel} | {e}", file=sys.stderr)
                return 1
    if required:
        ref = _ref()
        with tempfile.TemporaryDirectory(prefix="smoke_") as td:
            bundle = Path(td) / "bundle"
            bundle.mkdir()
            for rel in declared:          # 快照形态：只带申报集合
                dest = bundle / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(rel, dest)
            out = Path(td) / "summary.json"
            r = subprocess.run(
                [_py(), str(bundle / required), "--orders",
                 str(FIX / ref / "orders.csv"),
                 "--refunds", str(FIX / ref / "refunds.csv"),
                 "--out", str(out)], capture_output=True, text=True,
                cwd=str(bundle), timeout=120)
            if r.returncode != 0 or not out.is_file():
                print(f"冒烟失败 rc={r.returncode} {r.stderr[:200]}",
                      file=sys.stderr)
                return 1
            try:
                s2 = json.loads(out.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"冒烟产物非 JSON：{e}", file=sys.stderr)
                return 1
            north = [t for t in s2.get("totals", [])
                     if t.get("region") == "north"
                     and t.get("currency") == "CNY"]
            if not north or                     north[0].get("net_cents") != EXP[ref]["north_CNY_net"]:
                print(f"冒烟汇总不符（{ref}）："
                      f"{json.dumps(s2, ensure_ascii=False)[:200]}",
                      file=sys.stderr)
                return 1
    print("integration-ok")
    return 0


def root():
    """ROOT 根验收（异构组合）：独立 judge 全过（bundle=当前目录；
    2026-10-04 起 v2 判卷 22 例，含金额尾随换行两变体）。"""
    import os
    with tempfile.TemporaryDirectory(prefix="judge_ev_") as td:
        r = subprocess.run(
            [_py(), str(JUDGE), "--bundle", str(Path(".").resolve()),
             "--evidence", str(Path(td) / "evidence.json")],
            capture_output=True, text=True, timeout=600,
            cwd=str(JUDGE.parent))
    if r.returncode != 0:
        print(f"judge 未全过 rc={r.returncode}：{r.stdout[-400:]}",
              file=sys.stderr)
        return 1
    print("root-judge-ok(22/22, v2 判卷)")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] not in globals():
        print("用法: mod_checks.py "
              "n01|loader|validation|recon|cli|render|readme|integration|root"
              "（integration 可点名拼装入口，如 integration main.py）",
              file=sys.stderr)
        return 2
    if len(args) > 2:
        print("用法: 至多一个附加参数（integration <入口文件名>）",
              file=sys.stderr)
        return 2
    return int(globals()[args[0]](*args[1:]))


if __name__ == "__main__":
    sys.exit(main())
