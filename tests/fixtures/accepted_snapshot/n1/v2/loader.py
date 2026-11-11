# -*- coding: utf-8 -*-
"""N02 CSV 导入模块（订单-退款对账）。

接口（冻结）：
- InputError(ValueError)：InputError(code) 或 InputError(code, message)，
  属性 e.code 为错误码；
- load_orders(path: str) -> list[dict]
- load_refunds(path: str) -> list[dict]

行为：
- dict 键 = CSV 列名原样，值 = 字段字符串原样（金额不转 int，保留
  前导零/尾随空白等原始文本；引号字段去引号后原样返回）；
- 容忍 UTF-8 BOM（utf-8-sig）、合法引号字段（含逗号）、表头额外命名列；
- 表头重复或缺必需列 -> raise InputError("columns_missing")；行字段数
  与表头不符（多出未命名字段/必需列缺值）或引号非法，同样按
  "columns_missing" 拒绝。

仅使用 Python 标准库。
"""
from __future__ import annotations

import csv
from pathlib import Path

ORDER_COLUMNS = ("order_id", "region", "currency", "amount_cents", "status")
REFUND_COLUMNS = ("refund_id", "order_id", "currency", "amount_cents")

__all__ = ["InputError", "load_orders", "load_refunds",
           "ORDER_COLUMNS", "REFUND_COLUMNS"]


class InputError(ValueError):
    """输入格式错误。code 为平台冻结的错误码，message 可选。"""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


def _read(path: str, required: tuple) -> list:
    """按必需列契约读取 CSV，返回 list[dict]（键=列名原样，值=字符串原样）。"""
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, strict=True)
        fields = reader.fieldnames or []
        if len(fields) != len(set(fields)) or not set(required).issubset(fields):
            raise InputError("columns_missing", f"Required columns: {required}")
        rows = []
        try:
            for row in reader:
                if None in row or any(row.get(name) is None for name in required):
                    raise InputError("columns_missing", "Malformed CSV row")
                rows.append(row)
        except csv.Error as exc:
            raise InputError("columns_missing", str(exc)) from exc
        return rows


def load_orders(path: str) -> list[dict]:
    """读取订单 CSV。必需列：order_id,region,currency,amount_cents,status。"""
    return _read(path, ORDER_COLUMNS)


def load_refunds(path: str) -> list[dict]:
    """读取退款 CSV。必需列：refund_id,order_id,currency,amount_cents。"""
    return _read(path, REFUND_COLUMNS)
