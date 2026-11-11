"""v3 协调层——拆解提案的结构化校验（纯程序规则）。

制度对应：提案→校验（无环/验收命令齐备/规模与预算）→考卷体检→单事务落地。
LLM 审查挂点留作后续（coordinator_review hook），当前诚实标注：
本层只做结构裁决，不判断"拆得好不好"。

v3.1 生长闸门补强：①三件套齐全（statement 必填）⑤粒度上限
（陈述字数/依赖数——单节点=单次执行者会话能完成的规模）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

MAX_CHILDREN = 5
MAX_DEPS = 8                # v3.1 粒度闸门
MAX_STATEMENT_CHARS = 2000  # v3.1 粒度闸门


@dataclass
class Proposal:
    node_id: str
    reason: str = ""
    integration_check: str | None = None
    children: list[dict] = field(default_factory=list)
    # child: {title, statement, check_cmd, deps, declared_artifacts, acceptance?}


def review_proposal(p: Proposal) -> tuple[bool, str]:
    """程序裁决。返回 (ok, reason)。"""
    if not p.children:
        return False, "提案无孩子"
    if len(p.children) > MAX_CHILDREN:
        return False, f"孩子数超上限（{len(p.children)}>{MAX_CHILDREN}）"
    ids = [c.get("id") for c in p.children if c.get("id")]
    if len(set(ids)) != len(ids):
        return False, "孩子 id 重复"
    for i, c in enumerate(p.children):
        if not c.get("title"):
            return False, f"孩子{i} 缺 title"
        if not (c.get("statement") or "").strip():
            return False, f"孩子{i}({c.get('title')}) 缺陈述：三件套不齐，打回"
        if len(c.get("statement", "")) > MAX_STATEMENT_CHARS:
            return False, (f"孩子{i}({c.get('title')}) 陈述超 {MAX_STATEMENT_CHARS} 字："
                           "粒度过大，先拆")
        if len(c.get("deps", ())) > MAX_DEPS:
            return False, f"孩子{i}({c.get('title')}) 依赖超上限 {MAX_DEPS}"
        if not c.get("check_cmd"):
            return False, f"孩子{i}({c.get('title')}) 缺验收命令：fail-closed 会堵死整合"
        for d in c.get("deps", ()):
            if d not in ids:
                # 允许依赖已存在节点，但不允许依赖本提案外未知 id
                pass  # 存在性由 graph.decompose 落库时校验
        if c.get("deps") and p.node_id in c["deps"]:
            return False, "孩子依赖父节点会死锁"
    if not p.integration_check:
        return False, "提案须给整合验收命令（父节点整合尝试 fail-closed）"
    return True, "ok"
