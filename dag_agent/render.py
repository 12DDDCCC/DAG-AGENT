"""render.py — Obsidian看板渲染：mermaid DAG + 前沿 + 节点明细 + 盲读回 + 事件日志。

人在Obsidian里打开任务目录即可看板（mermaid原生渲染）；
agent用 dag_board / 直接Read同一文件——人机同视图。
每次重渲染前将旧版快照到 history/（默认保留10份，DAG_AGENT_HISTORY=0关闭）。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

HISTORY_KEEP = 10

# 看板写入进程内串行化：多线程并发提交时避免固定名tmp互踩/读取被replace的文件。
_RENDER_LOCK = threading.Lock()

STATUS_ICON = {"done": "✅", "open": "⬜", "blocked": "🚧"}
STATUS_CLASS = {"done": "stDone", "open": "stOpen", "blocked": "stBlocked"}


def _esc(s: str) -> str:
    return (s or "").replace('"', "'").replace("\n", " ").strip()


def render_index(store) -> Path:
    """多mission聚合看板：missions/BOARD.md——全部任务的进度总览（Obsidian首页）。"""
    with _RENDER_LOCK:
        return _render_index_locked(store.home)


def _render_index_locked(home: Path) -> Path:
    lines = ["# DAG-agent 任务总览", "",
             "> 全部mission的聚合页；每行可点进对应 dag.md 看节点生长细节。", ""]
    rows = []
    header_done = False
    for mid_dir in sorted(home.glob("*/")):
        if not header_done:
            lines.append("| 任务 | 名称 | 进度 | 阻塞 | 创建 | 最近事件 | 备注 |")
            lines.append("|---|---|---|---|---|---|---|")
            header_done = True
        state_p = mid_dir / "mission.json"
        if not state_p.exists():
            continue
        try:
            from dag_agent.store import _read_json_retry
            state = _read_json_retry(state_p)
        except (json.JSONDecodeError, FileNotFoundError):
            continue
        nodes = state["nodes"]
        done = sum(1 for n in nodes.values() if n["status"] == "done")
        blocked = sum(1 for n in nodes.values() if n["status"] == "blocked")
        root_done = nodes[state["root"]]["status"] == "done"
        icon = "🎉" if root_done else "🚧"
        # 阻塞原因（如有）
        blocked_info = ""
        if blocked:
            reasons = [_esc(n.get("blocked_reason") or "")[:30]
                       for n in nodes.values() if n["status"] == "blocked"]
            blocked_info = f"；🚧 {' / '.join(r for r in reasons if r)}"
        # 最近事件摘要（最后一条）
        ev_path = mid_dir / "events.jsonl"
        last_ev = "—"
        if ev_path.exists():
            ev_lines = ev_path.read_text(encoding="utf-8").strip().splitlines()
            for l in reversed(ev_lines):
                try:
                    rec = json.loads(l)
                except json.JSONDecodeError:
                    continue
                last_ev = f"{rec.get('type', '?')}@{rec.get('at', '?')[11:16]}"
                if rec.get("node"):
                    last_ev += f"({rec['node']})"
                break
        rows.append((state["created_at"],
                     f"| [{state['id']}](missions/{state['id']}/dag.md) "
                     f"| {icon} {state['name']} | {done}/{len(nodes)} | {blocked} |"
                     f" | {state['created_at'][:16]} | {last_ev}"
                     f"{blocked_info} |"))
    for _, row in sorted(rows, reverse=True):
        lines.append(row)
    if not rows:
        lines.append("_尚无任务。_")
    lines.append("")
    path = home / "BOARD.md"
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(path)
    return path


def _awaiting_retry(state: dict) -> bool:
    """存在open的aggregate，其children已全done但自身未闭合（组合检查失败待重试）。"""
    for nid, n in state["nodes"].items():
        if n["status"] != "open" or n["kind"] != "aggregate":
            continue
        children = [m for m in state["nodes"].values() if m.get("parent") == nid]
        if children and all(m["status"] == "done" for m in children):
            return True
    return False


def render_board(store, mid: str, state: dict) -> Path:
    with _RENDER_LOCK:
        return _render_board_locked(store, mid, state)


def _render_board_locked(store, mid: str, state: dict) -> Path:
    nodes = state["nodes"]
    cnt = {"done": 0, "open": 0, "blocked": 0}
    for n in nodes.values():
        cnt[n["status"]] += 1

    lines: list[str] = []
    lines.append(f"# {state['name']}")
    lines.append("")
    lines.append(f"> {state['brief']}")
    lines.append("")
    root_done = nodes[state["root"]]["status"] == "done"
    lines.append(f"**任务状态：{'🎉 已完成' if root_done else '🚧 进行中'}**"
                 f"　|　节点：{cnt['done']}/{len(nodes)} done"
                 f"，{cnt['open']} open，{cnt['blocked']} blocked"
                 f"　|　创建：{state['created_at']}")
    lines.append("")

    # ---- mermaid DAG ----
    lines.append("## DAG 图（节点生长过程）")
    lines.append("")
    lines.append("```mermaid")
    lines.append("graph TD")
    for nid, n in nodes.items():
        icon = STATUS_ICON[n["status"]]
        label = _esc(n["title"])
        auto = " ⟂" if n.get("auto_resolved") else ""
        lines.append(f'    {nid}["{icon} {nid} {label}{auto}"]')
    for nid, n in nodes.items():
        for dep in n["deps"]:
            lines.append(f"    {dep} --> {nid}")
        if n.get("parent"):
            lines.append(f"    {n['parent']} --> {nid}")
    lines.append("    classDef stDone fill:#d4edda,stroke:#2e7d32;")
    lines.append("    classDef stOpen fill:#e3f2fd,stroke:#1565c0;")
    lines.append("    classDef stBlocked fill:#ffe0b2,stroke:#ef6c00;")
    for nid, n in nodes.items():
        lines.append(f"    class {nid} {STATUS_CLASS[n['status']]}")
    lines.append("```")
    lines.append("")

    # ---- 前沿 ----
    lines.append("## 前沿（可立即认领）")
    lines.append("")
    fr = store.frontier(mid)
    if not fr:
        if root_done:
            lines.append("_无前沿：任务已全部完成。_")
        elif _awaiting_retry(state):
            lines.append("_无前沿：存在组合检查失败的aggregate节点——"
                         "修复对应产物后调用 dag_retry 重试闭合。_")
        elif cnt["blocked"]:
            lines.append("_无前沿：存在阻塞节点，需要人类决策（等外部/换路径/降级）。_")
        else:
            lines.append("_无前沿：所有open节点都有未完成依赖（异常状态，检查依赖环）。_")
    else:
        for b in fr:
            checks = "、".join(b["checks"]) or "（无checks，凭陈述验收）"
            lines.append(f"- **{b['id']} {b['title']}** — {b['statement']}"
                         f"　`检查: {checks}`")
    lines.append("")

    # ---- 节点明细 ----
    lines.append("## 节点明细")
    lines.append("")
    lines.append("| ID | 状态 | 标题 | 类型 | 依赖 | 陈述 | 结果 |")
    lines.append("|---|---|---|---|---|---|---|")
    for nid, n in nodes.items():
        icon = STATUS_ICON[n["status"]]
        deps = "、".join(n["deps"]) or "—"
        kind = "aggregate(自动)" if n["kind"] == "aggregate" else "task"
        res = ""
        r = n.get("result")
        if isinstance(r, dict):
            res = _esc(r.get("summary", ""))
            if r.get("attempts"):
                res += f"（失败{r['attempts']}次）"
        if n["status"] == "blocked":
            res = _esc(n.get("blocked_reason") or "")
        lines.append(f"| {nid} | {icon} {n['status']} | {_esc(n['title'])} "
                     f"| {kind} | {deps} | {_esc(n['statement'])} | {res} |")
    lines.append("")

    # ---- 盲读回审计（仅有证词时展示）----
    rbs = [(nid, rb) for nid, n in nodes.items()
           for rb in n.get("readbacks", [])]
    if rbs:
        lines.append("## 盲读回审计（独立agent盲写证词，人对照意图找差距）")
        lines.append("")
        for nid, rb in rbs:
            excerpt = _esc(rb["text"][:200])
            lines.append(f"- **{nid}**（{rb.get('model') or 'unknown'} @ {rb['at']}）：{excerpt}")
        lines.append("")

    # ---- 事件日志 ----
    lines.append("## 事件日志（最近20条，append-only）")
    lines.append("")
    lines.append("```text")
    for rec in store.read_events(mid, tail=20):
        lines.append(json.dumps(rec, ensure_ascii=False))
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append(
        f"*由 DAG-agent v0.1.0 渲染于 {state['created_at']} 后的最近一次变更；"
        "agent可Read本文件，人可在Obsidian直接查看。*")

    path = store.board_path(mid)
    if path.exists() and os.environ.get("DAG_AGENT_HISTORY", "1") != "0":
        hist = path.parent / "history"
        hist.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        (hist / f"dag-{stamp}.md").write_text(
            path.read_text(encoding="utf-8"), encoding="utf-8")
        snaps = sorted(hist.glob("dag-*.md"))
        for old in snaps[:-HISTORY_KEEP]:
            old.unlink()
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(path)
    return path
