"""DAG-agent v3 MCP 服务器：把讨论交接成任务，跑完可追踪。

工具面（全部走 v3 核心 SQLite+调度器）：
- dag3_create_mission(name, brief, root_check?, root_artifacts?) → mid,root
- dag3_add_node(mid, title, statement, parent?, deps?, check_cmd?, artifacts?)
- dag3_decompose(mid, node_id, children[], integration_check?)
- dag3_frontier / dag3_status / dag3_board / dag3_events
- dag3_pause / dag3_resume / dag3_cancel / dag3_recover
- dag3_run(mid, root?, max_concurrency?, max_retries?, max_calls?, max_cost?, real?)
    → 用调度器把任务跑到终局；real=true 时用 claude CLI×MiniMax 真执行
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from dag_agent.cc_backend import ClaudeCliBackend
from dag_agent.graph import Graph
from dag_agent.projection import render_mission
from dag_agent.recovery import RetryPolicy, cancel, pause, recover_mission, resume
from dag_agent.repository import Repository
from dag_agent.scheduler import Budget, Scheduler

mcp = FastMCP("dag-agent-v3")

_HOME = Path(os.environ.get(
    "DAG_AGENT_V3_HOME",
    Path.home() / ".dag-agent" / "v3" / "missions"))


def _repo_and_graph(mid: str) -> tuple[Repository, Graph]:
    repo = Repository(_HOME / mid / "mission.db")
    from dag_agent.assessment import default_checker3
    return repo, Graph(repo, checker3=default_checker3)


def _err(e: Exception) -> dict:
    return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def dag3_create_mission(name: str, brief: str = "", root_check: str = "",
                        root_artifacts: list[str] | None = None,
                        anchor: str = "", template: str = "") -> dict:
    """创建任务（讨论交接入口）。root_check 为根整合验收命令；artifacts 为根交付物；
    anchor 为外部权威锚（公共规范/RFC 引用，投影展示、终验对照）。
    template=research|code|docs|data：未给 root_check 时应用类别模板的
    异构根考卷（check_composite：存在+规模+结构小节，与子考卷异构）。"""
    try:
        from dag_agent.acceptance import preflight
        if not root_check and template:
            from dag_agent.task_templates import root_check_for, TEMPLATES
            if template not in TEMPLATES:
                return {"ok": False,
                        "error": f"未知模板 {template}：{list(TEMPLATES)}"}
            root_check = root_check_for(template)
            if not root_artifacts:
                from dag_agent.task_templates import TEMPLATES as _T
                root_artifacts = [_T[template]["root_artifact"]]
        if root_check:
            pf_ok, pf_why, _ = preflight(check_cmd=root_check)
            if not pf_ok:
                return {"ok": False, "error": f"根考卷体检未过：{pf_why}"}
        _HOME.mkdir(parents=True, exist_ok=True)
        mid_seed = tempfile.mkdtemp(prefix="m_", dir=_HOME)
        repo = Repository(Path(mid_seed) / "mission.db")
        g = Graph(repo)
        import shutil
        mid, root = g.create_mission(
            name, brief,
            root_check=root_check or None,
            root_declared_artifacts=tuple(root_artifacts or ()),
            anchor=anchor)
        final = _HOME / mid
        if Path(mid_seed) != final:
            repo.close()
            shutil.move(mid_seed, final)
            repo = Repository(final / "mission.db")
            repo.close()
        return {"ok": True, "mission_id": mid, "root": root}
    except Exception as e:
        return _err(e)


@mcp.tool()
def dag3_add_node(mid: str, title: str, statement: str = "", parent: str = "",
                  deps: list[str] | None = None, check_cmd: str = "",
                  artifacts: list[str] | None = None,
                  acceptance: list[dict] | None = None,
                  priority: int = 0, group_id: str = "",
                  group_order: int = 0, switch_after: int = 1,
                  exam_files: list[str] | None = None) -> dict:
    """加节点（生长闸门六条全过才落库）。parent=归属（必填，无主节点禁止）；
    deps=执行依赖；check_cmd/acceptance 创建时锁定；priority 大者先派。
    OR 替代组：同父同 group_id=OR 组（group_order 小者优先，switch_after=
    连续产物 FAIL 达数切支；ERROR 走维修不切支）。
    acceptance 为受限枚举清单：[{action: cmd|test|scan|script|http, ...参数}]。"""
    try:
        from dag_agent.acceptance import compile_all, preflight, validate
        if acceptance is not None:
            ok, why = validate(acceptance)
            if not ok:
                return {"ok": False, "error": f"验收清单语法：{why}"}
            cmd = " && ".join(compile_all(acceptance))
            pf_ok, pf_why, _ = preflight(acceptance=acceptance)
        else:
            cmd = check_cmd
            if cmd:
                pf_ok, pf_why, _ = preflight(check_cmd=cmd)
            else:
                pf_ok, pf_why = True, ""
        if not pf_ok:
            return {"ok": False, "error": f"考卷体检未过：{pf_why}"}
        repo, g = _repo_and_graph(mid)
        nid = g.add_node(mid, title, statement, parent_id=parent or None,
                         deps=tuple(deps or ()),
                         check_cmd=(check_cmd or cmd) or None,
                         declared_artifacts=tuple(artifacts or ()),
                         acceptance=acceptance, priority=priority,
                         group_id=group_id or None,
                         group_order=group_order,
                         switch_after=switch_after,
                         exam_files=tuple(exam_files or ()))
        repo.close()
        return {"ok": True, "node": nid}
    except Exception as e:
        return _err(e)


@mcp.tool()
def dag3_decompose(mid: str, node_id: str, children: list[dict],
                   integration_check: str = "") -> dict:
    """受控生长：children=[{title,statement,check_cmd,deps?,artifacts?}]。"""
    try:
        repo, g = _repo_and_graph(mid)
        kids = g.decompose(mid, node_id, children,
                           integration_check=integration_check or None)
        repo.close()
        return {"ok": True, "children": kids}
    except Exception as e:
        return _err(e)


@mcp.tool()
def dag3_frontier(mid: str) -> dict:
    """当前可立即执行的叶子节点。"""
    try:
        repo, g = _repo_and_graph(mid)
        out = {"ok": True, "frontier": g.frontier(mid),
               "integration_ready": g.integration_ready(mid)}
        repo.close()
        return out
    except Exception as e:
        return _err(e)


@mcp.tool()
def dag3_status(mid: str) -> dict:
    """任务总状态：mission/各节点/尝试计数。"""
    try:
        repo, g = _repo_and_graph(mid)
        m = repo.get_mission(mid)
        nodes = [{"id": n.id, "title": n.title, "status": n.status,
                  "kind": n.kind, "parent": n.parent_id} for n in repo.list_nodes(mid)]
        repo.close()
        return {"ok": True, "mission": m.status, "goal_version": m.goal_version,
                "nodes": nodes}
    except Exception as e:
        return _err(e)


@mcp.tool()
def dag3_board(mid: str) -> dict:
    """渲染 Obsidian 投影（任务总览.md+当前任务图.canvas+节点档案），返回总览路径。
    R3：统一走版本化入口（事件号守卫+原子发布），不再用无版本旧渲染。"""
    try:
        from dag_agent.projection import render_mission_versioned
        repo, g = _repo_and_graph(mid)
        path = render_mission_versioned(repo, g, mid)
        if path is None:
            path = repo.db_path.parent / "board" / "任务总览.md"
        repo.close()
        return {"ok": True, "overview": str(path)}
    except Exception as e:
        return _err(e)


@mcp.tool()
def dag3_events(mid: str, limit: int = 30) -> dict:
    """事件日志（append-only，最近 limit 条）。"""
    try:
        repo, g = _repo_and_graph(mid)
        evs = repo.events_of(mid)[-limit:]
        repo.close()
        return {"ok": True, "events": [
            {"at": e.at, "type": e.type, "node": e.node_id,
             "payload": json.loads(e.payload_json)} for e in evs]}
    except Exception as e:
        return _err(e)


@mcp.tool()
def dag3_pause(mid: str, node_id: str, reason: str = "paused") -> dict:
    try:
        repo, g = _repo_and_graph(mid)
        pause(repo, g, mid, node_id, reason)
        repo.close()
        return {"ok": True}
    except Exception as e:
        return _err(e)


@mcp.tool()
def dag3_resume(mid: str, node_id: str) -> dict:
    try:
        repo, g = _repo_and_graph(mid)
        resume(repo, g, mid, node_id)   # R2：可重复直达（非 blocked 即报错不假成功）
        repo.close()
        from dag_agent.daemon import RunManager
        kick = RunManager.get(_HOME).kick(mid)  # R2：恢复后自动续跑
        return {"ok": True, "kick": kick}
    except Exception as e:
        return _err(e)


@mcp.tool()
def dag3_cancel(mid: str, node_id: str, reason: str = "cancelled") -> dict:
    try:
        repo, g = _repo_and_graph(mid)
        cancel(repo, g, mid, node_id, reason)
        repo.close()
        return {"ok": True}
    except Exception as e:
        return _err(e)


@mcp.tool()
def dag3_recover(mid: str) -> dict:
    """服务重启后的恢复扫描：running 尝试孤儿化、节点回可派发。
    R2 防护：调度器活跃时拒绝（防误割在途）。"""
    try:
        from dag_agent.daemon import RunManager
        st = RunManager.get(_HOME).status(mid)
        if st.get("running"):
            return {"ok": False,
                    "error": f"任务 {mid} 调度器在途（inflight="
                             f"{st.get('inflight')}）：请先 dag3_run_stop"}
        repo, g = _repo_and_graph(mid)
        info = recover_mission(repo, g, mid)
        repo.close()
        return {"ok": True, **info}
    except Exception as e:
        return _err(e)


@mcp.tool()
def dag3_run(mid: str, root: str = "", max_concurrency: int = 2,
             max_retries: int = 2, max_calls: int | None = None,
             max_cost_usd: float | None = None, real: bool = False,
             timeout: int = 1800, backend_name: str | None = None,
             backend_options: dict | None = None,
             provider_pause_ack: bool = False) -> dict:
    """用调度器把任务跑到终局（阻塞式）。real=true 用 claude CLI×MiniMax。
    R2：统一经 RunManager（与 run_start 同一入口，绝不产生第二调度器）。
    成本/次数上限默认均不设（小孟 2026-10-06 决定：不限、费用如实记账；
    费马级大图 511 节点下固定默认 32 会中途误杀）；失控保护由每节点重试
    上限（RetryPolicy）+死局检测（无进展停机）承担；显式传入上限时
    熔断机制照常生效。"""
    try:
        from dag_agent.daemon import RunManager
        if real:
            from dag_agent.backends import create_backend
            probe = create_backend(backend_name, backend_options)
            avail, why = probe.available()
            if not avail:
                return {"ok": False, "error": f"真执行后端不可用：{why}"}
        mgr = RunManager.get(_HOME)
        out = mgr.run_blocking(
            mid, root=root or None, timeout=timeout,
            max_concurrency=max_concurrency, max_retries=max_retries,
            max_calls=max_calls, max_cost_usd=max_cost_usd, real=real,
            backend_name=backend_name, backend_options=backend_options,
            provider_pause_ack=provider_pause_ack)
        return out
    except Exception as e:
        return _err(e)


@mcp.tool()
def dag3_repair_check(mid: str, node_id: str, check_cmd: str = "",
                      acceptance: list[dict] | None = None,
                      reason: str = "",
                      exam_files: list[str] | None = None) -> dict:
    """考卷维修（出题方专属）：ERROR 后修考卷——新考卷过体检、版本+1留痕、
    旧验收证据作废、节点回 pending 重验。T6'：exam_files 同步更新（None=继承
    且必须覆盖新入口；显式节点空清单拒绝）。执行者会话不应调用本工具。"""
    try:
        repo, g = _repo_and_graph(mid)
        v = g.repair_check(mid, node_id, new_check_cmd=check_cmd or None,
                           acceptance=acceptance, reason=reason,
                           exam_files=exam_files)
        repo.close()
        return {"ok": True, "check_version": v}
    except Exception as e:
        return _err(e)


@mcp.tool()
def dag3_set_priority(mid: str, node_id: str, priority: int) -> dict:
    """调整节点调度优先级（大者先派）——人中途干预方向的通道。"""
    try:
        repo, g = _repo_and_graph(mid)
        g.set_priority(mid, node_id, priority)
        repo.close()
        return {"ok": True}
    except Exception as e:
        return _err(e)


@mcp.tool()
def dag3_set_strength(mid: str, node_id: str, strength: str) -> dict:
    """强度记账：交付时如实填"做到了什么/没做到什么"，防名实不符。"""
    try:
        repo, g = _repo_and_graph(mid)
        g.declare_strength(mid, node_id, strength)
        repo.close()
        return {"ok": True}
    except Exception as e:
        return _err(e)


@mcp.tool()
def dag3_selfcheck(mid: str, node_id: str) -> dict:
    """执行者自检（反馈角色，零权威）：跑同一考卷，落账 authoritative=0，
    不改变节点状态——只有平台 submit 的验收才入账。"""
    try:
        repo, g = _repo_and_graph(mid)
        out = g.self_check(mid, node_id)
        repo.close()
        return {"ok": True, **out}
    except Exception as e:
        return _err(e)


@mcp.tool()
def dag3_recall(mid: str, query: str, limit: int = 10) -> dict:
    """外部记忆检索：按查询找已有节点（复用先于生长——提议新节点前必查）。"""
    try:
        repo, g = _repo_and_graph(mid)
        from dag_agent.memory import recall
        hits = recall(repo, mid, query, limit=limit)
        repo.close()
        return {"ok": True, "hits": hits}
    except Exception as e:
        return _err(e)


@mcp.tool()
def dag3_review(mid: str, node_id: str, verdict: str, note: str = "",
                blind: bool = True) -> dict:
    """主观层审阅（盲读证词）：独立会话产出，append-only，永不与机器验收合并。
    verdict ∈ accept|concern|reject。"""
    try:
        repo, g = _repo_and_graph(mid)
        rid = g.attach_review(mid, node_id, verdict, note, blind=blind)
        repo.close()
        return {"ok": True, "review_id": rid}
    except Exception as e:
        return _err(e)


@mcp.tool()
def dag3_close_subjective(mid: str, note: str = "") -> dict:
    """显式降级：主观层闭合（closure_basis=subjective）。机器验不了的任务
    如实标注，不冒充机器闭合。"""
    try:
        repo, g = _repo_and_graph(mid)
        basis = g.close_mission_subjective(mid, note=note)
        repo.close()
        return {"ok": True, "closure_basis": basis}
    except Exception as e:
        return _err(e)


@mcp.tool()
def dag3_affected(mid: str, node_id: str) -> dict:
    """受影响面：谁传递依赖该节点+其归属闭包（增量重验的依据）。"""
    try:
        repo, g = _repo_and_graph(mid)
        out = g.affected_subgraph(mid, node_id)
        repo.close()
        return {"ok": True, "affected": out}
    except Exception as e:
        return _err(e)


class _NullBackend:
    name = "null"

    def run(self, statement, workdir, check_cmd, title=""):
        return {"ok": True, "summary": "null backend（无交付）", "session_id": "",
                "resources": {"cost_usd": 0.0, "duration_ms": 0}}


# ---- v3.2 运行接线与版本协议工具面 ----

@mcp.tool()
def dag3_run_start(mid: str, root: str = "", max_concurrency: int = 2,
                   max_retries: int = 2, max_calls: int | None = None,
                   max_cost_usd: float | None = None, real: bool = False,
                   node_cap: int | None = 200, project: bool = True,
                   timeout: int = 3600, auto_split: bool = True,
                   backend_name: str | None = None,
                   backend_options: dict | None = None,
                   provider_pause_ack: bool = False) -> dict:
    """常驻启动（非阻塞）：后台线程跑调度循环；一个任务仅一个有效调度器。
    启动即做单服务重启恢复（旧 running 一律收割，旧进程不获新提交权）。
    运行中可继续用本 MCP 的 status/frontier/set_priority/pause/resume/cancel
    干预——下一可派发 tick 生效，priority 不抢占已执行会话。
    auto_split（A-G1，默认 True）：执行者写入 .dag/proposal.json 的拆分建议
    自动进入独立出题+权威事务应用的受控生长链路，实现自动任务分解。"""
    try:
        from dag_agent.daemon import RunManager
        mgr = RunManager.get(_HOME)
        return mgr.start(mid, root=root, max_concurrency=max_concurrency,
                         max_retries=max_retries, max_calls=max_calls,
                         max_cost_usd=max_cost_usd, real=real,
                         node_cap=node_cap, project=project,
                         timeout_s=timeout, auto_split=auto_split,
                         backend_name=backend_name, backend_options=backend_options,
                         provider_pause_ack=provider_pause_ack)
    except Exception as e:
        return _err(e)


@mcp.tool()
def dag3_split_proposals(mid: str, status: str | None = None) -> dict:
    """查询自动生长提案队列（A-G1）：状态 queued/applied/rejected/discarded、
    源尝试、指纹、出题后端、实际新增节点数与拒绝原因。"""
    try:
        repo, _g = _repo_and_graph(mid)
        try:
            rows = repo.list_split_proposals(mid, status=status)
            out = []
            for r in rows:
                out.append({
                    "id": r["id"], "node_id": r["node_id"],
                    "source_attempt_id": r["source_attempt_id"],
                    "status": r["status"],
                    "content_sha256": r["content_sha256"][:16],
                    "plan_backend": r["plan_backend"],
                    "plan_attempts": r["plan_attempts"],
                    "nodes_added": r["nodes_added"],
                    "children_created": json.loads(
                        r["children_created_json"] or "[]"),
                    "reject_reason": r["reject_reason"][:200],
                    "created_at": r["created_at"],
                    "decided_at": r["decided_at"]})
            return {"ok": True, "proposals": out, "count": len(out)}
        finally:
            repo.close()
    except Exception as e:
        return _err(e)


@mcp.tool()
def dag3_run_status(mid: str) -> dict:
    """查询常驻运行状态（运行中/在途数/预算累计/任务状态/halt原因）。"""
    try:
        from dag_agent.daemon import RunManager
        return RunManager.get(_HOME).status(mid)
    except Exception as e:
        return _err(e)


@mcp.tool()
def dag3_run_stop(mid: str, mode: str = "drain") -> dict:
    """停止常驻运行：drain=不再派发、在途有界结束；kill=drain+取消在途尝试
    并请求后端终止进程树；以 stopped=True 和调度线程退出为完成依据。"""
    try:
        from dag_agent.daemon import RunManager
        return RunManager.get(_HOME).stop(mid, mode=mode)
    except Exception as e:
        return _err(e)


@mcp.tool()
def dag3_change_goal(mid: str, reason: str) -> dict:
    """目标变更：goal_version+1，在途尝试过期（提交将被拒），事件留痕。"""
    try:
        repo, g = _repo_and_graph(mid)
        v = g.change_goal(mid, reason)
        repo.close()
        return {"ok": True, "goal_version": v}
    except Exception as e:
        return _err(e)


@mcp.tool()
def dag3_revise(mid: str, node_id: str, reason: str) -> dict:
    """产物修订（版本协议）：revision+1、快照与证据作废、反向依赖与整合祖先
    级联失效、在途取消；mission 已 done 自动重开等待链条重建。"""
    try:
        repo, g = _repo_and_graph(mid)
        v = g.revise_node(mid, node_id, reason)
        repo.close()
        return {"ok": True, "revision": v}
    except Exception as e:
        return _err(e)


@mcp.tool()
def dag3_reopen(mid: str, reason: str) -> dict:
    """显式重开已 done 任务（根回等待态，重新调度）。"""
    try:
        repo, g = _repo_and_graph(mid)
        g.reopen_mission(mid, reason)
        repo.close()
        return {"ok": True}
    except Exception as e:
        return _err(e)


@mcp.tool()
def dag3_templates() -> dict:
    """v3 任务模板库：类别默认值（timeout/考卷条数/粒度/重试/节点熔断）与
    异构根考卷命令；provenance 标注默认值来源（本机实测，非P95统计）。"""
    try:
        from dag_agent.task_templates import TEMPLATES, root_check_for, \
            scheduler_defaults
        return {"ok": True,
                "templates": {k: {**{kk: vv for kk, vv in t.items()
                                     if kk != "leaf_acceptance_example"},
                                  "root_check": root_check_for(k),
                                  "scheduler": scheduler_defaults(k)}
                              for k, t in TEMPLATES.items()}}
    except Exception as e:
        return _err(e)


@mcp.tool()
def dag3_check_briefing(text: str) -> dict:
    """派发前简报五要素校验（A3）：输入/交付清单/接口签名/样例/自测入口
    以【段标记】结构化书写，齐全才放行（briefing.ok=true）；
    无绝对路径与超 800 字为告警（warnings），不硬拒。"""
    from dag_agent.briefing import check_briefing
    try:
        return {"ok": True, "briefing": check_briefing(text)}
    except Exception as e:
        return _err(e)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
