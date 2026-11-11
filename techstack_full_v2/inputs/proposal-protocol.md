# 实时数据分析平台选型：50 节点拆分提案协议

对应 dag_agent.autogrow.parse_proposal（顶层 11 字段、platform/-1 占位符、depends_on 引用提案内更早声明的 local_id）。

## 何时写提案

只有当你的节点 **statement 明确要求"决定拆分"** 时才写提案：在当前 attempt 目录创建 `.dag/proposal.json`，然后正常结束会话。拆分节点不生成本节点声明的正式交付；整合会话不得再提案（父整合阶段只汇集）。

## 模板与孩子集合（偏离即被整批拒绝）

| 你的节点 | template_id | children 必须恰好为 |
|---|---|---|
| 总研究 | `full-domains-v1` | 4 孩子：local_id=`database`/`message-queue`/`cache`（template_params `{"domain":<同名>}`，depends_on 空）+ `cross`（template_id `full-cross-v1`，params `{}`，depends_on=`["database","message-queue","cache"]`） |
| 数据库领域 | `full-domain-v1`（params `{"domain":"database"}`） | 4 孩子：`postgresql`/`clickhouse`/`mongodb`（template_id `full-tech-v1`，params `{"technology":<同名>}`，deps 空）+ `database-compare`（template_id `full-domain-v1`，params `{"domain":"database","leaf":"compare"}`，deps=`["postgresql","clickhouse","mongodb"]`） |
| 消息队列领域 | `full-domain-v1`（params `{"domain":"message-queue"}`） | 3 孩子：`kafka`/`rabbitmq` + `message-queue-compare`（同上模式） |
| 缓存领域 | `full-domain-v1`（params `{"domain":"cache"}`） | 3 孩子：`redis`/`memcached` + `cache-compare`（同上模式） |
| 某技术研究（7 种） | `full-tech-v1`（params `{"technology":<tech>}`） | 4 孩子：`<tech>-feature`/`<tech>-performance`/`<tech>-case`/`<tech>-cost`（template_id `full-tech-v1`，params `{"technology":<tech>,"leaf":<aspect>}`，deps 全空、可并行） |
| 跨领域整合 | `full-cross-v1` | 5 孩子：`arch-dataflow`/`consistency-recovery`/`total-cost`/`deployment-ops`/`migration-roadmap`（template_id `full-cross-v1`，params `{"leaf":<同名>}`，deps 全空、可并行） |

叶子研究节点（特性/性能/案例/成本、比较、跨领域研究）**不写提案**：直接交付文件。

孩子 spec 字段：`local_id`/`title`/`statement`/`input_refs`/`depends_on`/`template_id`/`template_params`/`deliverables`（清单见 deliverable-schema.md）。禁止 artifacts、check_cmd、deps 字段。

## 完整合法示例（总研究节点）

```json
{
  "schema_version": "1",
  "proposal_id": "full50-research-001",
  "source_attempt_id": "platform",
  "parent_node_id": "platform",
  "goal_version": -1,
  "node_revision": -1,
  "check_version": -1,
  "check_digest": "platform",
  "input_binding_hash": "platform",
  "reason": "按冻结需求拆为三个领域研究与跨领域整合；三个领域可并行，跨领域基于三领域成果",
  "children": [
    {"local_id": "database", "title": "数据库领域研究", "statement": "研究 PostgreSQL/ClickHouse/MongoDB 并产出领域比较。", "input_refs": [], "depends_on": [], "template_id": "full-domain-v1", "template_params": {"domain": "database"}, "deliverables": []},
    {"local_id": "message-queue", "title": "消息队列领域研究", "statement": "研究 Kafka/RabbitMQ 并产出领域比较。", "input_refs": [], "depends_on": [], "template_id": "full-domain-v1", "template_params": {"domain": "message-queue"}, "deliverables": []},
    {"local_id": "cache", "title": "缓存领域研究", "statement": "研究 Redis/Memcached 并产出领域比较。", "input_refs": [], "depends_on": [], "template_id": "full-domain-v1", "template_params": {"domain": "cache"}, "deliverables": []},
    {"local_id": "cross", "title": "跨领域整合研究", "statement": "基于三领域已验收证据开展架构、一致性、成本、运维与迁移研究。", "input_refs": [], "depends_on": ["database", "message-queue", "cache"], "template_id": "full-cross-v1", "template_params": {}, "deliverables": []}
  ]
}
```

整合型孩子（领域/技术/跨领域父）的 deliverables 由模板按结构生成，提案里可留空数组。身份字段一律 platform/-1 占位。协议、schema 及需求在 `inputs/<来源>/` 可读；inputs 只读。
