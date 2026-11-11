"""test_mcp.py — MCP冒烟：官方SDK ClientSession直连stdio服务器，模拟真实宿主。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT = Path(__file__).resolve().parent.parent
SERVER = PROJECT / "mcp_server.py"


def _text(result) -> dict:
    assert getattr(result, "isError", False) is False, result
    return json.loads(result.content[0].text)


@pytest.fixture()
def mcp_env(tmp_path):
    return dict(os.environ,
                DAG_AGENT_HOME=str(tmp_path / "missions"),
                PYTHONPATH=str(PROJECT), PYTHONIOENCODING="utf-8")


def test_mcp_full_loop(mcp_env):
    async def run() -> dict:
        params = StdioServerParameters(
            command=sys.executable, args=[str(SERVER)], env=mcp_env,
            cwd=str(PROJECT))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                assert {"dag_create_mission", "dag_decompose", "dag_frontier",
                        "dag_submit", "dag_status", "dag_board",
                        "dag_block"} <= names

                # 写一个真实检查脚本到任务目录
                mid_dir = Path(mcp_env["DAG_AGENT_HOME"]) / "mc1"
                (mid_dir / "workspace").mkdir(parents=True, exist_ok=True)
                (mid_dir / "check_ok.py").write_text(
                    "print('mcp check pass')", encoding="utf-8")

                r = _text(await session.call_tool("dag_create_mission", {
                    "mid": "mc1", "name": "MCP任务", "brief": "冒烟",
                    "root_title": "交付", "root_statement": "完成"}))
                assert r["ok"] and r["result"]["total"] == 1

                r = _text(await session.call_tool("dag_decompose", {
                    "mid": "mc1", "parent_id": "n0", "children": [
                        {"title": "子任务", "statement": "做点事",
                         "checks": [{"name": "ok_check",
                                     "command": ["python", "check_ok.py"]}]}]}))
                assert r["ok"] and r["result"]["children"] == ["n1"]

                fr = _text(await session.call_tool("dag_frontier",
                                                   {"mid": "mc1"}))
                assert fr["result"][0]["id"] == "n1"

                r = _text(await session.call_tool("dag_submit", {
                    "mid": "mc1", "node_id": "n1", "summary": "完成"}))
                assert r["ok"] and r["result"]["verdict"] == "PASS"
                assert r["result"]["status"]["mission_done"]

                board = _text(await session.call_tool("dag_board", {"mid": "mc1"}))
                assert "🎉 已完成" in board["result"]["content"]

                st = _text(await session.call_tool("dag_status", {"mid": "mc1"}))
                assert st["result"]["root_done"]
                return {"tools": len(names), "board": board["result"]["board"]}

    out = asyncio.run(run())
    assert out["tools"] >= 10


def test_mcp_rejection_shape(mcp_env):
    async def run() -> None:
        params = StdioServerParameters(
            command=sys.executable, args=[str(SERVER)], env=mcp_env,
            cwd=str(PROJECT))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                r = _text(await session.call_tool("dag_status", {"mid": "ghost"}))
                assert r["ok"] is False and r["rejected"] is True
                assert "不存在" in r["reason"]
    asyncio.run(run())
