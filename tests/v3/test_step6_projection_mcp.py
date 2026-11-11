"""步骤6验收：插件与展示——投影可重建/Canvas结构合法/MCP工具面/交接一条龙。"""
from __future__ import annotations

import os

import json
from pathlib import Path

import pytest

from dag_agent.graph import Graph
from dag_agent.projection import render_mission
from dag_agent.repository import Repository
from dag_agent.scheduler import Budget, Scheduler

from test_step4_scheduler import FakeBackend, _add

CHK = ("python -c \"import pathlib,sys; sys.exit(0 if any("
       "pathlib.Path('.').rglob('*.txt')) else 1)\"")


@pytest.fixture
def built(tmp_path):
    repo = Repository(tmp_path / "mission.db")
    g = Graph(repo)
    be = FakeBackend(delay=0.02)
    s = Scheduler(repo, g, be, max_concurrency=2, budget=Budget(max_calls=64))
    mid, root = g.create_mission("展示任务", "验收投影", root_check=CHK)
    _add((s, g, repo, be), mid, "A", ["FILE:a.txt:A1"], root)
    _add((s, g, repo, be), mid, "B", ["FILE:b.txt:B1"], root)
    summary = s.run_until_done(mid, root, timeout=90)
    assert summary["mission_status"] == "done"
    yield repo, g, mid, tmp_path
    repo.close()


def test_projection_renders_all_artifacts(built):
    repo, g, mid, _ = built
    overview = render_mission(repo, g, mid)
    board = overview.parent
    assert (board / "任务总览.md").is_file()
    assert (board / "当前任务图.canvas").is_file()
    nodes_dir = board / "节点"
    assert nodes_dir.is_dir() and len(list(nodes_dir.glob("*.md"))) >= 3
    text = (board / "任务总览.md").read_text(encoding="utf-8")
    assert "展示任务" in text and "验收记录" in text and "时间线" in text


def test_canvas_is_valid_json_canvas(built):
    repo, g, mid, _ = built
    overview = render_mission(repo, g, mid)
    data = json.loads((overview.parent / "当前任务图.canvas").read_text(encoding="utf-8"))
    assert set(data.keys()) >= {"nodes", "edges"}
    ids = {n["id"] for n in data["nodes"]}
    for e in data["edges"]:
        assert e["fromNode"] in ids and e["toNode"] in ids
    # 父子边齐全：root→A、root→B
    pairs = {(e["fromNode"], e["toNode"]) for e in data["edges"]}
    root = repo.list_nodes(mid)[0].id
    kids = [n.id for n in repo.list_nodes(mid) if n.parent_id == root]
    for k in kids:
        assert (root, k) in pairs


def test_projection_rebuildable_idempotent(built):
    repo, g, mid, _ = built
    first = render_mission(repo, g, mid)
    content1 = (first.parent / "当前任务图.canvas").read_text(encoding="utf-8")
    md1 = first.read_text(encoding="utf-8")
    # 删掉整个 board 重建
    import shutil
    shutil.rmtree(first.parent)
    second = render_mission(repo, g, mid)
    assert second == first
    assert (second.parent / "当前任务图.canvas").read_text(encoding="utf-8") == content1
    assert second.read_text(encoding="utf-8") == md1


def test_node_profile_contains_assessment(built):
    repo, g, mid, _ = built
    overview = render_mission(repo, g, mid)
    nodes_dir = overview.parent / "节点"
    root_id = repo.list_nodes(mid)[0].id
    profile = (nodes_dir / f"{root_id}.md").read_text(encoding="utf-8")
    assert "fail-closed" not in profile  # root 有验收命令
    assert "验收命令" in profile and "尝试与工件" in profile


def test_mcp_server_v3_tool_roundtrip(tmp_path):
    """官方 SDK 起 v3 MCP 服务器：create→add→status→board 全链路。"""
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    env = os.environ.copy()
    env["DAG_AGENT_V3_HOME"] = str(tmp_path / "mcp_home")
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).resolve().parents[2] / "mcp_server_v3.py")],
        env=env)

    import anyio

    async def _drive():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as sess:
                await sess.initialize()
                created = await sess.call_tool("dag3_create_mission", {
                    "name": "MCP交接", "brief": "从讨论到看板",
                    "root_check": CHK, "root_artifacts": ["report.txt"]})
                mid = json.loads(created.content[0].text)["mission_id"]
                added = await sess.call_tool("dag3_add_node", {
                    "mid": mid, "title": "叶子", "statement": "FILE:leaf.txt:L1",
                    "parent": json.loads(created.content[0].text)["root"],
                    "check_cmd": CHK, "artifacts": ["leaf.txt"]})
                assert json.loads(added.content[0].text)["ok"] is True
                status = await sess.call_tool("dag3_status", {"mid": mid})
                st = json.loads(status.content[0].text)
                assert st["ok"] and len(st["nodes"]) == 2
                board = await sess.call_tool("dag3_board", {"mid": mid})
                bp = json.loads(board.content[0].text)["overview"]
                assert Path(bp).is_file()
                return mid, bp

    mid, bp = anyio.run(_drive)
    text = Path(bp).read_text(encoding="utf-8")
    assert "MCP交接" in text
