"""DAG-agent MCP 服务器：把任务DAG能力暴露为MCP工具（stdio传输）。

运行：  python mcp_server.py
注册（项目根 .mcp.json）：
    {"mcpServers": {"dag-agent": {
        "command": "<python>",
        "args": ["<本文件绝对路径>"]}}}

标准循环见 PROTOCOL.md：探讨 → create → decompose → frontier → 执行 → submit
→（级联闭合）→ root done → 汇报。节点生长过程实时渲染到 missions/<mid>/dag.md
（Obsidian直接打开；agent用 dag_board 读同一文件，人机同视图）。
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from dag_agent.store import DagError, MissionStore  # noqa: E402

mcp = FastMCP("dag-agent", instructions=(
    "DAG-agent：探讨定任务后，把任务变成可验证生长的DAG。标准循环："
    "dag_create_mission(探讨结论) → dag_decompose(子任务各带可判定checks) → "
    "dag_frontier(认领) → 执行并把产物放missions/<mid>/workspace/ → "
    "dag_submit(运行checks,零写入) → 级联闭合 → root done → 汇报。"
    "人看 missions/<mid>/dag.md（Obsidian），agent用 dag_board 读同一文件。"))


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _guarded(fn, *args, **kwargs) -> str:
    try:
        return _j({"ok": True, "result": fn(*args, **kwargs)})
    except DagError as exc:
        return _j({"ok": False, "rejected": True, "reason": str(exc)})
    except Exception as exc:  # noqa: BLE001
        return _j({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                   "trace": traceback.format_exc().strip().splitlines()[-4:]})


@mcp.tool()
def dag_create_mission(mid: str, name: str, brief: str, root_title: str,
                       root_statement: str) -> str:
    """创建任务（探讨定稿后调用）：mid为短ID，root是最终交付节点。"""
    return _guarded(MissionStore().create_mission,
                    mid, name, brief, root_title, root_statement)


@mcp.tool()
def dag_decompose(mid: str, parent_id: str, children: list[dict]) -> str:
    """分解父节点：children=[{title,statement,checks?}]。children无执行依赖
    （归属关系记在parent字段，Prove2Me sketch语义）；父节点转aggregate，
    children全部done后父节点自动闭合。"""
    return _guarded(MissionStore().decompose, mid, parent_id, children)


@mcp.tool()
def dag_add_node(mid: str, title: str, statement: str,
                 deps: list[str], checks: list[dict] | None = None) -> str:
    """直接新增节点（不经过分解）：deps为依赖节点ID列表。"""
    return _guarded(MissionStore().add_node, mid, title, statement,
                    deps, checks, "task")


@mcp.tool()
def dag_frontier(mid: str) -> str:
    """前沿：可立即认领的节点（open且依赖全done）。空了就分解或解阻塞。"""
    return _guarded(MissionStore().frontier, mid)


@mcp.tool()
def dag_submit(mid: str, node_id: str, summary: str) -> str:
    """提交节点结果：运行该节点checks（零写入守卫），PASS级联闭合，FAIL带修复提示。"""
    return _guarded(MissionStore().submit, mid, node_id, summary)


@mcp.tool()
def dag_block(mid: str, node_id: str, reason: str) -> str:
    """标记阻塞（客观暂时解决不了）：移出前沿，交由人类决策。"""
    return _guarded(MissionStore().block, mid, node_id, reason)


@mcp.tool()
def dag_unblock(mid: str, node_id: str) -> str:
    """解除阻塞（人类决策后），并重估级联闭合。"""
    return _guarded(MissionStore().unblock, mid, node_id)


@mcp.tool()
def dag_retry(mid: str, node_id: str | None = None) -> str:
    """重试闭合：aggregate的children全done但组合检查失败后，修复产物再调用
    （node_id缺省=根节点）。"""
    return _guarded(MissionStore().retry, mid, node_id)


@mcp.tool()
def dag_readback(mid: str, node_id: str, text: str, model: str = "") -> str:
    """盲读回审计：由未接触任务意图的独立agent盲写的证词（只描述交付物字面上
    做了/断言了什么），供人类对照原始意图找差距。open节点可附，done后冻结。
    审计员提示模板见 AUDITOR.md（独立性是关键：不要把意图告诉审计agent）。"""
    return _guarded(MissionStore().set_readback, mid, node_id, text, model)


@mcp.tool()
def dag_audit_prepare(mid: str, node_id: str) -> str:
    """盲读回自动派发准备：生成给独立审计子agent的自包含提示（只含节点陈述+
    产物路径+审计规则，结构性排除brief/意图）。宿主把prompt交给全新上下文的
    子agent，收回证词后调 dag_readback 附着。"""
    return _guarded(MissionStore().audit_prepare, mid, node_id)


@mcp.tool()
def dag_status(mid: str) -> str:
    """任务总览：计数/前沿/根节点状态/看板路径。"""
    return _guarded(MissionStore().status, mid)


@mcp.tool()
def dag_templates() -> str:
    """列出可用mission模板（research/code-feature/audit）及其骨架。"""
    def _list():
        from dag_agent.templates import TEMPLATES
        return {k: {"root": v["root_title"],
                    "children": [c["title"] for c in v["children"]]}
                for k, v in TEMPLATES.items()}
    return _guarded(_list)


@mcp.tool()
def dag_create_from_template(mid: str, name: str, brief: str,
                             template: str) -> str:
    """按模板创建mission：research调研/code-feature功能/audit审计。
    自动写入通用交付物校验器并分解好骨架，创建后直接 dag_frontier 认领。"""
    def _create():
        from dag_agent.templates import create_from_template
        return create_from_template(MissionStore(), mid, name, brief, template)
    return _guarded(_create)


@mcp.tool()
def dag_board(mid: str) -> str:
    """读Obsidian看板（人机同视图）：返回dag.md全文与路径。"""
    def _read():
        store = MissionStore()
        p = store.board_path(mid)
        if not p.exists():
            raise DagError(f"看板不存在。动作：先创建任务 '{mid}'")
        return {"board": str(p), "content": p.read_text(encoding="utf-8")}
    return _guarded(_read)


@mcp.tool()
def dag_events(mid: str, tail: int = 20) -> str:
    """事件日志（append-only，提交不可变），默认最近20条。"""
    return _guarded(MissionStore().read_events, mid, tail)


if __name__ == "__main__":
    mcp.run()
