# -*- coding: utf-8 -*-
"""N05 CLI 入口模块（订单-退款对账）。

接口（冻结）：
- 用法：python main.py --orders O.csv --refunds R.csv --out summary.json
- 合法输入：退出码 0，把 reconcile() 的返回值原样以 UTF-8 JSON 写入
  --out 指定文件（ensure_ascii=False）；
- 非法输入：退出码 2，不产出 summary.json，stdout 仅输出单行 JSON
  {"error":"<错误码>","message":"<可选字符串>"}。

真实调用上游三模块（import 后调用，不预存答案）：
- loader.load_orders(path) / loader.load_refunds(path)
- validation.validate(orders, refunds) -> (paid, refunds2, cancelled)
- reconcile.reconcile(paid, refunds2, cancelled_orders=cancelled)

错误码：上游 InputError 的 13 个冻结码经 e.code 原样透出——
order_id_duplicate / refund_id_duplicate / unknown_order /
refund_on_cancelled / currency_mismatch / amount_invalid /
refund_exceeds_order / status_invalid / columns_missing /
currency_invalid / order_id_empty / refund_id_empty / region_empty。
本模块自用的非冻结码：usage（命令行参数缺失/非法）、io_error（输入
文件不可读或输出不可写）、internal_error（未预期异常）。

仅使用 Python 标准库。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:  # 发布形态：loader/validation/reconcile 与 main.py 同目录（脚本目录
    import loader  # 本就在 sys.path），或经 PYTHONPATH 提供。
    import reconcile
    import validation
except ImportError:  # 开发形态：上游模块播种在 ./inputs/<来源>/ 下，
    _inputs = Path(__file__).resolve().parent / "inputs"  # 追加后重试。
    if _inputs.is_dir():
        for _sub in sorted(_inputs.iterdir()):
            if _sub.is_dir():
                sys.path.append(str(_sub))
    import loader
    import reconcile
    import validation

__all__ = ["main"]


class _Parser(argparse.ArgumentParser):
    """用法错误也走 error-JSON + 退出码 2 的统一出口。"""

    def error(self, message: str) -> None:
        _fail("usage", message, None)


def _fail(code: str, message: str,
          args: argparse.Namespace | None) -> None:
    """非法输入统一出口：stdout 单行 error JSON、清理陈旧产物、退出 2。"""
    payload = {"error": code}
    if message and message != code:
        payload["message"] = message
    print(json.dumps(payload, ensure_ascii=False))
    if args is not None and args.out \
            and args.out not in (args.orders, args.refunds):
        try:  # 失败后不得残留旧 summary.json（绝不误删输入文件）
            Path(args.out).unlink()
        except OSError:
            pass
    raise SystemExit(2)


def _write_summary(path: str, summary: dict) -> None:
    """把 reconcile 返回值原样写为 UTF-8 JSON（ensure_ascii=False）。"""
    text = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(
        prog="main.py",
        description="订单-退款对账：读取订单/退款 CSV，校验后按 "
                    "(region, currency) 分组汇总，写出 JSON 摘要。")
    parser.add_argument("--orders", required=True, help="订单 CSV 路径")
    parser.add_argument("--refunds", required=True, help="退款 CSV 路径")
    parser.add_argument("--out", required=True, help="汇总 JSON 输出路径")
    args = parser.parse_args(argv)
    try:
        orders = loader.load_orders(args.orders)
        refunds = loader.load_refunds(args.refunds)
        paid, valid_refunds, cancelled = validation.validate(orders, refunds)
        summary = reconcile.reconcile(paid, valid_refunds,
                                      cancelled_orders=cancelled)
        _write_summary(args.out, summary)
    except loader.InputError as exc:
        _fail(exc.code, str(exc), args)
    except OSError as exc:
        _fail("io_error", str(exc), args)
    except Exception as exc:  # 未预期异常：绝不半途产出，按非法输入口径退出
        _fail("internal_error", f"{type(exc).__name__}: {exc}", args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
