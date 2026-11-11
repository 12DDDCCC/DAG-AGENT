# 数据库六节点冒烟包

本包使用现有DAG-Agent核心，在独立驱动进程中注册可信业务模板。默认零模型；不会自动启动收费模型，也不会向已运行的MCP服务注册模板。

## 当前范围

根 → 资料导入（宿主） → 数据库研究 → 一次自动拆分为PostgreSQL、ClickHouse、比较；比较依赖两份分析。最终共6节点。

资料导入和研究均归属根，研究依赖资料导入。只有研究节点由提案生长孩子。全部最终交付以平台accepted快照为准，原输入按SHA256绑定。输入来源为host_import，不算模型产出。

零模型下提案和报告由FixtureBackend生成，只证明机制和契约；数据带“合成测试数据，不用于技术决策”标记，所有来源使用.invalid，不表示研究结论成立。

## 零模型运行

在仓库根目录执行：

```powershell
$env:PYTHONUTF8='1'
python techstack_research_v2/run_smoke.py
```

默认新建work/db-smoke-runs/zero-model-随机目录。指定目录必须为空，不会清理已有数据：

```powershell
python techstack_research_v2/run_smoke.py --mode zero-model --output work/my-db-smoke --timeout 120
python -m pytest techstack_research_v2/tests -q -p no:cacheprovider
```

驱动会依次执行：复制并冻结检查器/输入摘要 → 六类考卷preflight → 创建根/导入/研究3节点 → 宿主导入并验收输入 → 注册3个业务模板 → RunManager调度 → 自动提案与应用 → 子执行/比较/整合 → 最终检查及证据落盘。

## 输出与结果判断

运行目录包含：

- run_results.json：success、mode、各项checks、实际节点/依赖、资源状态、停止确认和错误。
- mission.db：平台权威状态，work/<node>/<attempt>/：执行工作区。
- accepted/<node>/v<version>/：已验收快照。root_published指出根交付目录。
- board/任务总览.md、board/当前任务图.canvas：Obsidian投影。
- frozen/checks.py、frozen/manifest.json、frozen/inputs/：本次可信考卷/输入副本。
- events.json、attempts.json、proposals.json、root_assessments.json：完整链路证据。

成功要求：恰好6节点、全部accepted、一次提案应用新增3节点、输入绑定正确、可信模板被选择、源attempt已释放、根原卷摘要保持、根干净重放通过、发布文件存在且哈希有效、停止确认完成。analysis_execution_overlap报告两个分析会话是否实际重叠，不据此承诺性能收益。

平台历史字段plan_backend即使走模板仍可能显示planner-stub。请同时核对提案template_id、plan_output_json中的冻结考卷和实际子节点check_cmd；本驱动不会声称调用了独立模型规划器。

限制：最多12次执行调用、并发2、期望6节点。节点熔断设7，因为平台达到阈值即停派；最终仍严格验收6节点。每mission最多5次拆分护栏不变。运行超时/异常会调用kill并记录停止结果；未确认停止不得报告成功。

## 真实冒烟（尚待实际运行验证）

先完成零模型与反例验证，再在明确决定执行真实模型的情况下使用：

```powershell
python techstack_research_v2/run_smoke.py --mode real --timeout 1800 --output work/my-db-smoke-real
```

默认claude-cli，凭据使用现有外部配置。可用--backend-name zcode-cli和--backend-options <JSON路径>选择现有适配器参数；选项文件只包含受支持的配置路径/模型名/超时等，不在任务包存密钥。真实后端不可用会失败，不会退回FixtureBackend冒充成功。此命令会执行模型并可能计费；12次调用限制不是账单硬封顶。

真实模式不加载FixtureBackend、不由宿主写proposal或研究正文；模型从绑定协议自行提案，后续步骤仍调用相同平台核心。没有实际发生拆分，即使报告产出，也不通过六节点机制验收。

本驱动不实现跨进程自动续跑。重新运行须用新的空目录；已有任务库保留供审查，不要把普通MCP服务指向它后假定模板会自动加载。MCP入口接线需另行验证。

## 考卷与事实边界

统一检查器checks.py校验JSON schema、证据ID及引用、性能单位/条件、成本乘法及年度换算、比较输入哈希、整合不改写上游、根交付和输入摘要。它不会联网判断文档是否真实，也不能证明来源支持某项主张。来源可追溯字段完整不等于事实已核实。

verify_*.py保留为兼容入口，需传本次运行的--manifest。复核已发布根时，可在原根attempt工作区（含绑定inputs）执行：

```powershell
python D:/AI/DAG-agent/techstack_research_v2/verify_smoke_root.py --manifest D:/AI/DAG-agent/work/my-db-smoke/frozen/manifest.json
```

审计时优先使用本次frozen/checks.py的绝对路径，避免源码后续更新影响复验。平台干净重放已经自动执行；不要在缺inputs的单个accepted目录中直接跑需要绑定输入的整合考卷。

研究事实、价格来源、性能可比性和最终建议必须独立复核。review.json始终标content_review=pending；mission done/machine仅代表机器契约通过。

## 文件职责

- run_smoke.py：准备、冻结、资料导入、运行与证据收尾。
- templates.py：三种业务模板，仅在驱动进程注册。
- checks.py：六类统一验收，标准库实现，随任务冻结。
- fixture_backend.py：零模型正例生成与提案桩；真实模式不导入。
- inputs/requirements.json：显式任务假设。
- proposal-protocol.md：与平台parse_proposal一致的11字段协议。
- deliverable-schema.md：模型可读交付契约。
- tests/test_smoke.py：正例、对抗反例、协议、模板越权、六节点闭环与失败收尾。
