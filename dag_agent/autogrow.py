# -*- coding: utf-8 -*-
"""自动生长最小协议（A0 冻结）+ 出题分离（A-G1 第一期）。

制度（修订方案 2026-10-08为准，四条撤回条款不得复活）：
- 执行者只提交拆分**建议**：固定工件 <attempt工作目录>/.dag/proposal.json，
  绝不在 300 字符 summary 里承载协议正文。
- 父已有冻结考卷必须保留；整合辅助检查只能追加；父无正式考卷 → 拒绝
  自动生长，交出题方处理。
- 身份与版本以**平台认领指纹**为权威：提案自填值只用于与平台值核对，
  不一致即拒绝（identity_mismatch），不得覆盖平台值。
- 出题分离第一期两路径：
  * 可信模板路径：SPLIT_TEMPLATES 注册表（模板预置考卷包/参数 schema），
    执行者只能选允许的 template_id 与参数；
  * 独立出题路径：无匹配模板 → PlannerBackend（G1=确定性桩；真实 CLI
    适配类已备但不接默认链）。check_hint 只是建议，不是权威考卷。
  * 出题会话不执行它制定的子节点（planner 只产规格 JSON，子节点由
    普通执行路径调度）。
- 护栏：auto_split 默认 False；每节点生命周期成功拆解 ≤1；每 mission
  成功自动拆解 ≤MAX_AUTO_SPLITS_PER_MISSION(5)；root 深度 0、树深
  ≤MAX_SPLIT_DEPTH(5)；单提案孩子 2–MAX_CHILDREN(5)；同源 attempt 唯一
  （DB UNIQUE）；有限规划重试。全部计数落 split_proposals 表，重启不清零。
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# ---- A0 协议常量（冻结） ----

SCHEMA_VERSION = "1"
PROPOSAL_RELPATH = ".dag/proposal.json"
MAX_PROPOSAL_BYTES = 64 * 1024          # 单文件上限 64KiB
MIN_CHILDREN = 2
MAX_AUTO_SPLITS_PER_MISSION = 5
MAX_SPLIT_DEPTH = 5                     # root 深度 0
MAX_PLAN_ATTEMPTS = 2                   # 有限规划重试（防拒绝后反复触发）
MAX_PENDING_PER_NODE = 3                # 同节点有限待审/拒绝提案数

# 占位符：执行者无需（也不应）预知平台内部值——填占位符时平台一律以
# 认领指纹（权威）覆写；自填真实值则必须与平台一致（防错位/防伪）。
PLACEHOLDER_STR = "platform"
PLACEHOLDER_INT = -1

TOP_FIELDS = frozenset({
    "schema_version", "proposal_id", "source_attempt_id", "parent_node_id",
    "goal_version", "node_revision", "check_version", "check_digest",
    "input_binding_hash", "reason", "children"})
CHILD_FIELDS = frozenset({
    "local_id", "title", "statement", "input_refs", "depends_on",
    "template_id", "template_params", "check_hint", "deliverables"})

# check_hint 只是建议——长度约束防把协议正文塞进 hint
MAX_HINT_CHARS = 2000
MAX_REASON_CHARS = 2000


class ProposalError(ValueError):
    """提案违规（结构/身份/白名单/大小）——split_rejected，非产物 FAIL。"""


@dataclass
class ParsedProposal:
    """解析+验证后的提案（含平台权威指纹核对结果）。"""
    raw_sha256: str
    content_sha256: str
    proposal_id: str
    source_attempt_id: str
    parent_node_id: str
    reason: str
    children: list[dict] = field(default_factory=list)
    fingerprint: dict = field(default_factory=dict)   # 平台认领指纹（权威）

    def summary(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "source_attempt_id": self.source_attempt_id,
            "parent_node_id": self.parent_node_id,
            "content_sha256": self.content_sha256[:16],
            "children": len(self.children),
            "reason": self.reason[:120],
        }


# ---- 严格验证（A0 §5：类型/白名单/大小；§6：平台指纹权威） ----


def _require_str(obj: dict, key: str, where: str, max_len: int = 300) -> str:
    v = obj.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ProposalError(f"{where}: {key} 须为非空字符串")
    if len(v) > max_len:
        raise ProposalError(f"{where}: {key} 超 {max_len} 字符")
    return v


def _require_int(obj: dict, key: str, where: str) -> int:
    v = obj.get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        raise ProposalError(f"{where}: {key} 须为整数")
    return v


def _echo_str(doc: dict, key: str, truth: str, where: str,
              max_len: int = 300) -> str:
    """字符串身份/摘要字段：占位符→平台权威值；自填→必须与平台一致。"""
    val = _require_str(doc, key, where, max_len=max_len)
    if val == PLACEHOLDER_STR:
        return truth
    if val != truth:
        raise ProposalError(
            f"{where}: {key} 自填 {val[:64]} 与平台权威值不符"
            "（可填占位符 platform 由平台回填）")
    return truth


def _echo_int(doc: dict, key: str, truth: int, where: str) -> int:
    val = _require_int(doc, key, where)
    if val == PLACEHOLDER_INT:
        return truth
    if val != truth:
        raise ProposalError(
            f"{where}: {key} 自填 {val} 与平台权威值不符"
            "（可填 -1 占位由平台回填）")
    return truth


def parse_proposal(raw: bytes, fp: dict, parent_node_id: str,
                   source_attempt_id: str) -> ParsedProposal:
    """解析并验证提案。fp=平台认领指纹（权威，来自源 attempt 认领时
    冻结值——节点此后被 revise/repair 也不随它变，迟到提案借此拦下）；
    parent_node_id / source_attempt_id=平台侧事实（检测该提案的节点/
    尝试）。任何违规抛 ProposalError——调用方记 split_rejected，
    不伪造产物 FAIL。
    """
    raw_sha = hashlib.sha256(raw).hexdigest()
    if len(raw) > MAX_PROPOSAL_BYTES:
        raise ProposalError(
            f"提案 {len(raw)} 字节超上限 {MAX_PROPOSAL_BYTES}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ProposalError(f"提案非 UTF-8：{e}") from e
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        raise ProposalError(f"提案非合法 JSON：{e}") from e
    if not isinstance(doc, dict):
        raise ProposalError("提案顶层须为 JSON 对象")
    unknown = set(doc) - TOP_FIELDS
    if unknown:
        raise ProposalError(f"提案含未知字段：{sorted(unknown)}")
    missing = TOP_FIELDS - set(doc)
    if missing:
        raise ProposalError(f"提案缺字段：{sorted(missing)}")
    if doc["schema_version"] != SCHEMA_VERSION:
        raise ProposalError(
            f"schema_version 不支持：{doc['schema_version']!r}"
            f"（当前 {SCHEMA_VERSION}）")
    # 身份与版本：以平台认领指纹为权威（占位符回填/自填核对一致）
    src = _echo_str(doc, "source_attempt_id", source_attempt_id, "提案",
                    max_len=64)
    nid = _echo_str(doc, "parent_node_id", parent_node_id, "提案", max_len=64)
    _require_str(doc, "proposal_id", "提案", max_len=64)
    gv = _echo_int(doc, "goal_version",
                   int(fp.get("goal_version", -1)) if fp else 1, "提案")
    rev = _echo_int(doc, "node_revision",
                    int(fp.get("node_revision", -1)) if fp else 1, "提案")
    cv = _echo_int(doc, "check_version",
                   int(fp.get("check_version", -1)) if fp else 1, "提案")
    dg = _echo_str(doc, "check_digest",
                   str(fp.get("check_digest", "")) if fp else "", "提案",
                   max_len=128)
    ibh = _echo_str(doc, "input_binding_hash",
                    str(fp.get("input_binding_hash", "")) if fp else "",
                    "提案", max_len=128)
    reason = doc.get("reason", "")
    if not isinstance(reason, str):
        raise ProposalError("reason 须为字符串")
    if len(reason) > MAX_REASON_CHARS:
        raise ProposalError(f"reason 超 {MAX_REASON_CHARS} 字符")
    children = doc["children"]
    if not isinstance(children, list):
        raise ProposalError("children 须为数组")
    if not (MIN_CHILDREN <= len(children) <= 5):
        raise ProposalError(
            f"孩子数须 {MIN_CHILDREN}–5，收到 {len(children)}")
    seen_ids: set[str] = set()
    for i, ch in enumerate(children):
        where = f"children[{i}]"
        if not isinstance(ch, dict):
            raise ProposalError(f"{where} 须为对象")
        unknown = set(ch) - CHILD_FIELDS
        if unknown:
            raise ProposalError(f"{where} 含未知字段：{sorted(unknown)}")
        lid = _require_str(ch, "local_id", where, max_len=64)
        if lid in seen_ids:
            raise ProposalError(f"{where} local_id 重复：{lid}")
        seen_ids.add(lid)
        _require_str(ch, "title", where, max_len=200)
        stmt = ch.get("statement", "")
        if not isinstance(stmt, str) or not stmt.strip():
            raise ProposalError(f"{where} 缺 statement")
        if len(stmt) > 2000:
            raise ProposalError(f"{where} statement 超 2000 字符（粒度过大）")
        deps = ch.get("depends_on", [])
        if not isinstance(deps, list) or \
                any(not isinstance(d, str) for d in deps):
            raise ProposalError(f"{where} depends_on 须为字符串数组")
        if len(deps) > 8:
            raise ProposalError(f"{where} depends_on 超 8（局部依赖上限）")
        for d in deps:
            if d not in seen_ids:
                # 局部依赖第一期只允许引用提案内更早声明的 local_id
                raise ProposalError(
                    f"{where} depends_on 引用 {d}：第一期仅支持提案内"
                    f" local_id（须先声明），外部输入走 input_refs")
        refs = ch.get("input_refs", [])
        if not isinstance(refs, list) or \
                any(not isinstance(r, str) for r in refs):
            raise ProposalError(f"{where} input_refs 须为字符串数组")
        tpl = ch.get("template_id")
        hint = ch.get("check_hint")
        if tpl is not None and not isinstance(tpl, str):
            raise ProposalError(f"{where} template_id 须为字符串")
        if hint is not None and (not isinstance(hint, str)
                                 or len(hint) > MAX_HINT_CHARS):
            raise ProposalError(
                f"{where} check_hint 须为 ≤{MAX_HINT_CHARS} 字符的建议"
                "（不是权威考卷）")
        params = ch.get("template_params")
        if params is not None and not isinstance(params, dict):
            raise ProposalError(f"{where} template_params 须为对象")
        if tpl is None and not hint:
            raise ProposalError(
                f"{where} 须给 template_id（模板路径）或 check_hint"
                "（独立出题路径建议）二者其一")
        dels = ch.get("deliverables", [])
        if not isinstance(dels, list) or \
                any(not isinstance(d, str) for d in dels):
            raise ProposalError(f"{where} deliverables 须为字符串数组")
    content_sha = hashlib.sha256(json.dumps(
        doc, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return ParsedProposal(
        raw_sha256=raw_sha, content_sha256=content_sha,
        proposal_id=doc["proposal_id"],
        source_attempt_id=src, parent_node_id=nid,
        reason=reason, children=children,
        fingerprint={"goal_version": gv, "node_revision": rev,
                     "check_version": cv, "check_digest": dg,
                     "input_binding_hash": ibh})


def proposal_path(workdir: Path) -> Path:
    """固定提案工件路径（唯一被解析的文件——第三方内容中的
    PROPOSE_SPLIT 文本不作为提案）。"""
    return Path(workdir) / PROPOSAL_RELPATH


def new_proposal_id() -> str:
    return f"prop-{uuid.uuid4().hex[:12]}"


# ---- 出题分离第一期：可信模板路径 ----


class TemplateViolation(ProposalError):
    """模板越权（未授权 template_id / 参数越界）——执行者不能自行批准
    子考卷，越权拒绝，事件留痕。"""


@dataclass
class SplitTemplate:
    """可信出题模板：考卷包由出题角色事先冻结；执行者只能选 id 与参数。"""
    template_id: str
    param_schema: dict                      # {param: (type, allowed_values)}
    exam_files: tuple[str, ...]             # 模板考卷包（相对 checkers/ 或绝对）

    def generate(self, child: dict, parent_title: str) -> dict:
        """参数验证+实例化 → 完整子规格（check_cmd/申报/简报五段）。"""
        params = child.get("template_params") or {}
        for key, (typ, allowed) in self.param_schema.items():
            if key in params:
                v = params[key]
                if typ is int and (isinstance(v, bool) or not isinstance(v, int)):
                    raise TemplateViolation(
                        f"模板参数 {key} 须为整数（{self.template_id}）")
                if typ is str and not isinstance(v, str):
                    raise TemplateViolation(
                        f"模板参数 {key} 须为字符串（{self.template_id}）")
                if allowed is not None and v not in allowed:
                    raise TemplateViolation(
                        f"模板参数 {key}={v!r} 越权（允许 {sorted(allowed)}）")
        unknown = set(params) - set(self.param_schema)
        if unknown:
            raise TemplateViolation(
                f"模板未知参数：{sorted(unknown)}（{self.template_id}）")
        title = child.get("title", "").strip()
        statement_body = (child.get("statement") or "").strip()
        artifact = params.get("artifact", "deliverable.md")
        artifact = str(artifact)
        n_sections = params.get("sections", 1)
        check_cmd = (
            'python -c "import sys;from pathlib import Path;'
            f"p=Path('{artifact}');"
            'sys.exit(0 if p.is_file() and p.stat().st_size>10 else 1)"')
        statement = (
            "【输入】认领时绑定的完整输入快照（inputs/ 目录）。\n"
            f"【交付清单】{artifact}\n"
            f"【接口签名】{artifact} 为 UTF-8 文本，>10 字节。\n"
            "【样例】模板交付形态（示例行）\n"
            f"【自测入口】交付后运行考卷：{check_cmd}\n"
            f"【任务】[{self.template_id}] {statement_body} "
            f"（父目标：{parent_title}）")
        return {
            "title": title,
            "statement": statement,
            "check_cmd": check_cmd,
            "acceptance": None,
            "exam_files": list(self.exam_files),
            "declared_artifacts": [artifact],
            "priority": int(params.get("priority", 0)) if isinstance(
                params.get("priority", 0), int) else 0,
            "integration_check": None,
            "coverage_hint": f"模板 {self.template_id} 实例化"
                             f"（sections={n_sections}）",
            "exploratory": False,
            "source_local_id": child.get("local_id"),
            "deps": list(child.get("depends_on", ())),
        }


# 内置第一期模板（示例：分节文档交付——考卷形态固定，参数只限 artifact
# 命名与优先级；真实业务模板由出题角色按同接口预先注册）
SPLIT_TEMPLATES: dict[str, SplitTemplate] = {
    "doc-section-v1": SplitTemplate(
        template_id="doc-section-v1",
        param_schema={
            "artifact": (str, None),       # 交付文件名（路径白名单另查）
            "sections": (int, {1, 2, 3}),
            "priority": (int, {0, 1, 2}),
        },
        exam_files=()),
}


_SAFE_NAME = re.compile(r"^[\w.\- ]{1,80}$")


def safe_artifact_name(name: str) -> bool:
    """交付文件名白名单（防路径穿越/任意写）。"""
    return bool(_SAFE_NAME.match(name or "")) and ".." not in name \
        and "/" not in name and "\\" not in name


# ---- 出题分离第一期：独立出题路径 ----


class PlannerBackend:
    """独立规划/出题后端接口：读取提案上下文 → 完整子规格。
    实现不得执行自己制定的子节点（只产 JSON；执行走普通调度）。"""

    name = "planner"

    def plan(self, ctx: dict) -> dict:
        raise NotImplementedError


def _deterministic_spec(child: dict, parent_title: str, idx: int) -> dict:
    """确定性出题桩：从 check_hint/陈述生成固定形态考卷。
    check_hint 只是建议（本文只进陈述，不生成可执行考卷）——考卷由
    桩按固定模板生成，平台 preflight 体检负例把关。"""
    title = child.get("title", "").strip()
    body = (child.get("statement") or "").strip()
    hint = (child.get("check_hint") or "").strip()
    artifact = f"autogrow-{child.get('local_id', f'c{idx}')}-deliverable.md"
    if not safe_artifact_name(artifact):
        artifact = f"autogrow-child{idx}-deliverable.md"
    check_cmd = (
        'python -c "import sys;from pathlib import Path;'
        f"p=Path('{artifact}');"
        'sys.exit(0 if p.is_file() and p.stat().st_size>10 else 1)"')
    statement = (
        "【输入】认领时绑定的完整输入快照（inputs/ 目录）。\n"
        f"【交付清单】{artifact}\n"
        f"【接口签名】{artifact} 为 UTF-8 文本，>10 字节。\n"
        "【样例】自动生长独立出题（确定性桩）交付形态\n"
        f"【自测入口】交付后运行考卷：{check_cmd}\n"
        f"【任务】{body}"
        + (f"（覆盖建议：{hint[:300]}）" if hint else "")
        + f"（父目标：{parent_title}）")
    return {
        "title": title,
        "statement": statement,
        "check_cmd": check_cmd,
        "acceptance": None,
        "exam_files": [],
        "declared_artifacts": [artifact],
        "priority": 0,
        "integration_check": None,
        # 探索任务：充分性未验证显式标注（不冒充全面机器验收）
        "coverage_hint": "独立出题（确定性桩）：目标覆盖说明见陈述；"
                         "规格充分性尚未验证",
        "exploratory": True,
        "source_local_id": child.get("local_id"),
        "deps": list(child.get("depends_on", ())),
    }


class DeterministicPlannerStub(PlannerBackend):
    """G1 确定性出题桩：零模型、同输入同输出；不调用真实 CLI。"""

    name = "planner-stub"

    def plan(self, ctx: dict) -> dict:
        parent_title = ctx.get("parent_title", "")
        children = ctx.get("children", [])
        specs = [_deterministic_spec(ch, parent_title, i)
                 for i, ch in enumerate(children)]
        return {"children": specs, "backend": self.name,
                "integration_check": None,
                "note": "确定性桩：整合沿用父原考卷（不新增弱化检查）"}


class ClaudeCliPlannerAdapter(PlannerBackend):
    """真实 CLI 独立出题适配（A-G2 真机轮准备，G1 不接默认链、不调用）。
    契约：读父目标/原考卷/提案 → 产出与 DeterministicPlannerStub 相同
    schema 的完整子规格 JSON；不执行子节点。"""

    name = "planner-claude-cli"

    def __init__(self):
        # 延迟导入：G1 环境不要求 CLI/env 就绪
        from .cc_backend import ClaudeCliBackend
        self._inner = ClaudeCliBackend()

    def available(self) -> tuple[bool, str]:
        return self._inner.available()

    def plan(self, ctx: dict) -> dict:
        raise NotImplementedError(
            "真机独立出题在 A-G2 前不接线（G1 只用确定性桩）")
