# -*- coding: utf-8 -*-
"""N06 报告渲染模块（订单-退款对账）。

接口（冻结）：
- render(summary: dict) -> str

summary 结构（schema_version=1）：
    {
      "schema_version": 1,        # int，汇总结构版本，固定为 1
      "cancelled_orders": int,    # 取消订单数
      "totals": [                 # 分组小计，按 (currency, region) 升序
        {"region": str, "currency": str, "paid_orders": int,
         "gross_cents": int, "refund_cents": int, "net_cents": int},
        ...
      ]
    }

行为：
- 返回可读中文报告字符串：首两行为报告头（结构版本、取消订单数），
  随后每个 (region, currency) 分组恰占一行，展示支付订单数与
  总额/退款/净额（单位：分，int 原样输出，不做币种最小单位换算）；
- totals 为空列表时输出"无分组小计"说明行，不抛异常；
- 输入明显不合结构（summary 非 dict、totals 非 list）-> TypeError；
  必需键缺失 -> KeyError：宁可响亮失败，不静默渲染错误数据。

仅使用 Python 标准库。
"""
from __future__ import annotations

__all__ = ["render"]


def render(summary: dict) -> str:
    """将对账汇总 summary 渲染为中文报告字符串，每分组一行。"""
    if not isinstance(summary, dict):
        raise TypeError(
            f"summary 必须为 dict，实际为 {type(summary).__name__}")
    totals = summary["totals"]
    if not isinstance(totals, list):
        raise TypeError(
            f"totals 必须为 list，实际为 {type(totals).__name__}")

    lines = [
        f"订单-退款对账报告（schema_version={summary['schema_version']}）",
        f"取消订单总数：{summary['cancelled_orders']}",
        f"分组小计（共 {len(totals)} 组）：",
    ]
    for t in totals:
        lines.append(
            "{region}/{currency}：支付订单 {paid_orders} 单，"
            "总金额（分）{gross_cents}，退款（分）{refund_cents}，"
            "净额（分）{net_cents}".format(**t)
        )
    if not totals:
        lines.append("（无分组小计）")
    return "\n".join(lines)


if __name__ == "__main__":  # 手工查看示例报告
    import sys
    try:
        sys.stdout.reconfigure(errors="replace")  # Windows GBK 控制台兜底
    except (AttributeError, ValueError):
        pass
    print(render({
        "schema_version": 1, "cancelled_orders": 0,
        "totals": [{"region": "north", "currency": "CNY", "paid_orders": 1,
                    "gross_cents": 100, "refund_cents": 0,
                    "net_cents": 100}],
    }))
