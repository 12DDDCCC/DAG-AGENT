"""v3 核心数据模型：mission/node/dependency/attempt/artifact/assessment/event/receipt。

与 v0.1（store.py JSON）的区别：完成语义改为"孩子全 accepted 只解锁整合尝试，
父节点必须自身整合尝试过检才 accepted"；状态与事件同事务落 SQLite。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ---- 状态枚举（字符串落库，枚举只做校验与文档） ----

MISSION_STATUSES = ("active", "done", "cancelled")

NODE_STATUSES = (
    "pending",           # 可认领（依赖满足与否在认领时把关）
    "waiting_children",  # 已分解：等孩子全 accepted 后解锁整合尝试
    "waiting_split",     # 自动生长：拆分提案已入队，等独立出题+权威应用
                         # （持久等待态；不被 frontier/integration 认领）
    "in_work",           # 存在 running 尝试
    "submitted",         # 尝试已提交待验收
    "accepted",          # 终态：验收通过
    "failed",            # 终态可重启：最近一次尝试失败
    "blocked",           # 人工阻塞
    "cancelled",
)

ATTEMPT_STATUSES = ("running", "submitted", "accepted", "failed", "error",
                    "orphaned", "cancelled", "rejected", "split")

# v3.2 提交身份协议：attempt 终局分类（产物重试额度只计 fail，见协议03§6）
# split=拆分请求已被受理（原执行位释放、交付走父整合）——明确非 FAIL/ERROR
ATTEMPT_CATEGORIES = ("accept", "fail", "error", "cancel", "orphan", "reject",
                      "split", "")

NODE_KINDS = ("task", "aggregate")

# v3.1 三分判决：error=考卷/环境故障（不打回执行者，走维修通道）
ASSESSMENT_VERDICTS = ("pass", "fail", "error")

# v3.1 闭合依据（证据分级不合流）：machine=机器闭合 / subjective=主观层闭合
CLOSURE_BASES = ("machine", "subjective")


def _valid(value: str, allowed: tuple) -> str:
    if value not in allowed:
        raise ValueError(f"非法状态 {value!r}，允许：{allowed}")
    return value


@dataclass
class Mission:
    id: str
    name: str
    brief: str = ""
    status: str = "active"
    goal_version: int = 1
    closure_basis: str = "machine"   # v3.1：machine|subjective（闭合依据显式标注）
    anchor: str = ""                 # v3.1：外部权威对齐锚（公共规范/RFC/标准引用）
    budget_json: str = "{}"          # v3.2：预算持久（calls/cost，重启不重置）
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self):
        _valid(self.status, MISSION_STATUSES)
        _valid(self.closure_basis, CLOSURE_BASES)


@dataclass
class Node:
    id: str                 # 全局唯一 n-<seq>
    mission_id: str
    title: str
    statement: str = ""     # 陈述（做什么），不含实现
    parent_id: Optional[str] = None   # 归属关系：为哪个父目标服务
    kind: str = "task"      # task|aggregate
    status: str = "pending"
    check_cmd: Optional[str] = None   # 验收命令：创建时锁定，API 不提供修改
    check_version: int = 1
    acceptance_json: Optional[str] = None  # v3.1：受限枚举验收清单（五动作）
    priority: int = 0       # v3.1：调度优先级（大者先派）
    strength_actual: str = ""   # v3.1：实际强度记账（交付时填，防名实不符）
    accepted_artifact_version: Optional[int] = None
    declared_artifacts_json: str = "[]"  # 交付物清单（创建时声明，调度器据此申报工件）
    revision: int = 1                  # v3.2：产物修订版本（revise/reopen 时+1）
    group_id: Optional[str] = None     # v3.2：同父同组=OR 替代组（NULL=普通 AND 孩子）
    group_order: int = 0               # v3.2：组内激活顺序（小者先）
    switch_after: int = 1              # v3.2：连续产物 FAIL 达此数切下一支
    exam_files_json: str = "[]"        # T6：考卷包显式清单（空=扫描闭包默认）
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self):
        _valid(self.status, NODE_STATUSES)
        _valid(self.kind, NODE_KINDS)

    @property
    def is_aggregate(self) -> bool:
        return self.kind == "aggregate"


@dataclass
class Dependency:
    node_id: str            # 执行依赖：开始前需取得谁的 accepted 产物
    depends_on: str
    expected_version: Optional[int] = None  # None=最新 accepted 版本
    created_at: str = ""


@dataclass
class Attempt:
    id: str                 # a-<hex>
    node_id: str
    mission_id: str
    backend: str = "stub"   # stub|claude-cli|...
    status: str = "running"
    workdir: str = ""       # 独立执行目录（步骤2启用）
    pid: Optional[int] = None
    heartbeat_at: str = ""  # 步骤5恢复用
    resources_json: str = "{}"
    result_summary: str = ""
    input_binding_json: str = "[]"      # v3.2：认领时冻结的输入绑定（协议02）
    claim_fingerprint_json: str = "{}"  # v3.2：认领指纹（协议03）
    category: str = ""                  # v3.2：终局分类 accept|fail|error|cancel|orphan|reject
    started_at: str = ""
    ended_at: str = ""

    def __post_init__(self):
        _valid(self.status, ATTEMPT_STATUSES)
        _valid(self.category, ATTEMPT_CATEGORIES)


@dataclass
class Artifact:
    id: int
    attempt_id: str
    node_id: str
    mission_id: str
    rel_path: str
    content_hash: str       # sha256
    version: int            # 节点内单调递增
    snapshot_id: Optional[int] = None  # v3.2：所属快照（协议01）
    created_at: str = ""


@dataclass
class Snapshot:
    """v3.2 工件 manifest（协议01）：一次提交的全部文件共享一个快照身份。"""
    id: int
    mission_id: str
    node_id: str
    attempt_id: str
    artifact_version: int      # accepted 存储 v<该值> 目录
    node_revision: int = 1
    goal_version: int = 1
    manifest_hash: str = ""
    files_json: str = "[]"
    valid: int = 1
    created_at: str = ""


@dataclass
class Assessment:
    id: int
    node_id: str
    attempt_id: str
    artifact_version: int   # 被检产物版本（验收绑定）
    goal_version: int       # 验收时的目标版本：期间变更则旧验收失效
    check_cmd: str
    check_version: int
    method: str             # cmd|stub
    evidence: str
    verdict: str            # pass|fail|error（v3.1 三分）
    authoritative: int = 1  # v3.1：1=平台终检入账；0=执行者自检（反馈非证据）
    invalid: int = 0        # v3.1：考卷维修后旧证据作废
    snapshot_id: Optional[int] = None  # v3.2：被检快照（协议01）
    check_digest: str = ""             # v3.2：考卷包内容摘要（命令+脚本内容）
    input_binding_hash: str = ""       # v3.2：提交时输入绑定清单摘要（协议02）
    env_note: str = ""                 # v3.2：环境指纹（python 版本等）
    created_at: str = ""

    def __post_init__(self):
        _valid(self.verdict, ASSESSMENT_VERDICTS)


@dataclass
class Review:
    """v3.1 主观层审阅证据：独立会话盲读产出的意见，永不与机器结果合并。"""
    id: int
    node_id: str
    verdict: str            # accept|concern|reject（主观判断）
    note: str = ""
    blind: int = 1          # 1=盲读（材料不含执行者自述）
    snapshot_id: Optional[int] = None  # v3.2：签收针对的产物快照（协议01）
    basis: str = ""                     # v3.2：签收依据（引用什么作出判断）
    created_at: str = ""


@dataclass
class Event:
    id: int
    mission_id: str
    at: str
    type: str
    node_id: Optional[str] = None
    payload_json: str = "{}"
