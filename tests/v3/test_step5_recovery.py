"""步骤5验收：恢复——暂停/取消/孤儿收割/迟到结果拒收/回执幂等/重启恢复/自动重试。"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from dag_agent.graph import Graph, GraphError
from dag_agent.recovery import (RetryPolicy, cancel, guarded_action, heartbeat,
                                pause, reap_orphans, recover_mission, resume)
from dag_agent.repository import Repository
from dag_agent.scheduler import Budget, Scheduler

from test_step4_scheduler import FakeBackend, _add

CHK = "python -c 'pass'"


@pytest.fixture
def env(tmp_path):
    repo = Repository(tmp_path / "mission.db")
    g = Graph(repo)
    be = FakeBackend(delay=0.05)
    s = Scheduler(repo, g, be, max_concurrency=2, budget=Budget(max_calls=64))
    yield s, g, repo, be
    repo.close()


def test_pause_resume_blocks_scheduling(env):
    s, g, repo, be = env
    mid, root = g.create_mission("暂停", root_check=CHK)
    a = _add(env, mid, "A", ["FILE:a.txt:A1"], root)
    pause(repo, g, mid, a, "人工暂停")
    assert repo.get_node(a).status == "blocked"
    summary = s.run_until_done(mid, root, timeout=3, poll=0.1, max_idle=5)
    assert repo.get_node(a).status == "blocked"  # 暂停中不被调度
    resume(repo, g, mid, a)
    summary = s.run_until_done(mid, root, timeout=30)
    assert summary["mission_status"] == "done"


def test_cancel_running_rejects_late_submit(env):
    s, g, repo, be = env
    mid, root = g.create_mission("取消", root_check=CHK)
    a = _add(env, mid, "A", ["FILE:a.txt:A1"], root)
    aid = g.start_attempt(mid, a)
    cancel(repo, g, mid, a, "需求撤回")
    assert repo.get_attempt(aid).status == "cancelled"
    assert repo.get_node(a).status == "cancelled"
    with pytest.raises(GraphError):
        g.submit_attempt(mid, a, aid, artifact_paths=[])  # 迟到结果不能覆盖


def test_orphan_reap_and_redispatch(env):
    s, g, repo, be = env
    mid, root = g.create_mission("孤儿", root_check=CHK)
    a = _add(env, mid, "A", ["FILE:a.txt:A1"], root)
    aid = g.start_attempt(mid, a)
    heartbeat(repo, aid)
    # 模拟进程假死：心跳停在 10 分钟前
    old = "2020-01-01T00:00:00"
    repo.update_attempt(aid, heartbeat_at=old)
    reaped = reap_orphans(repo, g, mid, stale_seconds=300)
    assert aid in reaped
    assert repo.get_attempt(aid).status == "orphaned"
    assert repo.get_node(a).status == "pending"  # 回到可派发
    with pytest.raises(GraphError):
        g.submit_attempt(mid, a, aid, artifact_paths=[])  # 孤儿尝试提交=迟到结果
    summary = s.run_until_done(mid, root, timeout=30)
    assert summary["mission_status"] == "done"


def test_recover_mission_after_restart(env):
    s, g, repo, be = env
    mid, root = g.create_mission("重启", root_check=CHK)
    a = _add(env, mid, "A", ["FILE:a.txt:A1"], root)
    aid = g.start_attempt(mid, a)  # 服务崩了：running 留在库里
    info = recover_mission(repo, g, mid)
    assert aid in info["reaped_attempts"]
    assert a in info["resumable_nodes"]
    summary = s.run_until_done(mid, root, timeout=30)
    assert summary["mission_status"] == "done"


def test_guarded_action_idempotent(env):
    s, g, repo, be = env
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        return {"did": True}

    r1 = guarded_action(repo, "op-9", boom)
    r2 = guarded_action(repo, "op-9", boom)
    assert calls["n"] == 1  # 不盲目重复
    assert r1 == r2 == {"did": True}


def test_auto_retry_on_transient_failure(env):
    s, g, repo, be = env
    s.retry_policy = RetryPolicy(max_retries=1)
    # 检查命令：文件内容为 ok2 才过（第一次后端写 bad，重试写 ok2）
    mid, root = g.create_mission("自动重试", root_check=CHK)
    import itertools
    counter = itertools.count()

    class FlakyBackend(FakeBackend):
        def run(self, statement, workdir, check_cmd, title=""):
            n = next(counter)
            content = "bad" if n == 0 else "ok2"
            (Path(workdir) / "out.txt").write_text(content, encoding="utf-8")
            return {"ok": n > 0, "summary": f"run{n}", "session_id": "f",
                    "resources": {"cost_usd": 0.0, "duration_ms": 1}}

    s.backend = FlakyBackend(delay=0)
    nid = g.add_node(mid, "F", statement="x", parent_id=root,
                     check_cmd="python -c \"import sys; sys.exit(0) if open('out.txt').read()=='ok2' else sys.exit(1)\"",
                     declared_artifacts=("out.txt",))
    summary = s.run_until_done(mid, root, timeout=30)
    assert summary["mission_status"] == "done", summary
    assert repo.get_node(nid).status == "accepted"


def test_integration_retry_after_compensation_failure(env):
    """整合提交失败→root failed→自动重试接得住（引擎抖动复盘回归）。"""
    s, g, repo, be = env
    s.retry_policy = RetryPolicy(max_retries=1)

    class FlakyIntegration(FakeBackend):
        def run(self, statement, workdir, check_cmd, title=""):
            out = super().run(statement, workdir, check_cmd, title)
            if title.startswith("整合") and not hasattr(self, "_hit"):
                self._hit = True
                return out  # 第一次：不写申报工件（模拟整合者失手）
            (Path(workdir) / "report.txt").write_text("ok", encoding="utf-8")
            return out

    s.backend = FlakyIntegration(delay=0)
    mid, root = g.create_mission("整合重试", root_check=CHK,
                                 root_declared_artifacts=("report.txt",))
    _add(env, mid, "叶子", ["FILE:leaf.txt:L1"], root)
    summary = s.run_until_done(mid, root, timeout=30)
    assert summary["mission_status"] == "done", summary
    assert repo.get_node(root).status == "accepted"


def test_retry_exhausted_deadlock(env):
    s, g, repo, be = env
    s.retry_policy = RetryPolicy(max_retries=0)
    mid, root = g.create_mission("重试耗尽", root_check=CHK)
    a = g.add_node(mid, "坏", statement="FILE:x.txt:X", parent_id=root,
                   check_cmd="python -c 'import sys; sys.exit(1)'",
                   declared_artifacts=("x.txt",))
    summary = s.run_until_done(mid, root, timeout=30)
    assert "死局" in (summary["halt_reason"] or "")
    assert repo.get_node(a).status == "failed"
