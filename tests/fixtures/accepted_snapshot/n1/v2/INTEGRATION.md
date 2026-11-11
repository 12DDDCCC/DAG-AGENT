# INTEGRATION — 订单-退款对账 CLI 最终交付整合说明

本文件由执行器G（最终交付整合节点）撰写，说明 bundle 九件交付物各自的
来源（`inputs/` 下哪个上游来源目录）与用途，以及 bundle 的运行方式。

## 1. 上游来源目录对照

`inputs/` 下播种了三个已验收快照（分别对应三个整合阶段）：

| 来源目录 | 对应阶段 | 快照内容 |
| --- | --- | --- |
| `inputs/n2` | 输入整合 | loader.py、validation.py、data/、INTEGRATION.md |
| `inputs/n6` | 计算整合 | 输入整合全部文件 + reconcile.py |
| `inputs/n8` | 交付整合 | 计算整合全部文件 + render.py、main.py、README.md |

同名文件在多个来源出现时均为逐字节相同（MD5 一致），不存在内容冲突，
故无需取舍；本次整合未对任何上游内容做修改。

## 2. 九件交付物清单与来源

| # | 文件 | 来源 | 用途 |
| --- | --- | --- | --- |
| 1 | `loader.py` | `inputs/n2`（输入整合；n6/n8 中副本逐字节相同） | CSV 导入模块（N02）：`load_orders` / `load_refunds`，容忍 UTF-8 BOM 与引号字段，定义 `InputError` 与 13 个冻结错误码的载体。 |
| 2 | `validation.py` | `inputs/n2`（输入整合；n6/n8 中副本逐字节相同） | 业务校验模块（N03）：`validate(orders, refunds)` 按 froze 顺序完成 13 项规则校验，输出 paid 订单、合法退款（金额转 int）与取消订单数。 |
| 3 | `reconcile.py` | `inputs/n6`（计算整合；n8 中副本逐字节相同） | 汇总计算模块（N04）：`reconcile()` 按 (region, currency) 分组整数核算 paid_orders / gross_cents / refund_cents / net_cents，产出 schema_version=1 的 summary。 |
| 4 | `render.py` | `inputs/n8`（交付整合，仅此来源含该文件） | 报告渲染模块（N06）：`render(summary)` 把 summary 渲染为中文可读报告（每分组一行）。 |
| 5 | `main.py` | `inputs/n8`（交付整合，仅此来源含该文件） | CLI 入口（N05）：真实调用 loader→validation→reconcile，成功退出 0 并写 UTF-8 JSON；非法输入退出 2 且 stdout 输出单行 error JSON。 |
| 6 | `README.md` | `inputs/n8`（交付整合，仅此来源含该文件） | 使用说明：功能概述、命令行用法、输入格式、13 个错误码一览与运行示例。 |
| 7 | `data/orders.csv` | `inputs/n2`（输入整合；n6/n8 中副本逐字节相同） | 样例订单数据 v1（14 行，north/CNY 组 gross=30000）。 |
| 8 | `data/refunds.csv` | `inputs/n2`（输入整合；n6/n8 中副本逐字节相同） | 样例退款数据 v1（8 行，north/CNY 组退款合计 4000）。 |
| 9 | `INTEGRATION.md` | 本节点（执行器G）撰写 | 即本文件：九件交付物的来源与用途对照、bundle 运行方式。 |

注：`inputs/*/INTEGRATION.md` 为上游 driver v2 机械生成的溯源说明
（三者内容相同），按任务简报要求由本文件在工作目录根承担交付版
INTEGRATION.md 职责。

## 3. bundle 运行方式

在 bundle 根目录（本目录）执行：

```
python main.py --orders data/orders.csv --refunds data/refunds.csv --out summary.json
```

- 合法输入：退出码 0，生成 UTF-8 编码的 `summary.json`
  （`ensure_ascii=False`，顶层含 `schema_version`、`cancelled_orders`、
  按 (currency, region) 升序的 `totals` 数组）。
- 非法输入：退出码 2，不产出 `summary.json`，stdout 输出单行
  `{"error":"<错误码>","message":"<可选说明>"}`。
- 仅依赖 Python 3 标准库，无第三方依赖。
- 对随附样例数据 v1，`summary.json` 中 north/CNY 组
  `net_cents = 26000`（gross 30000 − refund 4000）。
- `render.py` 可独立运行（`python render.py`）打印示例报告，或经
  `import render; render.render(summary)` 复用；`main.py` 本身不调用
  render，summary 直接落盘为 JSON。
