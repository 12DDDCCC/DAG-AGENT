# DAG-agent v3.2.1 技术预览版发布说明

## 发布范围

本发布面向单机、单服务、人工监督下的技术评估与二次开发。公开内容只包含当前源码、测试、冻结夹具、协议、使用文档和发布工程，不包含内部 `work/`、历史任务数据库、模型账本或 Obsidian 会话记忆。

发布渠道为 GitHub 源码与 GitHub Release，不发布到 PyPI。

## 入口

- `dag-agent-mcp`：v3 MCP stdio 服务；
- `dag-agent-full50`：50 节点、12 次受控拆分的零模型/真实执行基准；
- `dag-agent`：v1 历史兼容 CLI，无 checks 时的降级语义见 README。

从 README 开始；完整命令见 [使用指南](DAG-agent-技术预览版使用指南-2026-11-08.md) 和 [full50 指南](../techstack_full_v2/FULL_TASK_GUIDE.md)。

## Release 资产

最终 Release 应包含：

- `dag_agent-3.2.1-py3-none-any.whl`；
- `dag_agent-3.2.1.tar.gz`；
- `SHA256SUMS`；
- `RELEASE_MANIFEST.json`；
- 本发布说明、常规/slow 测试 JUnit，以及 wheel/sdist 安装验收 JSON。

制品必须来自验收通过的固定来源提交。实际文件名、大小、SHA256、测试日志和支持平台以最终验收报告为准；未登记前不得沿用旧版制品摘要。

## 当前边界

- 技术预览，不宣称生产级或长期无人值守；
- 非 OS 沙箱，不执行不可信任务或检查脚本；
- 零模型全链成功不等于真实供应商或业务事实质量验收；
- 预算是停派阈值，不是最终账单硬封顶；
- 固定模板生长不等于通用自由规划；
- 支持平台以 CI 与最终验收实际结果为准。

安全报告见 [SECURITY.md](../SECURITY.md)，贡献规则见 [CONTRIBUTING.md](../CONTRIBUTING.md)。
