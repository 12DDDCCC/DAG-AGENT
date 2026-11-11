# -*- coding: utf-8 -*-
"""v3.2.1 公开使用链回归：MCP 恢复确认、安装数据目录与投影显示。"""
from __future__ import annotations

import inspect
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import dag_agent.backends as backends
import mcp_server_v3 as mcp_v3
from dag_agent.daemon import RunManager
from dag_agent.graph import Graph
from dag_agent.projection import render_mission
from dag_agent.repository import Repository
from tests.v3.test_v57_provider_pause import FakeNodeBackend, build_task, quota_result

ROOT = Path(__file__).resolve().parents[2]


def _params(home: Path) -> dict:
    conn = sqlite3.connect(home / "mission.db")
    try:
        row = conn.execute("SELECT params_json FROM run_control").fetchone()
        return json.loads(row[0]) if row and row[0] else {}
    finally:
        conn.close()


def _paused_task(home: Path) -> tuple[str, str]:
    mid, root, _ = build_task(home)
    backend = FakeNodeBackend({
        "节点0": quota_result("quota_exhausted", "offline quota exhausted"),
    })
    mgr = RunManager(home)
    result = mgr.run_blocking(
        mid, timeout=10, root=root, backend_factory=lambda: backend,
        real=False, project=False, max_calls=10, max_concurrency=1,
        timeout_s=5,
    )
    assert "供应商暂停" in str(result.get("halt_reason", ""))
    mgr.cleanup_finished(keep=0)
    assert _params(home).get("provider_pause")
    return mid, root


def test_mcp_provider_pause_ack_start_and_blocking_passthrough(tmp_path, monkeypatch):
    """公开 MCP 必须能表达内核已有的暂停确认，且失败预检不得清除暂停。"""
    assert "provider_pause_ack" in inspect.signature(mcp_v3.dag3_run_start).parameters
    assert "provider_pause_ack" in inspect.signature(mcp_v3.dag3_run).parameters

    home = tmp_path / "mcp-provider-pause"
    home.mkdir()
    mid, _root = _paused_task(home)
    monkeypatch.setattr(mcp_v3, "_HOME", home)
    monkeypatch.setattr(RunManager, "_instance", None)

    rejected = mcp_v3.dag3_run_start(
        mid, real=False, project=False, max_calls=0, timeout=5,
    )
    assert rejected.get("ok") is False
    assert "provider_pause_ack" in rejected.get("error", "")
    assert _params(home).get("provider_pause")

    class Unavailable:
        def available(self):
            return False, "offline unavailable"

    monkeypatch.setattr(backends, "create_backend", lambda *a, **kw: Unavailable())
    failed_preflight = mcp_v3.dag3_run_start(
        mid, real=True, backend_name="pi-cli", provider_pause_ack=True,
        project=False, max_calls=0, timeout=5,
    )
    assert failed_preflight.get("ok") is False
    assert _params(home).get("provider_pause")

    class Available:
        name = "stub"
        model = "offline"
        provider = "offline"
        cli = ""

        def available(self):
            return True, "ok"

    monkeypatch.setattr(backends, "create_backend", lambda *a, **kw: Available())
    monkeypatch.setattr(RunManager, "_instance", None)
    accepted = mcp_v3.dag3_run_start(
        mid, real=True, backend_name="pi-cli", provider_pause_ack=True,
        project=False, max_calls=0, timeout=5,
    )
    assert accepted.get("ok") is True, accepted
    assert not _params(home).get("provider_pause")
    RunManager.get(home).stop(mid, mode="kill")
    RunManager.get(home).cleanup_finished(keep=0)
    monkeypatch.setattr(RunManager, "_instance", None)

    captured = {}

    class BlockingManager:
        def run_blocking(self, mission_id, **kwargs):
            captured["mid"] = mission_id
            captured.update(kwargs)
            return {"ok": True, "status": "done"}

    monkeypatch.setattr(RunManager, "get", classmethod(lambda cls, home: BlockingManager()))
    blocked = mcp_v3.dag3_run(
        mid, real=False, provider_pause_ack=True, timeout=1,
    )
    assert blocked["ok"] is True
    assert captured["mid"] == mid
    assert captured["provider_pause_ack"] is True


