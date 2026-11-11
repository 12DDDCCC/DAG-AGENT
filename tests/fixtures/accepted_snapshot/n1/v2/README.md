# 订单-退款对账工具 使用说明

## 1. 功能概述

本工具读取订单 CSV 与退款 CSV 两个输入文件，完成配对与业务校验，并按「币种 × 区域」分组核算净额，产出 UTF-8 编码的 JSON 汇总文件（如 `summary.json`）。

## 2. 运行环境

- Python 3，标准库实现，无第三方依赖。

## 3. 命令行用法（冻结接口）

```
python main.py --orders O.csv --refunds R.csv --out summary.json
```

| 参数 | 含义 |
| --- | --- |
| `--orders` | 订单 CSV 文件路径（必需） |
| `--refunds` | 退款 CSV 文件路径（必需） |
| `--out` | 汇总 JSON 输出路径（必需，如 `summary.json`） |

## 4. 输入文件格式

**orders.csv** 表头（5 列，必须齐全）：

```
order_id,region,currency,amount_cents,status
```

**refunds.csv** 表头（4 列，必须齐全）：

```
refund_id,order_id,currency,amount_cents
```

**业务规则（冻结）：**

- `currency` 仅允许 `CNY` / `USD`；
- `status` 仅允许 `paid` / `cancelled`；
- `amount_cents` 必须是 ASCII 非负十进制整数（单位：分）；
- 退款必须引用订单表中存在的 `order_id`，且币种须与所属订单一致；
- `cancelled` 订单不允许发起退款；同一订单的退款合计不得超过订单金额；
- `order_id`、`refund_id` 在各自文件内不得重复、不得为空；订单的 `region` 不得为空；
- 支持 UTF-8 BOM 与标准 CSV 引号字段。

## 5. 成功与失败行为

- **有效输入**：退出码 `0`，产出 UTF-8 编码的 JSON 汇总文件，顶层含 `schema_version`、`cancelled_orders` 与按 `(currency, region)` 升序排列的 `totals` 数组（每项含 `region` / `currency` / `paid_orders` / `gross_cents` / `refund_cents` / `net_cents`）。
- **非法输入**：退出码 `2`，**不产出** `summary.json`，stdout 输出单行 JSON：`{"error":"<错误码>","message":"<可选说明>"}`（`message` 为可选字段）。

## 6. 错误码一览（13 个）

| 错误码 | 说明 |
| --- | --- |
| `order_id_duplicate` | 订单表中同一 order_id 出现了多行。 |
| `refund_id_duplicate` | 退款表中同一 refund_id 出现了多行。 |
| `unknown_order` | 退款引用的 order_id 在订单表中不存在。 |
| `refund_on_cancelled` | 对已取消（cancelled）状态的订单发起了退款。 |
| `currency_mismatch` | 退款币种与其所属订单的币种不一致。 |
| `amount_invalid` | 金额不是 ASCII 非负十进制整数（单位：分）。 |
| `refund_exceeds_order` | 同一订单的退款合计金额超过了订单金额。 |
| `status_invalid` | 订单 status 不是 paid/cancelled 之一。 |
| `columns_missing` | CSV 缺少必需列（表头不齐全）。 |
| `currency_invalid` | currency 不是 CNY/USD 之一。 |
| `order_id_empty` | 订单行的 order_id 为空。 |
| `refund_id_empty` | 退款行的 refund_id 为空。 |
| `region_empty` | 订单行的 region 为空。 |

## 7. 运行示例（可直接复制）

**第一步：准备样例输入。**

`orders.csv`：

```
order_id,region,currency,amount_cents,status
o1,north,CNY,100,paid
o2,south,USD,200,cancelled
```

`refunds.csv`：

```
refund_id,order_id,currency,amount_cents
r1,o1,CNY,40
```

**第二步：合法输入，运行成功。**

```
python main.py --orders orders.csv --refunds refunds.csv --out summary.json
echo $?
```

退出码为 `0`（Windows CMD 下可用 `echo %ERRORLEVEL%` 查看），`summary.json` 内容示例：

```json
{
  "schema_version": 1,
  "cancelled_orders": 1,
  "totals": [
    {"region": "north", "currency": "CNY", "paid_orders": 1, "gross_cents": 100, "refund_cents": 40, "net_cents": 60}
  ]
}
```

（`o2` 为已取消订单，计入 `cancelled_orders`，且因其组内无 paid 订单而不出现在 `totals` 中。）

**第三步：非法输入，退出码 2 示例。**

把 `refunds.csv` 改为引用不存在的订单后再次运行：

```
refund_id,order_id,currency,amount_cents
r9,o404,CNY,10
```

```
python main.py --orders orders.csv --refunds refunds.csv --out summary.json
```

行为：退出码 `2`，stdout 输出单行 JSON（`message` 内容以实际实现为准）：

```
{"error":"unknown_order","message":"o404"}
```

且本次运行不产出 `summary.json`。

## 8. 退出码总结

| 退出码 | 含义 |
| --- | --- |
| `0` | 成功，已产出 `--out` 指定的 UTF-8 JSON 汇总文件 |
| `2` | 输入非法，stdout 给出 `{"error":...,"message":...}`，不产出汇总文件 |
