# 数据库冒烟交付契约 v1

交付UTF-8。输入在 `inputs/<来源节点>/`，通过查找文件名定位，不猜节点编号。输入和上游交付不得改写。没有可靠证据时报告未完成，不编造数字。

## 技术分析

每技术交付 `<technology>-analysis.md`、`<technology>-evidence.json`，technology为postgresql/clickhouse。
Markdown必须有二级标题：技术概述、核心特性、性能与边界、优势、劣势、适用场景、成本。JSON全部证据在正文以 `[@证据ID]` 引用；未知ID拒绝。

JSON结构示例（以下不是实际性能或报价）：

```json
{
  "schema_version":1, "technology":"postgresql", "mode":"real", "review_status":"unverified",
  "facts":[{
    "id":"pg-capability", "kind":"capability",
    "claim":"至少30字符、可独立核对的具体主张；这里仅展示格式，不是实际研究证据",
    "basis":"published",
    "source":{"url":"https://实际一手来源页面", "title":"具体文档标题", "locator":"文档章节或表格定位", "accessed_on":"2026-11-05"}
  }],
  "costs":[{"id":"compute","item":"计算资源月度成本","quantity":"720","quantity_unit":"小时","unit_price":"0.50","monthly_total":"360.00","period":"month","currency":"CNY","source_id":"pg-capability"}],
  "limitations":["至少20字符，说明来源适用条件、未测项目与估算缺口。"]
}
```

facts至少3条，覆盖capability、benchmark、case。ID跨技术唯一。来源必须有HTTPS URL、标题、具体定位、访问日期。benchmark还需 `metric: {"value":"1200","unit":"QPS","workload":"至少20字符说明查询、数据和并发条件","environment":"至少20字符说明硬件、软件版本和测量条件"}`。性能值为正，单位限QPS/TPS/ms/MB/s/GB/s。

costs非空，数量/金额用十进制字符串，quantity×unit_price按分四舍五入等于monthly_total；source_id指向包含价格或估算依据的证据。来源是否支持该主张仍需独立复核。真实模式basis限published/measured；零模型模式为zero-model、basis=synthetic、URL主机以.invalid结尾，正文标“合成测试数据，不用于技术决策”。

## 比较

交付database-comparison.md和database-comparison.json。正文引用两技术全部证据。JSON结构：

```json
{
  "schema_version":1,
  "input_sha256":{"postgresql-analysis.md":"SHA256","postgresql-evidence.json":"SHA256","clickhouse-analysis.md":"SHA256","clickhouse-evidence.json":"SHA256"},
  "criteria":[{"requirement_id":"daily_ingest","assessment":"至少40字符的适用性比较和证据局限，不能凭数字数量决定结论。","evidence":{"postgresql":["pg-capability"],"clickhouse":["ch-capability"]}}],
  "annual_cost_cny":{"postgresql":"月度明细合计乘12","clickhouse":"月度明细合计乘12"},
  "recommendation":{"technology":"postgresql","status":"provisional","rationale":"至少80字符说明需求、取舍、证据缺口和进一步验证条件。"}
}
```

criteria恰好覆盖requirements.json全部requirement_id；输入哈希来自绑定的已验收文件，不能自行制作上游替代品。年度费用等于各技术全部月度明细合计乘12。

## 研究整合与根整合

研究父节点原样汇集4份分析交付、2份比较交付，共6文件。根再原样汇集这6文件，以及requirements.json、proposal-protocol.md、deliverable-schema.md、input-provenance.json，新增：

- `99-数据库选型报告.md`：二级标题执行摘要、需求与决策、成本与风险、实施步骤、验证边界，引用两技术全部证据。测试模式标明“合成测试数据，不用于技术决策”。
- `review.json`：严格为 `{"machine_scope":"structure_consistency_traceability","content_review":"pending","mode":"real"}`，零模型时mode为zero-model。

独立复核在执行流程外记录。mission done/machine只代表契约通过，不表示研究事实已核实。
