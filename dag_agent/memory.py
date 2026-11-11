"""v3.1 外部记忆与检索——"查重先于生长"的眼睛。

设计对应（Anthropic 原版平台四支柱之一，覆盖度审计 §4.1）：
- 每个节点的 title/statement 是可检索的自然语言描述；
- agent 提议新节点前先 recall 搜已有节点（复用优先于生长）；
- 生长闸门的查重：等价节点已存在 → 打回引用，不长新的。
"""
from __future__ import annotations

import re

from .repository import Repository

_DUP_THRESHOLD = 0.6   # token Jaccard ≥ 此值视为等价候选


def tokenize(text: str) -> set[str]:
    """分词：拉丁词 + CJK 二元组（中文无空格，用字符二元组保召回）。"""
    text = (text or "").lower()
    latin = re.findall(r"[a-z0-9_]{2,}", text)
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    bigrams = {a + b for a, b in zip(cjk, cjk[1:])}
    return set(latin) | bigrams


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def recall(repo: Repository, mid: str, query: str, limit: int = 10) -> list[dict]:
    """检索已有节点（外部记忆读路径，agent 可随时调用）。"""
    tokens = sorted(tokenize(query))
    if not tokens:
        return []
    hits = repo.search_nodes(mid, tokens[:8], limit=max(limit * 3, 30))
    q = tokenize(query)
    scored = [
        {"id": n.id, "title": n.title, "status": n.status, "kind": n.kind,
         "statement": n.statement[:120],
         "score": round(jaccard(q, tokenize(f"{n.title} {n.statement}")), 3)}
        for n in hits
    ]
    scored = [s for s in scored if s["score"] > 0]
    scored.sort(key=lambda s: -s["score"])
    return scored[:limit]


def find_similar(repo: Repository, mid: str, title: str, statement: str = "",
                 exclude: tuple[str, ...] = ()) -> list[dict]:
    """生长闸门查重（硬闸门）：返回与本提议同名的已有节点。

    判据=规范化标题完全相等（去空白+小写）——零误伤的硬规则，拦截最常见的
    "同名重提"。标题词序换排/陈述相似属软信号，由 recall 检索提示复用，
    不做硬拦截（误拦合法生长的代价 > 漏拦可由检索兜底的重复）。
    """
    norm_new = re.sub(r"\s+", "", (title or "").lower())
    if not norm_new:
        return []
    out: list[dict] = []
    for n in repo.list_nodes(mid):
        if n.id in exclude:
            continue
        if re.sub(r"\s+", "", (n.title or "").lower()) == norm_new:
            out.append({"id": n.id, "title": n.title, "status": n.status,
                        "score": 1.0})
    return out
