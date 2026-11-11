"""v3 步骤6：Obsidian 投影——事件驱动、可重建的人机同视图。

产物（写入 <mission_dir>/board/，数据库是唯一事实源，删了能整目录重建）：
- 任务总览.md：状态+节点表+时间线+验收记录
- 当前任务图.canvas：JSON Canvas 开放格式（节点/坐标/有向连线）
- 节点/<nid>.md：单节点陈述/尝试/工件/验收全档案
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

from .repository import Repository

_STATUS_ICON = {"pending": "⚪", "waiting_children": "🟦", "in_work": "🔄",
                "submitted": "📤", "accepted": "✅", "failed": "❌",
                "blocked": "🚫", "cancelled": "⬛"}

# R3：版本判读→渲染→原子发布→标记 全程串行——并发渲染永不回退或擦除版本
_RENDER_LOCK = threading.Lock()


def current_version(repo: Repository, mid: str) -> int:
    """投影版本=事件序号（单调），用于守卫：旧投影永不覆盖新投影。"""
    row = repo._conn.execute(
        "SELECT COALESCE(MAX(id),0) AS v FROM events WHERE mission_id=?",
        (mid,)).fetchone()
    return row["v"]


def render_mission_versioned(repo: Repository, graph, mid: str) -> Path | None:
    """v3.2 运行接线入口（R3 串行化）：事件无推进则跳过；渲染走原子替换
    （tmp+os.replace）；完成后落版本标记。删除 board/ 可整目录重建。
    返回总览路径；跳过返回 None。"""
    with _RENDER_LOCK:
        board = Path(repo.db_path).parent / "board"
        marker = board / ".projection_version"
        version = current_version(repo, mid)
        if marker.is_file():
            try:
                if version <= int(marker.read_text(encoding="utf-8").strip()):
                    return None
            except ValueError:
                pass
        overview = render_mission(repo, graph, mid)
        with open(overview, "r", encoding="utf-8") as f:
            text = f.read()
        text = text.replace(
            "*由 DAG-agent v3.1 projection 渲染",
            f"*投影版本：事件#{version}　由 DAG-agent v3.2 projection 渲染")
        _atomic_write(overview, text)
        marker.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(marker, str(version))
        return overview


def _atomic_write(path: Path, text: str):
    """原子发布：写临时文件后 os.replace——读者永不看到半截文件。
    支持 Windows 下瞬态共享锁与杀毒扫描重试（最多 5 次指数退避），超限清理临时文件并抛出明确错误。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    last_err: OSError | None = None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        for i in range(5):
            try:
                os.replace(tmp, path)
                last_err = None
                break
            except OSError as e:
                last_err = e
                if i == 4:
                    raise
                time.sleep(0.05 * (i + 1))
    except BaseException:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        raise


