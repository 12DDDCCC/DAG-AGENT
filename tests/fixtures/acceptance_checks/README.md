# 多agent协同验收项目

目标：多个真实一次性agent共同实现**订单-退款对账CLI**，通过独立功能考卷，并验证并行、依赖交接、OR替代、故障恢复、真实MCP干预、跨父/OR赢家增量重验和干净重放。

先读 [SPEC.md](SPEC.md)。业务题目、任务图、故障注入及八项硬门槛均已冻结在该文件。

交付文件：
- `fixtures/v1/`：14订单、8退款及预期JSON。
- `fixtures/v2/`：修订数据及不同预期JSON。
- `fixtures/freeze_manifest.json`：冻结文件摘要。
- `oracle.py`：独立CSV校验及SQLite聚合答案。
- `judge.py`：20个实际功能用例（2冻结正例+5本次随机正例+13负例）。

候选由DAG-agent执行者交付 `bundle/main.py` 及自包含模块。接口：
```powershell
python bundle/main.py --orders orders.csv --refunds refunds.csv --out summary.json
```

判卷器自检（不代表候选或协同通过）：
```powershell
Set-Location 'D:/AI/DAG-agent/outputs/多agent协同验收项目'
& 'D:/Anaconda/python.exe' -B judge.py --self-check
```

候选功能判卷：
```powershell
& 'D:/Anaconda/python.exe' -B judge.py --bundle 'D:/path/to/accepted/bundle' --evidence 'D:/AI/DAG-agent/work/your-run/functional.json'
```

`functional.json`是功能结果，八项协同门槛仍需MCP、真实会话、依赖、故障和修订证据。禁止把self-check或20/20功能结果单独称为“完整协同通过”。

判卷器先按seed生成随机用例，seed归档；每次运行保留独立证据。输入/源码摘要检查只能发现观察点的变更，本地候选子进程不是OS沙箱。仅运行已审查的本地候选代码。

本项目包已完成判卷器自检，并确认明显错误候选被拒、缺候选被拒；正向oracle测试适配器仅用于验证判卷管道。**真实多agent项目尚未执行。** 原框架的独立缺陷复核见 [v3.2复核报告](../DAG-agent-v3.2-Codex独立验收复核.md)。
