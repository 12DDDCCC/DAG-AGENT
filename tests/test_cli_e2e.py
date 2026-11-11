"""test_cli_e2e.py — CLI端到端：真实子进程跑通 create→decompose→frontier→submit→done。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent


def _cli(args: list[str], home: Path) -> dict:
    env = dict(os.environ, DAG_AGENT_HOME=str(home),
               PYTHONPATH=str(PROJECT), PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "dag_agent.cli", *args],
        cwd=str(PROJECT), env=env, capture_output=True, text=True,
        encoding="utf-8", timeout=60)
    assert proc.returncode == 0, f"CLI失败: {proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)


def test_full_mission_lifecycle(tmp_path):
    home = tmp_path / "missions"
    # 1) 创建
    st = _cli(["create", "e2e", "端到端任务", "演示全生命周期", "交付", "全部子任务完成"],
              home)
    assert st["total"] == 1
    # 2) 准备真实检查脚本（放workspace/，命令相对mission目录）
    mid_dir = home / "e2e"
    (mid_dir / "workspace").mkdir(parents=True, exist_ok=True)
    (mid_dir / "workspace" / "report.md").write_text(
        "# 调研报告\n结论：DAG-agent可用", encoding="utf-8")
    _write(mid_dir, "check_report.py",
           "from pathlib import Path\n"
           "t = Path('workspace/report.md').read_text(encoding='utf-8')\n"
           "assert '结论' in t, '报告缺结论'\n"
           "print('report ok:', len(t), 'chars')")
    # 3) 分解：child带真实检查
    r = _cli(["decompose", "e2e", "n0", "--child", json.dumps({
        "title": "写报告", "statement": "workspace/report.md存在且含结论",
        "checks": [{"name": "report_check",
                    "command": ["python", "check_report.py"]}],
        "checks_note": "zero-write"})], home)
    assert r["children"] == ["n1"]
    # 4) 前沿认领并提交（先FAIL一次再PASS，验证修复环）
    fr = _cli(["frontier", "e2e"], home)
    assert fr[0]["id"] == "n1"
    (mid_dir / "workspace" / "report.md").write_text("# 草稿", encoding="utf-8")
    bad = _cli(["submit", "e2e", "n1", "--summary", "先交一版"], home)
    assert bad["verdict"] == "FAIL" and "报告缺结论" in bad["evidence"]
    (mid_dir / "workspace" / "report.md").write_text(
        "# 调研报告\n结论：DAG-agent可用", encoding="utf-8")
    ok = _cli(["submit", "e2e", "n1", "--summary", "报告定稿"], home)
    assert ok["verdict"] == "PASS"
    # 5) 依赖闭合：root自动done
    assert ok["auto_closed"] == ["n0"] and ok["status"]["mission_done"]
    # 6) 看板与事件
    board = _cli(["board", "e2e"], home)
    assert "🎉 已完成" in board["content"]
    ev = _cli(["events", "e2e", "--tail", "10"], home)
    types = [e["type"] for e in ev]
    assert "auto_done" in types and "fail" in types and "done" in types


def _write(mid_dir: Path, name: str, content: str) -> None:
    (mid_dir / name).write_text(content, encoding="utf-8")


def test_cli_rejection_exit_code(tmp_path):
    home = tmp_path / "missions"
    out = _run_raw(["status", "ghost"], home)
    assert out.returncode == 2 and json.loads(out.stdout)["rejected"]


def _run_raw(args: list[str], home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, DAG_AGENT_HOME=str(home),
               PYTHONPATH=str(PROJECT), PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, "-m", "dag_agent.cli", *args],
                          cwd=str(PROJECT), env=env, capture_output=True,
                          text=True, encoding="utf-8", timeout=60)