def render_mission(repo: Repository, graph, mid: str) -> Path:
    """渲染整个任务投影，返回 任务总览.md 路径。幂等（同状态同输出）。"""
    mission = repo.get_mission(mid)
    nodes = repo.list_nodes(mid)
    board = Path(repo.db_path).parent / "board"
    (board / "节点").mkdir(parents=True, exist_ok=True)

    # ---- 任务总览.md ----
    basis_label = {"machine": "机器闭合", "subjective": "主观层闭合（降级）"}.get(
        mission.closure_basis, mission.closure_basis)
    lines = [f"# {mission.name}",
             "",
             f"> {mission.brief}" if mission.brief else "",
             "",
             f"**任务状态：{'🎉 已完成' if mission.status == 'done' else '🚧 ' + mission.status}**"
             f"　|　目标版本 v{mission.goal_version}"
             f"　|　闭合依据 **{basis_label}**"
             f"　|　节点 {sum(1 for n in nodes if n.status == 'accepted')}/{len(nodes)} accepted"
             f"　|　创建 {mission.created_at}",
             ""]
    if getattr(mission, "anchor", ""):
        lines += [f"> 外部权威锚：{mission.anchor}", ""]
    lines += ["```mermaid", _mermaid(nodes), "```", "", "## 节点明细", "",
              "| ID | 状态 | 标题 | 类型 | 依赖 | 优先级 | 验收命令 | 产物版本 | 强度 |",
              "|---|---|---|---|---|---|---|---|---|"]
    for n in nodes:
        deps = ",".join(d.depends_on for d in repo.deps_of(n.id)) or "—"
        cmd = (n.check_cmd or "—")[:60]
        strength = (getattr(n, "strength_actual", "") or "—")[:40]
        lines.append(f"| {n.id} | {_STATUS_ICON.get(n.status, n.status)} {n.status} "
                     f"| {n.title} | {n.kind} | {deps} | {getattr(n, 'priority', 0)} "
                     f"| {cmd} | "
                     f"{n.accepted_artifact_version or '—'} | {strength} |")
    lines += ["", "## 时间线（事件，append-only）", "", "```text"]
    for e in repo.events_of(mid):
        lines.append(f"{e.at} {e.type} {e.node_id or ''} {e.payload_json[:120]}")
    lines += ["```", "", "## 验收记录（证据分级：authoritative=平台终检；自检=反馈非证据）",
              ""]
    any_assess = False
    for n in nodes:
        for a in repo.assessments_of(n.id):
            any_assess = True
            marks = []
            if not a.authoritative:
                marks.append("自检")
            if a.invalid:
                marks.append("已作废")
            mark = f"（{'·'.join(marks)}）" if marks else ""
            lines.append(f"- `{n.id}` v{a.artifact_version} 目标v{a.goal_version} "
                         f"→ **{a.verdict}**（{a.method}）{mark}{a.evidence[:100]}")
    if not any_assess:
        lines.append("_暂无_")
    lines += ["", "## 主观层审阅（盲读证词，永不与机器结果合并）", ""]
    any_review = False
    for n in nodes:
        for r in repo.reviews_of(n.id):
            any_review = True
            lines.append(f"- `{n.id}` → **{r.verdict}**"
                         f"{'（盲读）' if r.blind else '（非盲）'} {r.note[:120]}")
    if not any_review:
        lines.append("_暂无_")
    lines += ["", "---", "*由 DAG-agent v3.1 projection 渲染；数据库为唯一事实源，本目录可整目录重建。*"]
    overview = board / "任务总览.md"
    _atomic_write(overview, "\n".join(l for l in lines if l is not None))

    # ---- 当前任务图.canvas ----
    canvas = _canvas(nodes, repo, mid)
    _atomic_write(board / "当前任务图.canvas",
                  json.dumps(canvas, ensure_ascii=False, indent=1))

    # ---- 节点/<nid>.md ----
    for n in nodes:
        _render_node(repo, board / "节点" / f"{n.id}.md", n)
    return overview


def _mermaid(nodes) -> str:
    out = ["graph TD"]
    for n in nodes:
        icon = _STATUS_ICON.get(n.status, "")
        out.append(f'    {n.id}["{icon} {n.id} {n.title}"]')
    by_id = {n.id: n for n in nodes}
    for n in nodes:
        if n.parent_id and n.parent_id in by_id:
            out.append(f"    {n.parent_id} --> {n.id}")
    out.append("    classDef done fill:#d4edda,stroke:#2e7d32;")
    out.append("    classDef open fill:#e3f2fd,stroke:#1565c0;")
    out.append("    classDef bad fill:#ffe0b2,stroke:#ef6c00;")
    for n in nodes:
        cls = "done" if n.status == "accepted" else (
            "bad" if n.status in ("failed", "blocked", "cancelled") else "open")
        out.append(f"    class {n.id} {cls}")
    return "\n".join(out)


