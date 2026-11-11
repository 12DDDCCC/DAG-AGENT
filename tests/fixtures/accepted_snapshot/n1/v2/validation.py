# -*- coding: utf-8 -*-
r"""N03 业务校验模块（订单-退款对账）。

接口（冻结）：
- validate(orders, refunds) -> (paid订单列表, 合法退款列表, cancelled订单数)；
  返回行 dict 的 amount_cents 已转为 int（Python 任意精度整数，支持超
  int64 大整数，全程不走 float）；输入行 dict 不被就地修改。

校验规则（与平台 oracle 对齐，全部 raise InputError(code)）：
- 订单逐行，按序：order_id_empty -> order_id_duplicate -> region_empty
  -> currency_invalid -> status_invalid -> amount_invalid；
- 退款逐行，按序：refund_id_empty -> refund_id_duplicate ->
  amount_invalid -> unknown_order -> refund_on_cancelled ->
  currency_mismatch -> refund_exceeds_order（同订单累计退款不得超过
  订单金额）；订单全部先行校验完毕再校验退款。

细节：
- 空白-only 字段视为空值（strip 后为空即空）；
- currency 仅 CNY/USD；status 仅 paid/cancelled（不 strip，原样比对）；
- amount_cents 必须是 ASCII 非负十进制整数，允许前导零（"00800"->800）；
  金额正则用 r"[0-9]+\Z" 全匹配——Python 的 $ 容忍尾随换行，\Z 不容忍，
  故 "8000\n"、"2.5" 均拒为 amount_invalid。

仅使用 Python 标准库。
"""
from __future__ import annotations

import re

from loader import InputError

AMOUNT_RE = re.compile(r"[0-9]+\Z")
CURRENCIES = frozenset({"CNY", "USD"})
STATUSES = frozenset({"paid", "cancelled"})

__all__ = ["validate"]


def _amount(value: object) -> int:
    """金额文本 -> int；非字符串或非纯 ASCII 数字则 amount_invalid。"""
    if not isinstance(value, str) or not AMOUNT_RE.fullmatch(value):
        raise InputError("amount_invalid",
                         "amount_cents must contain ASCII digits only")
    return int(value)


def _non_empty(value: object) -> bool:
    """空白-only 视为空值。"""
    return bool(value) and bool(value.strip())


def validate(orders: list[dict], refunds: list[dict]) -> tuple[list, list, int]:
    """校验订单与退款，返回 (paid订单列表, 合法退款列表, cancelled订单数)。"""
    by_id: dict[str, dict] = {}
    paid_orders: list[dict] = []
    cancelled_orders = 0
    for row in orders:
        identity = row["order_id"]
        if not _non_empty(identity):
            raise InputError("order_id_empty")
        if identity in by_id:
            raise InputError("order_id_duplicate")
        if not _non_empty(row["region"]):
            raise InputError("region_empty")
        if row["currency"] not in CURRENCIES:
            raise InputError("currency_invalid")
        if row["status"] not in STATUSES:
            raise InputError("status_invalid")
        amount = _amount(row["amount_cents"])
        cleaned = dict(row, amount_cents=amount)
        by_id[identity] = cleaned
        if row["status"] == "paid":
            paid_orders.append(cleaned)
        else:
            cancelled_orders += 1

    refund_ids: set[str] = set()
    refunded_per_order: dict[str, int] = {}
    valid_refunds: list[dict] = []
    for row in refunds:
        identity = row["refund_id"]
        if not _non_empty(identity):
            raise InputError("refund_id_empty")
        if identity in refund_ids:
            raise InputError("refund_id_duplicate")
        refund_ids.add(identity)
        amount = _amount(row["amount_cents"])
        order = by_id.get(row["order_id"])
        if order is None:
            raise InputError("unknown_order")
        if order["status"] != "paid":
            raise InputError("refund_on_cancelled")
        if row["currency"] != order["currency"]:
            raise InputError("currency_mismatch")
        total = refunded_per_order.get(row["order_id"], 0) + amount
        if total > order["amount_cents"]:
            raise InputError("refund_exceeds_order")
        refunded_per_order[row["order_id"]] = total
        valid_refunds.append(dict(row, amount_cents=amount))

    return paid_orders, valid_refunds, cancelled_orders
