# DAG-agent full50 任务包指南

## 这个任务包是什么

`techstack_full_v2` 是一个固定模板的全流程基准：以实时数据分析平台技术选型为业务样例，从 **3 个初始节点**开始，经 **12 次真实提案应用**新增 47 个节点，最终形成并验收 **恰好 50 个节点**。

它验证任务图生长、依赖输入、执行回执、机器验收、根干净重放、并行调度和跨进程恢复。它不等于通用自由规划，也不自动证明研究内容中的事实与选型结论正确。

## 零模型运行

要求 Python 3.11+。安装项目后，在任意可写目录执行：

```powershell
$env:PYTHONUTF8 = "1"
dag-agent-full50 --mode zero-model --output .\zero-model-demo
```

源码中也可使用：

```powershell
python -m techstack_full_v2.run_full --mode zero-model --output .\zero-model-demo
```

每次使用新的空目录。默认墙钟为 21600 秒，可用正数 `--timeout` 调整：

```powershell
dag-agent-full50 --mode zero-model --timeout 300 --output .\zero-model-300s
```

成功结果：

- CLI 退出码为 0；
- `<输出目录>/run_results.json` 中 `success=true`；
- `node_count=50`、`applied_splits=12`；
- 50 个节点均 accepted，mission 为 `done/machine`；
- 根干净重放及结果中的全部 checks 通过。

主要文件：

```text
<输出目录>/
├── mission.db                 # 权威状态、事件、绑定、回执与预算账
├── run_results.json           # 零模型汇总与验收判据
├── frozen/                    # 冻结任务结构、检查器和输入
├── accepted/                  # 各节点已验收的不可变工件快照
├── board/                     # 可重建的 Obsidian 投影
└── work/                      # 各节点执行工作区
```

## 真实执行（可选，会产生供应商用量）

真实模式需要另行安装并配置执行 CLI。当前后端名为 `claude-cli`、`pi-cli`、`zcode-cli`；配置方法和已验证边界见 [技术预览版使用指南](../outputs/DAG-agent-技术预览版使用指南-2026-11-08.md)。

`--backend-options` 是 **JSON 文件路径**，不是内联 JSON，也不应在其中直接写密钥。例如 `pi-options.json` 只包含：

```json
{"timeout": 1500}
```

然后运行：

```powershell
dag-agent-full50 --mode real --backend-name pi-cli `
  --backend-options .\pi-options.json --output .\real-demo
```

真实模式结果写入 `<输出目录>/real_run_results.json`。费用参数与回执只驱动平台停派和记账，不构成供应商账单硬保证。当前 `3.2.1` 公开候选的真实供应商重跑不是零模型发布门禁的替代项；历史真实运行不能直接证明修订后的版本。

## 跨进程恢复

原驱动中断后，`--output` 必须指向原运行目录：

```powershell
dag-agent-full50 --resume --output .\real-demo
```

恢复会核对冻结检查器、输入、模板、代际、后端及不可变墙钟，沿用 accepted、预算和拆分额度；已完成任务直接幂等返回，不重新调用执行者。结果写入 `resume_results.json`。

若发生额度、余额或认证类供应商暂停，先人工处置，再显式确认：

```powershell
dag-agent-full50 --resume --output .\real-demo --pause-ack
```

后端预检失败时不会清除暂停。`resume` 未显式提供后端时会沿用原运行配置；显式传空配置也会按配置漂移拒绝。

## 停止边界

标准 `dag-agent-full50` CLI 没有独立的跨进程 `stop` 子命令。驱动在墙钟到期或异常收尾时调用同进程 `RunManager.stop()`；外部强杀驱动不自动证明其模型 CLI 子进程已经退出，恢复前须检查残留进程。

自定义同进程驱动只能复用实际启动该任务的 `RunManager` 句柄；不能新建一个空管理器来控制另一进程内的调度线程。`drain` 不再派发并等待在途收敛，`kill` 还会取消在途尝试并请求后端终止进程树。

## 交付核验

数据库是首选权威锚。在待验根发布物副本目录中运行冻结检查器，并使用绝对路径指向任务材料：

```powershell
Set-Location <独立根发布物副本目录>
python <原运行目录>\frozen\checks.py --stage root `
  --manifest <原运行目录>\frozen\manifest.json `
  --trusted-db <原运行目录>\mission.db
```

无数据库的离线重放需要由平台或审计方预先生成并受控保存 `trusted-export.json` 与对应 digest。生成入口是现有 `techstack_full_v2.checks.export_trusted(db_path, out_json_path, out_digest_path)`；digest 必须放在待验执行者不可写的位置。纯离线文件可被整体伪造，因此位置与保管边界是信任链的一部分。

## 不应从成功结果外推什么

- 不证明检查器覆盖了所有业务错误；
- 不证明模型引用的来源真实存在或结论适合生产；
- 不证明多机分布式调度；
- 不证明任何预算都是最终费用硬上限；
- 不证明当前修订已完成新的真实供应商验收。
