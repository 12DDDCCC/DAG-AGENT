# DAG-agent 设计说明（DESIGN v3.2.1）

> 本文回答"为什么这样设计"。v7（第一代文件版）设计说明见 git 历史 7b6c72b；
> 本文只描述当前系统：SQLite 核心 + 本轮按《设计覆盖度审计》HTML 定稿升级的
> 节点契约/验收/证据/生长/记忆体系。与实现不一致时以实现为准并回改本文。
> v3.2.1（2026-11-02）：自动生长执行链路默认启用。

## 一、设计原点

1. **Prove2Me 源码检验**（费马仓库实物）：kernel 一票否决、无锁认领、
   冲突容忍、提交不可变、骨架分解级联闭合、盲读回审计；
2. **设计覆盖度审计**（审查记录/2026-10-01-设计覆盖度审计/）：判卷半边
   （契约/验收/证据）按讨论定稿落地；运营半边沿用 Anthropic 原版机制；
3. **本地教训**：假完成、stub 假阴性、自报通过、软关键词验收——每条
   变成一条结构约束。

一句话哲学：**智能放进系统结构（契约+检查器+调度器+看板），agent 只做
无状态 worker；判卷端只会"跑命令、比退出码"这一个傻动作。**

## 二、节点 = 三层数据 + 三件套

```
契约层(锁定只读)：statement + check_cmd/check_version + acceptance_json
                  + deps + priority + strength_actual(空位)
产物层(执行者唯一写域)：work/<node>/<attempt>/ + accepted/(write-once,内容寻址)
证据层(append-only,仅平台写)：assessments(verdict三分/authoritative/invalid)
                  + reviews(主观层) + events(同事务)
```

三件套在节点创建时锁定（执行者会话无修改入口，唯一例外是出题方维修出新版）。

## 三、核心设计决策（v3.1）

### D1 五动作枚举与受限语法（acceptance.py）
出题方只能在 `cmd|test|scan|script|http` 五种"执行规格的方式"里组合
（`ACTIONS` 枚举 + 必填参数校验），编译产物全部落在白名单入口
（python/pytest/py）。灵活性全部放在参数与脚本（图灵完备的规格数据），
判卷端只认退出码。**关键词匹配永远不配当判决**——scan 动作只做禁用词
扫描（辅助守卫），功能验收必须真的执行。

### D2 考卷体检三关（acceptance.preflight）
考卷上考场（创建/维修/提案）前必过：**①能编译**（shlex+白名单静态检查）
**②能跑起**（空目录执行非 ERROR）**③咬得动坏产物**（空目录执行必须 FAIL
——空目录全绿=废卷/永绿断言，拒绝）。关②③共用同一次空目录执行。

