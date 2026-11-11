"""步骤4单元验收：并行与生长——并发上限/重叠证据/动态入队/拆解提案释放执行位/
预算熔断/整合播种/死局检测。全部用 FakeBackend（零真调用，时间可控）。"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from dag_agent.coordinator import Proposal
from dag_agent.graph import Graph
from dag_agent.repository import Repository
from dag_agent.scheduler import Budget, Scheduler

CHK = "python -c 'pass'"


class FakeBackend:
    """脚本化后端：按节点标题给延迟与交付文件；统计峰值并发。"""
    name = "fake"

    def __init__(self, delay: float = 0.3, fail_titles: tuple = ()):
        self.delay = delay
        self.fail_titles = fail_titles
        self._lock = threading.Lock()
        self.concurrent = 0
        self.peak = 0
        self.calls = 0
        self.cost_each = 0.01

    def run(self, statement, workdir, check_cmd, title=""):
        with self._lock:
            self.concurrent += 1
            self.peak = max(self.peak, self.concurrent)
            self.calls += 1
        time.sleep(self.delay)
        with self._lock:
            self.concurrent -= 1
        # 交付约定：陈述中的 `FILE:<名>:<内容>` 令牌 → 写文件（fail 也写了，
        # 后端自报失败 ≠ 交付缺失；真假由验收命令裁决）
        for part in statement.split():
            if part.startswith("FILE:"):
                _, name, content = part.split(":", 2)
                (Path(workdir) / name).write_text(content, encoding="utf-8")
        if title in self.fail_titles:
            return {"ok": False, "summary": "fake fail", "session_id": "f",
                    "resources": {"cost_usd": self.cost_each, "duration_ms": 1}}
        return {"ok": True, "summary": "DONE fake", "session_id": "fake-sess",
                "resources": {"cost_usd": self.cost_each, "duration_ms": 1}}


@pytest.fixture
def env(tmp_path):
    repo = Repository(tmp_path / "mission.db")
    g = Graph(repo)
    be = FakeBackend()
    s = Scheduler(repo, g, be, max_concurrency=2, budget=Budget(max_calls=64))
    yield s, g, repo, be
    repo.close()


# v3.1：考卷须"有牙齿"（空目录必 FAIL 才过体检）——any-txt 存在性断言
BITES = ("python -c \"import pathlib,sys; sys.exit(0 if any("
         "pathlib.Path('.').glob('*.txt')) else 1)\"")


def _add(envx, mid, title, files, parent, check=BITES):
    s, g, repo, be = envx
    declared = tuple(f.split(":")[1] for f in files if f.startswith("FILE:"))
    return g.add_node(mid, title,
                      statement=f"{title}号节点交付 " + " ".join(files),
                      parent_id=parent, check_cmd=check, declared_artifacts=declared)


def test_parallel_overlap_and_completion(env):
    s, g, repo, be = env
    mid, root = g.create_mission("并行", root_check=CHK)
    _add(env, mid, "A", ["FILE:a.txt:A1"], root)
    _add(env, mid, "B", ["FILE:b.txt:B1"], root)
    summary = s.run_until_done(mid, root, timeout=30)
    assert summary["mission_status"] == "done", summary
    assert summary["overlap_pairs"] >= 1, "两分支应有真实并行重叠"
    assert be.peak <= 2


def test_max_concurrency_respected(env):
    s, g, repo, be = env
    mid, root = g.create_mission("并发上限", root_check=CHK)
    for t in "PQRST":
        _add(env, mid, t, [f"FILE:{t}.txt:{t}"], root)
    summary = s.run_until_done(mid, root, timeout=60)
    assert summary["mission_status"] == "done"
    assert 1 <= be.peak <= 2, f"峰值并发应不超过上限2，实际{be.peak}"


def test_dynamic_enqueue_midflight(env):
    s, g, repo, be = env
    mid, root = g.create_mission("动态入队", root_check=CHK)
    _add(env, mid, "A", ["FILE:a.txt:A1"], root)

    def late():
        time.sleep(0.1)
        _add(env, mid, "C", ["FILE:c.txt:C9"], root)

    threading.Thread(target=late).start()
    summary = s.run_until_done(mid, root, timeout=30)
    assert summary["mission_status"] == "done"
    c = [n for n in repo.list_nodes(mid) if n.title == "C"][0]
    assert c.status == "accepted"


def test_proposal_releases_running_attempt(env):
    s, g, repo, be = env
    mid, root = g.create_mission("提案释放", root_check=CHK)
    a_id = _add(env, mid, "A", ["FILE:a.txt:A1"], root)
    aid = g.start_attempt(mid, a_id)  # 在途
    p = Proposal(node_id=a_id, reason="拆两半", integration_check=BITES,
                 children=[{"title": "A1", "statement": "上半", "check_cmd": BITES},
                           {"title": "A2", "statement": "下半", "check_cmd": BITES}])
    kids = s.propose_decomposition(mid, p)
    assert len(kids) == 2
    assert repo.get_attempt(aid).status == "cancelled"  # 释放执行位
    assert repo.get_node(a_id).status == "waiting_children"
    with pytest.raises(Exception, match="cancelled|已转入分解"):
        g.submit_attempt(mid, a_id, aid)  # 旧尝试不得再提交
    assert "attempt_cancelled" in [e.type for e in repo.events_of(mid)]


def test_proposal_validation(env):
    s, g, repo, be = env
    mid, root = g.create_mission("提案校验", root_check=CHK)
    a = _add(env, mid, "A", ["FILE:a.txt:A1"], root)
    with pytest.raises(ValueError, match="缺陈述"):
        s.propose_decomposition(mid, Proposal(
            node_id=a, children=[{"title": "x"}], integration_check=CHK))
    with pytest.raises(ValueError, match="缺验收命令"):
        s.propose_decomposition(mid, Proposal(
            node_id=a, children=[{"title": "x", "statement": "s"}], integration_check=CHK))
    with pytest.raises(ValueError, match="整合验收命令"):
        s.propose_decomposition(mid, Proposal(
            node_id=a, children=[{"title": "x", "statement": "s", "check_cmd": BITES}]))
    with pytest.raises(ValueError, match="超上限"):
        s.propose_decomposition(mid, Proposal(
            node_id=a, integration_check=BITES,
            children=[{"title": f"y{i}", "statement": f"第{i}块",
                       "check_cmd": BITES} for i in range(6)]))


def test_integration_seeded_with_child_artifacts(env):
    s, g, repo, be = env
    seen = {}

    class SpyBackend(FakeBackend):
        def run(self, statement, workdir, check_cmd, title=""):
            if title == "播种任务":
                seen["inputs"] = sorted(
                    p.name for p in Path(workdir, "inputs").rglob("*.txt"))
            return super().run(statement, workdir, check_cmd, title)

    s.backend = SpyBackend()
    mid, root = g.create_mission("播种任务", root_check=CHK)
    g.decompose(mid, root, [{
        "title": "K1", "statement": "FILE:k1.txt:K1DATA",
        "check_cmd": CHK, "declared_artifacts": ["k1.txt"]}])
    summary = s.run_until_done(mid, root, timeout=30)
    assert summary["mission_status"] == "done", summary
    assert seen.get("inputs") == ["k1.txt"], seen


def test_budget_halt(env):
    s, g, repo, be = env
    s.budget = Budget(max_calls=1)
    mid, root = g.create_mission("预算", root_check=CHK)
    _add(env, mid, "A", ["FILE:a.txt:A1"], root)
    _add(env, mid, "B", ["FILE:b.txt:B1"], root)
    summary = s.run_until_done(mid, root, timeout=30)
    assert "熔断" in (summary["halt_reason"] or "")
    assert be.calls == 1


def test_deadlock_detection(env):
    s, g, repo, be = env
    be.fail_titles = ("坏节点",)
    mid, root = g.create_mission("死局任务",
                                 root_check="python -c 'import sys; sys.exit(1)'")
    _add(env, mid, "坏节点", ["FILE:x.txt:X"], root)
    summary = s.run_until_done(mid, root, timeout=30)
    assert "死局" in (summary["halt_reason"] or "")
