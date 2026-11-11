# DAG-agent v3.2.1 更新日志

**功能版本起点**: 2026-11-02

**公开预览收口**: 2026-11-11

**主题**: 受控自动生长 + 可复现的公开安装与使用链

## 公开技术预览收口

- 统一包、模块、导出目录、清单和发布文档为 `3.2.1`；公开渠道为 GitHub 源码与 Release，不接 PyPI。
- MCP `dag3_run` / `dag3_run_start` 支持 `provider_pause_ack`，供应商暂停可经公开工具面安全恢复。
- v1/v3 默认数据目录迁至用户目录，full50 默认输出到调用者工作目录，避免写入安装树。
- Obsidian 节点档案支持结构化费用、unknown 和 Pi 会话身份。
- 发布扫描器具备独立自测和失败型退出码；导出器增加必需文件检查、来源提交及严格原始字节核验。
- README、full50 指南、可信重放、停止与恢复说明改为当前公开包实际入口；新增 SECURITY 与 CONTRIBUTING。
- 发布资产包括 wheel、sdist、SHA256SUMS 和 RELEASE_MANIFEST；以实际验收报告记录最终摘要。

## 功能变更：自动生长执行链路启用

## 🎯 核心变更

### 自动生长执行链路默认启用

实现真正的自主任务执行：执行者自动提案 → 自动分解 → 自动执行的完整闭环。

**关键修改**：

1. **`dag_agent/scheduler.py`**
   - `AutogrowConfig.enabled`: `False` → `True`
   - 注释更新：说明默认开启

2. **`mcp_server_v3.py`**
   - `dag3_run_start(auto_split)`: `False` → `True`
   - 工具文档更新：说明自动生长已默认启用

3. **`agent.md`**
   - 新增 v3.2.1 版本说明
   - 从"显式延期特性"移到"核心能力变更"
   - 详细说明自动生长机制和使用方式

4. **`DESIGN.md`**
   - 版本号更新至 v3.2.1
   - 说明自动生长执行链路默认启用

## 🔧 技术细节

### 自动生长机制

- **提案协议**: 执行者在工作目录写入 `.dag/proposal.json`
- **独立出题**: 
  - 可信模板路径（预置考卷包）
  - 独立出题路径（DeterministicPlannerStub 或真实 CLI 规划器）
- **权威应用**: 结构验证 → 考卷体检 → 单事务落地
- **护栏机制**:
  - 每任务默认≤5次自动拆分；full50 显式配置 12 次，平台允许范围为 1..50
  - 树深≤5层
  - 单提案2-5个孩子
  - 有限规划重试（防拒绝后反复触发）

### 控制接口

```python
# 启动任务（自动生长默认开启）
dag3_run_start(mid, auto_split=True)  # 现为默认值

# 查询提案队列
dag3_split_proposals(mid, status="queued")

# 提案状态：queued/applied/rejected/discarded
```

### 事件追踪

自动生长过程产生的事件：
- `split_proposed`: 提案被受理
- `split_rejected`: 提案违规被拒
- `split_duplicate`: 同源同内容幂等返回
- `split_discarded`: 父原考卷通过，无需拆分
- `split_activated`: 激活拆解，原执行位释放

## 📊 验证测试

新增 `test_autogrow_enabled.py` 验证套件：
- ✅ AutogrowConfig 默认启用
- ✅ Scheduler 正确应用 auto_split 配置
- ✅ MCP 工具 dag3_run_start 的 auto_split 默认为 True
- ✅ 自动生长基础设施完整（split_proposals 表）

## 🎓 使用指南

### 执行者视角

在任务执行过程中，如果发现需要拆分，创建提案文件：

```json
{
  "schema_version": "1",
  "proposal_id": "prop-xxx",
  "source_attempt_id": "platform",
  "parent_node_id": "platform",
  "goal_version": -1,
  "node_revision": -1,
  "check_version": -1,
  "check_digest": "platform",
  "input_binding_hash": "platform",
  "reason": "任务过大，需要拆分为子任务",
  "children": [
    {
      "local_id": "step1",
      "title": "子任务1",
      "statement": "子任务1的详细描述",
      "input_refs": [],
      "depends_on": [],
      "template_id": "doc-section-v1",
      "template_params": {"artifact": "step1.md"},
      "deliverables": ["step1.md"]
    },
    {
      "local_id": "step2",
      "title": "子任务2",
      "statement": "子任务2的详细描述",
      "input_refs": [],
      "depends_on": ["step1"],
      "check_hint": "验证 step2.md 存在且内容完整",
      "deliverables": ["step2.md"]
    }
  ]
}
```

保存到 `.dag/proposal.json`，平台会自动检测并处理。

### 宿主视角

```python
# 创建任务
result = dag3_create_mission("研究项目", "深度分析某技术领域")

# 启动运行（自动生长已默认开启）
dag3_run_start(
    mid=result["mission_id"],
    max_concurrency=2,
    real=True,
    auto_split=True  # 可省略，已是默认值
)

# 监控提案队列
proposals = dag3_split_proposals(result["mission_id"])
print(f"待审提案: {proposals['count']} 个")

# 查看任务状态
status = dag3_status(result["mission_id"])
```

## 🔄 向后兼容

- 旧代码显式传 `auto_split=False` 仍有效
- 默认行为变更：不传参数时自动生长开启
- 可通过参数关闭：`dag3_run_start(mid, auto_split=False)`

## 📝 文档更新

- ✅ agent.md: 核心能力变更说明
- ✅ DESIGN.md: 版本号更新
- ✅ mcp_server_v3.py: 工具文档更新
- ✅ test_autogrow_enabled.py: 新增验证测试

## 🎉 影响

此次更新实现了 DAG-agent 的核心设计目标：

> **执行者自动提案 → 自动分解 → 自动执行**

不再需要宿主 agent 手动调用 `dag3_decompose`，系统能够真正实现自主任务执行。

---

**下一步建议**：

1. 在真实场景中验证自动生长效果
2. 收集执行者提案质量反馈
3. 根据反馈调整护栏参数（max_splits_per_mission、max_depth 等）
4. 考虑接入真实 CLI 规划器（ClaudeCliPlannerAdapter）
