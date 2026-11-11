# 协议 01：工件 Manifest（完整快照）——v3.2 冻结版

> 冻结日期：2026-10-03。本协议是修订五步第 1/2 步的最小协议之一，实现须与本文件一致；
> 变更须走版本化修订并更新本文件版本号。

版本：F1（冻结）

## 1. 目的

一次提交的全部交付文件共享**一个快照身份**。修复"逐文件各自版本、整合只取末件、
下游拿不到完整交付"的输入契约缺陷。manifest 是输入绑定（协议02）与提交身份（协议03）
的数据基础。

## 2. 数据结构

新表 `snapshots`（每任务库内）：

| 字段 | 类型 | 语义 |
|---|---|---|
| id | INTEGER PK 自增 | 快照号（节点内=artifacts.version，全局唯一行） |
| mission_id / node_id / attempt_id | TEXT | 归属 |
| node_revision | INTEGER | 发布时的节点产物修订号（默认 1） |
| goal_version | INTEGER | 发布时的目标版本 |
| manifest_hash | TEXT | sha256(规范化 files 清单)，快照整体摘要 |
| files_json | TEXT | `[{"rel_path","sha256","size"}]`（posix 相对路径，排序后哈希） |
| valid | INTEGER | 1=有效；0=已被修订/重开作废（作废快照禁止作为输入绑定） |
| created_at | TEXT | |

`artifacts` 增列 `snapshot_id INTEGER`（旧行为 NULL=历史遗留，不得伪造为新协议行）。

manifest_hash 计算：对 files 按 rel_path 排序，逐行 `"{rel_path}:{sha256}:{size}"`，
join "\n" 后 sha256 十六进制。

## 3. 状态转换

快照生命周期：`created(valid=1) → [revise/reopen 级联] → valid=0`。
- 只在提交**正式接收**（协议03 COMMIT 阶段）的同事务内 INSERT；
- `revise_node`（第2步）与增量失效（第4步）在事务内把受影响快照置 valid=0 并写事件
  `snapshot_invalidated(reason)`；已作废快照永不复活，重验通过生成**新**快照行。

## 4. 存储布局

accepted 存储沿用 `<home>/accepted/<nid>/v<version>/<rel>`；version 与 snapshot_id 在
节点内一一对应（同一正式接收事务同时产生）。发布先落文件、后进事务（见协议03），
崩溃残留的"无 DB 行文件"为孤儿，无害可回收，正式接收永不指向缺件。

## 5. 考卷包（命令锁定→考卷锁定）

考卷包 = acceptance_json（五动作清单）或 check_cmd + 其引用脚本内容：
- `assessments` 增列：`check_digest`（sha256(acceptance_json 规范化文本 或 check_cmd
  + script 动作引用文件的逐文件内容摘要拼接)）、`snapshot_id`（被检快照）、
  `input_binding_hash`（协议02 绑定清单摘要）、`env_note`（python 版本等环境指纹）。
- 只锁命令文本不算锁考卷：script 动作的 `path` 指向的项目内脚本，其内容摘要进入
  check_digest；脚本文件改动 → digest 变 → 证据可判定"考卷已非彼时考卷"。

## 6. 示例

```json
{"id": 3, "node_id": "n2", "attempt_id": "a-1f", "node_revision": 1,
 "goal_version": 1,
 "manifest_hash": "9f86…",
 "files_json": [
   {"rel_path": "data.csv", "sha256": "aa…", "size": 8},
   {"rel_path": "schema.json", "sha256": "bb…", "size": 19}],
 "valid": 1}
```

下游绑定时收到**两件全量**，而非 `arts[-1]` 一件。

## 7. 兼容与迁移

- 迁移策略：`ALTER TABLE` 幂等加列 + `snapshots` 新表（CREATE IF NOT EXISTS），
  `PRAGMA user_version=3`。旧库旧证据缺新列值 → 保持 NULL/默认，**不伪造**；
  历史快照语义按旧行为解释（在迁移测试中验证，不覆盖真实 data/）。