def test_installed_default_data_paths_leave_package_tree(tmp_path):
    """未传目录时，v1/v3 使用用户数据目录，full50 使用调用者 cwd。"""
    env = dict(os.environ)
    env.update({
        "HOME": str(tmp_path / "home"),
        "USERPROFILE": str(tmp_path / "home"),
        "PYTHONPATH": str(ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    env.pop("DAG_AGENT_HOME", None)
    env.pop("DAG_AGENT_V3_HOME", None)
    code = (
        "import json; "
        "import mcp_server_v3 as m; "
        "from dag_agent import store; "
        "from dag_agent.daemon import RunManager; "
        "print(json.dumps({'v3': str(m._HOME), 'v1': str(store.default_home()), "
        "'daemon': str(RunManager.get().home)}))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, env=env,
        capture_output=True, text=True, check=True,
    )
    paths = json.loads(proc.stdout)
    home = tmp_path / "home"
    assert Path(paths["v3"]) == home / ".dag-agent" / "v3" / "missions"
    assert Path(paths["v1"]) == home / ".dag-agent" / "v1" / "missions"
    assert Path(paths["daemon"]) == home / ".dag-agent" / "v3" / "missions"


def test_full50_default_output_uses_current_working_directory(tmp_path, monkeypatch, capsys):
    from techstack_full_v2 import run_full as rf

    captured = {}

    def fake_mkdtemp(prefix, dir):
        captured["base"] = Path(dir)
        out = tmp_path / "zero-model-result"
        out.mkdir()
        return str(out)

    def fake_run(output, mode, timeout, backend_name, options):
        captured["output"] = Path(output)
        return {"success": True, "error": None, "summary_error": ""}

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rf.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(rf, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["dag-agent-full50", "--mode", "zero-model"])
    assert rf.main() == 0
    assert captured["base"] == tmp_path / "dag-agent-runs" / "full50"
    assert captured["output"] == tmp_path / "zero-model-result"
    assert json.loads(capsys.readouterr().out)["success"] is True


def test_projection_displays_structured_cost_and_pi_identity(tmp_path):
    repo = Repository(tmp_path / "mission.db")
    graph = Graph(repo)
    try:
        mid, root = graph.create_mission("公开投影", root_check="python -c pass")
        cases = {
            "reported": {
                "duration_ms": 10,
                "session_id": "reported-session-123456",
                "cost": {"amount": 1.25, "basis": "reported", "currency": "USD"},
            },
            "estimated": {
                "duration_ms": 20,
                "cost": {"amount": 0.5, "basis": "estimated", "currency": "USD"},
            },
            "unknown": {
                "duration_ms": 30,
                "cost": {"amount": None, "basis": "unknown", "currency": "USD"},
            },
            "pi": {
                "duration_ms": 40,
                "pi_session_id": "pi-session-abcdefghijk",
                "agent_id": "pi-session-abcdefghijk",
                "cost": {"amount": 0, "basis": "estimated", "currency": "USD"},
            },
            "legacy": {
                "duration_ms": 50,
                "session_id": "legacy-session-123456",
                "cost_usd": 0.25,
            },
        }
        nodes = {}
        for label, resources in cases.items():
            nid = graph.add_node(
                mid, label, "projection fixture", parent_id=root,
                check_cmd="python -c pass",
            )
            aid = graph.start_attempt(mid, nid, backend="stub")
            repo._conn.execute(
                "UPDATE attempts SET resources_json=? WHERE id=?",
                (json.dumps(resources), aid),
            )
            repo._conn.commit()
            nodes[label] = nid

        overview = render_mission(repo, graph, mid)
        profiles = {
            label: (overview.parent / "节点" / f"{nid}.md").read_text(encoding="utf-8")
            for label, nid in nodes.items()
        }
        assert "cost=$1.25 (reported)" in profiles["reported"]
        assert "cost=$0.5 (estimated)" in profiles["estimated"]
        assert "cost=unknown" in profiles["unknown"]
        assert "$None" not in profiles["unknown"]
        assert "session=pi-session-a" in profiles["pi"]
        assert "cost=$0.25 (legacy)" in profiles["legacy"]
        assert "session=legacy-sess" in profiles["legacy"]
    finally:
        repo.close()
