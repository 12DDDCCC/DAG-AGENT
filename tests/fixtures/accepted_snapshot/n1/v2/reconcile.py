# -*- coding: utf-8 -*-
"""N04 汇总计算模块（订单-退款对账）。

接口（冻结）：
- reconcile(orders: list[dict], refunds: list[dict],
            cancelled_orders: int = 0) -> dict
  输入为 validate() 输出：paid 订单列表、合法退款列表（两者的
  amount_cents 均已转为 int）、cancelled 订单数。

行为：
- 按 (region, currency) 分组汇总 paid_orders / gross_cents /
  refund_cents / net_cents；净额 = gross - refund，全程整数运算，
  禁浮点；不同币种不合并；
- 退款金额计入对应订单所在 (region, currency) 组的 refund_cents
  （退款币种上游已校验与订单一致，分组键取订单侧）；
- 返回 {"schema_version": 1, "cancelled_orders": N, "totals": [...]}，
  totals 按 (currency, region) 升序，每项字段
  region/currency/paid_orders/gross_cents/refund_cents/net_cents，
  类型精确匹配（str/str/int/int/int/int）；输入列表不被就地修改。

仅使用 Python 标准库。
"""
from __future__ import annotations

__all__ = ["reconcile"]


def reconcile(orders: list[dict], refunds: list[dict],
              cancelled_orders: int = 0) -> dict:
    """按 (region, currency) 分组汇总，返回冻结 schema 的对账结果。"""
    stats: dict[tuple[str, str], dict[str, int]] = {}
    order_group: dict[str, tuple[str, str]] = {}
    for order in orders:
        key = (order["region"], order["currency"])
        bucket = stats.get(key)
        if bucket is None:
            bucket = stats[key] = {"paid_orders": 0,
                                   "gross_cents": 0,
                                   "refund_cents": 0}
        bucket["paid_orders"] += 1
        bucket["gross_cents"] += order["amount_cents"]
        order_group[order["order_id"]] = key

    for refund in refunds:
        key = order_group.get(refund["order_id"])
        if key is None:
            raise ValueError(f"unknown_order: {refund['order_id']}")
        stats[key]["refund_cents"] += refund["amount_cents"]

    totals = [
        {
            "region": region,
            "currency": currency,
            "paid_orders": bucket["paid_orders"],
            "gross_cents": bucket["gross_cents"],
            "refund_cents": bucket["refund_cents"],
            "net_cents": bucket["gross_cents"] - bucket["refund_cents"],
        }
        for (region, currency), bucket in stats.items()
    ]
    totals.sort(key=lambda item: (item["currency"], item["region"]))

    return {
        "schema_version": 1,
        "cancelled_orders": int(cancelled_orders),
        "totals": totals,
    }
