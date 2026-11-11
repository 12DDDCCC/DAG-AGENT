# 数据库冒烟拆分提案协议

对应 dag_agent.autogrow.parse_proposal。决定拆分时，在当前 attempt 目录创建 `.dag/proposal.json`，正常结束会话；此次冒烟此时不要生成研究父节点声明的6份正式交付。父整合会话只汇集结果，不再拆分。

顶层11字段均必填。身份字符串填platform、版本整数填-1，由平台核对回填。proposal_id自行取唯一非空字符串。完整合法示例：

```json
{
  "schema_version":"1",
  "proposal_id":"db-research-unique-001",
  "source_attempt_id":"platform",
  "parent_node_id":"platform",
  "goal_version":-1,
  "node_revision":-1,
  "check_version":-1,
  "check_digest":"platform",
  "input_binding_hash":"platform",
  "reason":"分别研究两种数据库并基于已验收证据进行比较",
  "children":[
    {"local_id":"pg","title":"PostgreSQL证据分析","statement":"按绑定需求研究PostgreSQL，提交正文与结构化证据。","input_refs":[],"depends_on":[],"template_id":"db-smoke-postgresql-v1","template_params":{},"deliverables":["postgresql-analysis.md","postgresql-evidence.json"]},
    {"local_id":"ch","title":"ClickHouse证据分析","statement":"按绑定需求研究ClickHouse，提交正文与结构化证据。","input_refs":[],"depends_on":[],"template_id":"db-smoke-clickhouse-v1","template_params":{},"deliverables":["clickhouse-analysis.md","clickhouse-evidence.json"]},
    {"local_id":"compare","title":"数据库证据对比","statement":"基于两份已验收分析比较需求覆盖和年度成本。","input_refs":[],"depends_on":["pg","ch"],"template_id":"db-smoke-comparison-v1","template_params":{},"deliverables":["database-comparison.md","database-comparison.json"]}
  ]
}
```

子字段白名单：local_id/title/statement/input_refs/depends_on/template_id/template_params/check_hint/deliverables。禁止artifacts、check_cmd、deps。depends_on引用提案内更早的local_id，不是数字下标或全局ID。此次模板只接受空template_params，考卷由宿主预注册。

父认领的外部输入由平台传给孩子；比较再依赖两个分析孩子。导入资料来自宿主，不是模型产出。协议、schema及需求在inputs/<来源>/可读。

预期一次拆分、3个孩子、最终6节点。平台每任务5次拆分、每提案2–5孩子、深度5的护栏不变。模板只注册在驱动进程，原MCP服务不会自动加载。拒绝或未拆分需如实报告，宿主不能代写提案冒充真实自主性。
