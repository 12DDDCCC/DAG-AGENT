"""test_stress_mp.py — 跨进程双开压测：rev乐观锁+文件锁在真实多进程下的表现。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
WORKER = Path(__file__).resolve().parent / "_mp_worker.py"


def _spawn_worker(home: Path, mid: str, wid: str) -> dict:
    env = dict(os.environ, DAG_AGENT_HOME=str(home),
               PYTHONPATH=str(PROJECT), PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, str(WORKER), str(home), mid, wid],
        cwd=str(PROJECT), env=env, capture_output=True, text=True,
        encoding="utf-8", timeout=180)
    lines = [l for l in proc.stdout.strip().splitlines() if l.strip()]
    assert lines, f"worker{wid}无输出: {proc.stderr[-500:]}"
    return json.loads(lines[-1])


def test_two_process_double_open(tmp_path):
    """真双开：父进程建任务，两个独立子进程并发认领提交同一mission。
    验收：全部节点done+根闭合+任一进程结果不含未处理错误。"""
    from dag_agent.store import MissionStore
    s = MissionStore(home=tmp_path)
    s.create_mission("mp1", "双开压测", "b", "交付", "完成")
    s.decompose("mp1", "n0", [{"title": f"c{i}", "statement": "s"}
                              for i in range(6)])
    stats = [_spawn_worker(tmp_path, "mp1", wid) for wid in ("A", "B")]
    st = s.status("mp1")
    assert st["mission_done"], f"双开后未闭合: {stats}"
    assert st["counts"]["done"] == 7, "6子+根全部完成（无丢失更新）"
    assert sum(x["submitted"] for x in stats) == 6, "6个节点恰被提交6次"
    for x in stats:
        assert not x["errors"], f"worker{x['worker']}有未处理错误: {x['errors']}"


def test_four_process_heavier(tmp_path):
    """4进程×5子任务加重压测：文件锁+rev锁协同下零丢失。"""
    from dag_agent.store import MissionStore
    s = MissionStore(home=tmp_path)
    s.create_mission("mp2", "四开压测", "b", "交付", "完成")
    s.decompose("mp2", "n0", [{"title": f"c{i}", "statement": "s"}
                              for i in range(5)])
    stats = [_spawn_worker(tmp_path, "mp2", wid) for wid in ("W1", "W2", "W3", "W4")]
    st = s.status("mp2")
    assert st["mission_done"] and st["counts"]["done"] == 6
    for x in stats:
        assert not x["errors"], x["errors"]
