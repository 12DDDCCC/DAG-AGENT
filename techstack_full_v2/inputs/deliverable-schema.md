# 50 节点选型研究交付契约 v1

交付 UTF-8。输入在 `inputs/<来源节点>/`，按文件名查找定位，不猜节点编号。输入与上游交付不得改写。没有可靠证据时如实报告，不编造数字。所有数值用十进制字符串。

## 证据文件（`<subject>-evidence.json` 系列）

```json
{
  "schema_version": 1,
  "subject": "postgresql-feature",
  "mode": "real",
  "review_status": "unverified",
  "facts": [{
    "id": "pgf-cap-001",
    "kind": "capability",
    "claim": "至少30字符、可独立核对的具体主张（本行是格式说明，不是实际证据）",
    "basis": "published",
    "source": {"url": "https://实际一手来源页面", "title": "具体文档标题",
               "locator": "文档版本+章节/表格定位", "accessed_on": "2026-11-07"}
  }],
  "limitations": ["至少20字符：来源适用条件、未测项目、估算缺口。"]
}
```

- `subject` 必须等于节点 subject（如 `postgresql-feature`、`cross-total-cost`）。
- facts 至少 3 条，ID 全局唯一（`^[a-z0-9][a-z0-9_-]{2,63}$`，建议前缀含 subject 缩写）。
- source 必须 HTTPS URL + 标题 + 可定位信息（版本+章节）+ 访问日期；主机不得为 .invalid/localhost。
- benchmark 类 fact 增加 `metric: {"value":"1200","unit":"QPS","workload":"≥20字符负载条件","environment":"≥20字符硬件与版本"}`，单位限 QPS/TPS/ms/MB/s/GB/s；官方公布与第三方测量在 claim 或 source.title 中区分。
- cost 行（`costs`）：`quantity × unit_price = monthly_total`（分四舍五入）、`period: "month"`、`currency: "CNY"`、`source_id` 指向已有 fact、注明价格日期/地区/未计项目。

## 各节点正文（Markdown 二级标题必需）

| 节点类型 | 正文文件 | 必需二级标题 |
|---|---|---|
| `<tech>-feature` | `<tech>-feature.md` | 核心机制、适用与不适用场景、接口与运维边界 |
| `<tech>-performance` | `<tech>-performance.md` | 指标与来源、适用条件、不适用条件 |
| `<tech>-case` | `<tech>-case.md` | 案例与来源、采用原因、迁移限制 |
| `<tech>-cost` | `<tech>-cost.md` | 计费口径、成本计算、未计项目 |
| `<tech>-integration`（父） | `<tech>-integration.md` | 整合结论、证据索引 |
| `<domain>-comparison` | `<domain>-comparison.md` | 逐项比较、成本对比、初步推荐 |
| `<domain>-integration`（父） | `<domain>-integration.md` | 领域结论、证据索引 |
| `cross-<name>` | `cross-<name>.md` | 分析、依据与来源、限制 |
| `cross-integration`（父） | `cross-integration.md` | 整合结论、证据索引 |

正文必须用 `[@证据ID]` 引用其 evidence.json 中的全部 fact；不得出现未知 ID。

## 整合父（技术/领域/跨领域）

`<x>-integration.json`：
```json
{"schema_version":1, "subject":"<x>", "mode":"real", "review_status":"unverified",
 "upstream":{"postgresql-feature.md":"SHA256", "...":"..."},
 "conclusions":["至少30字符结论"], "open_questions":["未决问题，可空"]}
```
`upstream` 必须列出全部整合文件名与 SHA256；正文 md 引用全部上游证据 ID；整合不得改写上游（平台按 inputs 快照哈希核对）。

## 比较（`<domain>-comparison.json`）

```json
{"schema_version":1, "subject":"database-comparison", "mode":"real", "review_status":"unverified",
 "input_sha256":{"postgresql-integration.json":"...","clickhouse-integration.json":"...","mongodb-integration.json":"..."},
 "criteria":[{"requirement_id":"daily_ingest","assessment":"≥40字符逐项比较与证据局限","evidence":{"postgresql":["pgf-..."],"clickhouse":["chf-..."],"mongodb":["mgf-..."]}}],
 "recommendation":{"ranking":["clickhouse","postgresql"],"status":"provisional","rationale":"≥80字符：需求匹配、取舍、证据缺口"}}
```
criteria 的 requirement_id 必须覆盖 requirements.json 全部五条；evidence 的技术键=本领域全部技术。

## 跨领域研究（`cross-<name>-evidence.json`）

`subject`=`cross-<name>`；`facts` 之外增加 `domain_refs`: {"database":["..."],"message-queue":["..."],"cache":["..."]}——必须引用三领域上游真实证据 ID（至少各 1 条）。`cross-total-cost` 的 costs 需给出 7 技术月度合计与年度换算（分四舍五入）。

## 根（root）

- `source-index.json`：`{"schema_version":1, "mode":"real", "sources":[{"id":"...","title":"...","url":"...","locator":"...","accessed_on":"..."}]}`——收录全部证据来源（按 URL+locator 去重）。
- `delivery-manifest.json`：`{"schema_version":1, "deliverables":{"<文件名>":"SHA256", ...}}`——覆盖全部非导入节点的交付文件（清单见任务总览）。
- `final-report.md`：必需二级标题：执行摘要、需求匹配结论、成本与风险、实施建议、限制与验证状态；引用关键证据 ID。
- `review.json`：恰好 `{"machine_scope":"structure_consistency_traceability","content_review":"pending","mode":"real"}`——不得冒充独立复核签收。
