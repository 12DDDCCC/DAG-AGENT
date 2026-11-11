# DAG-agent v3.2.1（技术预览）

DAG-agent 把多 Agent 协作中的任务图、执行状态、交付文件和机器验收外置到 SQLite 账本：调度器运行一次性执行会话，验收通过后保存不可变工件快照，Obsidian 看板是可删除、可重建的投影。

当前公开版本适合**单机、单服务、人工监督下的技术评估与二次开发**。它不是操作系统沙箱，也不宣称生产级无人值守能力。

## 三个入口

| 命令 | 用途 | 任务库 |
|---|---|---|
| `dag-agent-mcp` | **推荐**。v3 MCP stdio 服务，提供 29 个 `dag3_*` 工具、调度、验收、恢复和 Obsidian 投影 | v3 SQLite |
| `dag-agent-full50` | 固定模板的全流程基准：3 个初始节点，经 12 次受控拆分形成 50 节点 | 独立运行目录 |
| `dag-agent` | v1 历史兼容 CLI；无 checks 时会按陈述与 summary 直接通过，不等同 v3 的 fail-closed 机器验收 | v1 文件目录 |

三个入口的任务库不互通。需要 v3 验收语义时请使用 `dag-agent-mcp`。

## 安装

要求 Python 3.11+。零模型模式不需要 Node.js、API Key 或任何付费服务。

### 从源码

下载 GitHub 源码并进入项目根目录，PowerShell 示例：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install .
```

开发及运行测试：

```powershell
.\.venv\Scripts\python.exe -m pip install ".[dev]"
```

### 从 GitHub Release wheel

下载 Release 中的 `dag_agent-3.2.1-py3-none-any.whl`，然后在其所在目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install .\dag_agent-3.2.1-py3-none-any.whl
dag-agent-full50 --help
```

本项目当前不通过 PyPI 发布，因此不要以 `pip install dag-agent` 作为安装说明。

## 五分钟零模型运行

在源码包中，或安装 wheel 后，均可直接运行：

```powershell
$env:PYTHONUTF8 = "1"
dag-agent-full50 --mode zero-model --output .\demo-output
```

也可以使用模块入口：

```powershell
python -m techstack_full_v2.run_full --mode zero-model --output .\demo-output
```

每次使用一个新的空输出目录。成功时退出码为 0，`demo-output\run_results.json` 记录：50 节点、12 次拆分、全部 accepted、`done/machine`，以及全部验收检查结果。未传 `--output` 时，结果写到当前工作目录下的 `dag-agent-runs\full50\`，不会写入 Python 安装目录。

详细任务包说明见 [FULL_TASK_GUIDE.md](techstack_full_v2/FULL_TASK_GUIDE.md)，完整配置与恢复说明见 [技术预览版使用指南](outputs/DAG-agent-技术预览版使用指南-2026-11-08.md)。

## 使用 v3 MCP

安装后，在宿主 Agent 的 MCP 配置中注册命令，并显式指定可写数据目录：

```json
{
  "mcpServers": {
    "dag-agent-v3": {
      "command": "C:\\path\\to\\.venv\\Scripts\\dag-agent-mcp.exe",
      "env": {
        "DAG_AGENT_V3_HOME": "D:\\dag-agent-data\\v3"
      }
    }
  }
}
```

将示例路径替换为你的可写目录。未设置时，v3 默认使用用户目录 `~/.dag-agent/v3/missions`；v1 使用 `~/.dag-agent/v1/missions`。

最小使用循环：

```text
dag3_create_mission(name, brief, template="research")
dag3_add_node(...) / dag3_decompose(...)
dag3_run_start(mid, real=False)              # 零模型调度；真实执行见使用指南
dag3_run_status(mid)
dag3_run_stop(mid, mode="drain" | "kill")
dag3_board(mid)                               # 返回看板实际路径
```

自动生长 `auto_split=True` 默认开启，可在 `dag3_run_start` 中关闭。供应商额度、余额或认证暂停后，先人工处置，再以 `dag3_run_start(..., provider_pause_ack=True)` 恢复；后端预检失败不会清除暂停状态。

## 运行测试

在完整公开源码中：

```powershell
python -m pytest tests techstack_full_v2/tests techstack_research_v2/tests -m "not slow" --basetemp=work/tmp-pytest/public -q
python -m pytest tests techstack_full_v2/tests techstack_research_v2/tests -m slow --basetemp=work/tmp-pytest/public-slow -q
python scripts/publication_scan.py --self-test
```

发布验收与覆盖规则见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 目录

```text
DAG-agent/
├── mcp_server_v3.py          # v3 MCP stdio 入口
├── dag_agent/                # 状态机、验收、调度、后端、恢复与 Obsidian 投影
├── techstack_full_v2/        # 50 节点固定模板任务、检查器与恢复驱动
├── techstack_research_v2/    # 六节点数据库任务包与测试
├── tests/                    # 核心、回归与公开冻结夹具
├── scripts/                  # 扫描、导出、严格核验与安装后验收
├── 协议/                     # 工件、输入绑定与提交身份协议
├── outputs/                  # 当前公开使用指南与发布说明
├── DESIGN.md / PROTOCOL.md / AUDITOR.md
└── SECURITY.md / CONTRIBUTING.md / LICENSE
```

## 边界

- 命令白名单、路径检查、快照和提示词约束**不是 OS 沙箱**；真实执行者仍可能访问其进程权限范围内的资源。
- 考卷体检只能拒绝部分无效检查，不证明业务验收充分；关键任务仍需独立内容复核。
- 固定模板生长不等于通用自由规划。真实模型输出、事实来源和最终业务质量需要单独审查。
- 预算和调用次数参数是调度停派阈值，不是供应商最终账单的硬封顶。
- Windows 是本轮主要验收环境；其他平台以 CI 实际通过项为准。
- `dag-agent-full50` 的零模型成功证明机制链路，不证明真实供应商在当前版本已重新验收。

设计与协议见 [DESIGN.md](DESIGN.md)、[PROTOCOL.md](PROTOCOL.md)、[AUDITOR.md](AUDITOR.md)。许可证为 [MIT](LICENSE)。