### D3 三分判决 PASS/FAIL/ERROR（assessment.run_check3）
pass=产物合格；fail=产物不合格（打回执行者）；**error=考卷/环境故障
（不打回执行者）**。error 判定：解析失败/白名单外/超时/检查器写现场
（考卷不得咬坏现场）/**约定退出码 2=检查器自身故障**（python 找不到脚本、
pytest 用法错误、check_http 网络不通——"考卷跑不起来"借此与"产物不合格"
区分）。ERROR 的后果：attempt 标 error、节点转 blocked（等维修）、
**不消耗重试预算**（RetryPolicy 只数 failed）。

### D4 考卷维修协议（graph.repair_check）
出题方专属：新考卷先过体检三关 → check_version+1 → 事件 `check_repaired`
留痕（from/to/reason）→ **旧验收证据作废（invalid=1）** → 节点回 pending
重验。accepted/cancelled 终态不可修。执行者会话不持有此授权。

### D5 证据分级不合流（models.Assessment/Review）
`authoritative=1` 只有平台 submit 的验收；执行者自检（graph.self_check）
落账 `authoritative=0` 且不改变节点状态——**agent 的检验是反馈不是证据**。
主观层审阅（graph.attach_review，盲读证词）单列 reviews 表，**永不与机器
结果合并成一个"通过"**。闭合依据显式标注：`closure_basis =
machine|subjective`（主观闭合=显式降级，不冒充机器闭合）。

### D6 强度记账（graph.declare_strength）
交付时如实填"做到了什么/没做到什么"（strength_actual），防名实不符
（名可响，账须实——对应费马 Mazur 只证 Frey 情形）。空声明拒绝；
投影与事件均留痕。

### D7 生长闸门六条（graph.add_node/decompose + coordinator）
①三件套齐全（title+statement，缺验收靠 fail-closed 兜底）
②无环（新边只指向已存在节点，结构无环+依赖闭包防御）
③归属（**无主节点禁止**——必须挂父）
④依赖可满足（不得依赖 cancelled 节点=悬空依赖）
⑤粒度（陈述≤2000 字、deps≤8——单节点=单次执行者会话能完成的规模）
⑥查重（规范化标题完全相等→打回引用；**硬闸门零误伤**，陈述级相似走
recall 软提示——复用先于生长）。

### D8 外部记忆检索（memory.py）
节点 title/statement 即自然语言描述（Anthropic 原版平台四支柱之一）。
`recall()` 按 token（拉丁词+CJK 二元组）检索已有节点，agent 提议新节点前
必查；`find_similar()` 是生长闸门⑥的兜底。记忆的账由平台持有，agent 只是
检索的使用者。

### D9 调度优先级与受影响面（Anthropic 原版补齐）
节点 `priority` 字段（大者先派，同权按 id 稳定）——人中途干预方向的通道
（"优先完成 Mazur"）。`affected_subgraph()` 给出依赖闭包+归属闭包，是
增量重验（验证成本跟变更走）的依据。

### D10 沿用 v3 步骤 1-5 不变
SQLite 单写短事务（状态与事件同事务）/ accepted 不可变存储+哈希版本绑定+
检查期零写入守卫 / 真实执行后端（claude CLI×MiniMax）/ 调度器并发上限+
预算熔断 / 恢复层（暂停/取消迟到拒收/孤儿收割/回执幂等/失败重试）。

## 四、MCP 工具面（mcp_server_v3，20 工具）

原 12 工具不变（create_mission†/add_node†/decompose/frontier/status/board/
events/pause/resume/cancel/recover/run）；†已接体检与闸门。
新增 8 个：`dag3_repair_check`（维修）、`dag3_set_priority`（优先级）、
`dag3_set_strength`（强度）、`dag3_selfcheck`（自检·零权威）、
`dag3_recall`（记忆检索）、`dag3_review`（主观审阅）、
`dag3_close_subjective`（主观闭合）、`dag3_affected`（受影响面）。
create_mission 增 `anchor`（外部权威锚：公共规范/RFC，投影展示终验对照）。

## 五、七条铁律（自检清单）

1. 契约锁定后全系统只读，唯一例外是出题方维修出新版且永远留痕。
2. 判决三分：考卷故障（ERROR）永远不打回执行者、不耗重试预算。
3. 考卷上考场前先过体检：能编译、能跑起、咬得动坏产物。
4. 验收只认平台直跑（authoritative）；自检是反馈不是证据。
5. 证据分级不合流：机器/主观分开记，闭合依据显式标注。
6. 失败工件永不删；检查器不得咬坏现场（零写入守卫）。
7. 判卷端只认退出码——关键词匹配最多做辅助守卫。

## 六、诚实的边界

- **查重是保守的**：硬闸门只拦"同名重提"，词序换排/同义重述靠 recall
  软提示兜底——误拦合法生长的代价大于漏拦。
- **退出码 2 约定有成本**：产物检查脚本若业务上退出 2 会被误判 ERROR
  （约定文档化：检查脚本断言失败请退 1）。
- **验证器独立性=分级简化**：体检反例试跑（关③即 mutation smoke）+
  独立 runner 已覆盖日常；nanoda 式第二实现未做，留给高风险任务。
- **模板库未迁 v3**（v4 六模板仍在文件版）；多执行者进程池未上
  （并发上限是节点级）；主观闭合无强制审阅前置。
- **考卷体检不适用于"必须带现场"的检查**：依赖被测目录内容的考卷在
  空目录体检时天然 FAIL 视为有牙齿——语义正确但要注意体检环境差异。

## v3.2 完整框架构建（2026-10-03，任务书收官）

依据：outputs/ZCode-DAG-agent完整框架构建提示词.md + Codex 三轮复审 +
《协议/01-03》（F1 冻结：manifest / 输入绑定 / 提交身份）。

1. **第1步·六缺陷修复**：提交两阶段接收（P0 预检→P1 冻结候选→P2 候选上
   逐条三分检查→P3 备文件→P4 事务内核验提交权/指纹/绑定，幂等、拒收不覆写）；
   完整快照+叶子整合统一播种（摘要校验、缺件拒绝、禁改绑）；ERROR 分类记账
   （fail≤max_retries=2，error/cancel/orphan/reject 不耗额度）；体检与判卷同
   一逐条聚合（ERROR 优先，弃 && 短路）；路径组件包含（is_relative_to）；
   周期心跳线程+收割接线（protected 防误割）+预算持久（budget_json）。
   四反例探针转正回归（反例翻转=错误被拒绝）。
2. **第2步·版本协议**：revise_node（revision+1、快照/证据作废、反向依赖+
   整合祖先级联、本节点与下游在途取消、done 自动重开）；reopen_mission；
   主观签收绑定快照+basis 必填；回执失败不落账。
3. **第3步·AND/OR**：group_id/order/switch_after；激活规则（前支 FAIL 达
   阈值切支，ERROR 走维修）；赢家原子选定（同事务取消败者 lost_race 留史）；
   整合与下游依赖解析到赢家快照；闸门④组感知。
4. **第4步+运行接线**：根干净重放进提交链（不过不机器闭合）；根考卷异构
   （check_composite）；task_templates 四类别（默认值 provenance=本机实测非
   P95）；节点数熔断；daemon.RunManager（一任务一调度器/drain|kill/重启恢复/
   投影自动更新）；projection 版本守卫+原子发布；MCP 20→27 工具。
5. **第5步**：三迁移场景（多文件依赖/数据变更重验/研究事实+主观签收）
   ALL_SCENARIOS_OK×5 连跑；真机 E2E MiniMax-M2 3 调用 $2.47/63s
   machine 闭合。

测试口径：tests/v3 122 项全绿（88 基线规格演进+34 新增回归）；遗留 46 全绿。
诚实边界：隔离=白名单+目录快照+提示词约束（非 OS 沙箱，env_note 如实记录）；
场景脚本曾出现一次环境级偶发（Windows 临时目录 AV 扫描窗口），5 连跑通过；
显式延期：自动生长执行链路（宿主显式 decompose）、多服务并发、web UI、
Canvas 美化、第二验证器。