def _canvas(nodes, repo: Repository, mid: str) -> dict:
    """JSON Canvas 开放格式：节点卡+父子有向连线。"""
    import math
    by_level: dict[int, list] = {}
    for n in nodes:
        depth = 0
        cur = n
        seen = set()
        while cur.parent_id and cur.parent_id in {x.id for x in nodes} and cur.id not in seen:
            seen.add(cur.id)
            depth += 1
            cur = next(x for x in nodes if x.id == cur.parent_id)
        by_level.setdefault(depth, []).append(n)
    canvas_nodes = []
    canvas_edges = []
    for depth, row in sorted(by_level.items()):
        for i, n in enumerate(row):
            canvas_nodes.append({
                "id": n.id, "type": "text",
                "x": i * 260, "y": depth * 180,
                "width": 240, "height": 120,
                "text": f"{_STATUS_ICON.get(n.status, '')} **{n.title}**\n{n.status}"
                        f"\n`{n.id}`",
            })
    for n in nodes:
        if n.parent_id and any(c["id"] == n.parent_id for c in canvas_nodes):
            canvas_edges.append({"id": f"e-{n.parent_id}-{n.id}",
                                 "fromNode": n.parent_id, "toNode": n.id,
                                 "fromSide": "bottom", "toSide": "top"})
    return {"nodes": canvas_nodes, "edges": canvas_edges}


def _render_node(repo: Repository, path: Path, n):
    deps = [d.depends_on for d in repo.deps_of(n.id)]
    dependents = [r["node_id"] for r in repo._conn.execute(
        "SELECT node_id FROM dependencies WHERE depends_on=?", (n.id,)).fetchall()]
    children = [r["id"] for r in repo._conn.execute(
        "SELECT id FROM nodes WHERE parent_id=?", (n.id,)).fetchall()]
    lines = [f"# {n.id} {n.title}", "",
             f"- 状态：{_STATUS_ICON.get(n.status, '')} `{n.status}`",
             f"- 类型：{n.kind}　父节点：{n.parent_id or '—'}"
             f"　优先级：{getattr(n, 'priority', 0)}",
             f"- 陈述：{n.statement or '—'}",
             f"- 验收命令：`{n.check_cmd or '未配置(fail-closed)'}`"
             f"（v{n.check_version}，创建时锁定）",
             f"- 受限枚举清单：{getattr(n, 'acceptance_json', None) or '—'}",
             f"- 已验收产物版本：{n.accepted_artifact_version or '—'}",
             f"- 实际强度：{getattr(n, 'strength_actual', '') or '—（未申报）'}",
             f"- 引用（依赖）：{', '.join(deps) or '—'}",
             f"- 被引用（下游）：{', '.join(dependents) or '—'}",
             f"- 孩子（归属）：{', '.join(children) or '—'}", "",
             "## 尝试与工件", ""]
    atts = repo._conn.execute(
        "SELECT * FROM attempts WHERE node_id=? ORDER BY started_at", (n.id,)).fetchall()
    if not atts:
        lines.append("_无尝试_")
    for a in atts:
        res = json.loads(a["resources_json"] or "{}")
        lines.append(f"### {a['id']} — {a['status']}（{a['backend']}）")
        lines.append(f"- 结果：{a['result_summary'][:200] or '—'}")
        if res:
            cost = res.get("cost")
            if isinstance(cost, dict):
                amount = cost.get("amount")
                basis = cost.get("basis") or "unknown"
            else:
                amount = res.get("cost_usd")
                basis = "legacy"
            cost_text = f"${amount} ({basis})" if amount is not None else "unknown"
            session = (res.get("session_id") or res.get("pi_session_id")
                       or res.get("agent_id") or "")
            lines.append(f"- 资源：cost={cost_text} "
                         f"duration={res.get('duration_ms')}ms "
                         f"session={session[:12]}")
    arts = repo.artifacts_of(n.id)
    if arts:
        lines += ["", "## 工件版本", ""]
        for art in arts:
            lines.append(f"- v{art.version} `{art.rel_path}` sha256:{art.content_hash[:16]}…")
    reviews = repo.reviews_of(n.id)
    if reviews:
        lines += ["", "## 主观层审阅", ""]
        for r in reviews:
            lines.append(f"- **{r.verdict}**"
                         f"{'（盲读）' if r.blind else '（非盲）'} {r.note[:160]}")
    lines += ["", "---", f"*节点档案由 projection 渲染于任务库 {repo.db_path.name}*"]
    _atomic_write(path, "\n".join(lines))
