"""v3.2 第3步回归：AND/OR fallback 完成语义（任务书第3步全部场景）。

- 普通子目标 AND；同父同 group_id = OR 替代组（成员满足同一组级契约）。
- 首选路线（group_order 小）先派；连续产物 FAIL 达 switch_after 切下一支。
- ERROR 默认维修不切支；赢家原子选定，后到提交被拒；败者取消留史。
- 父整合只读普通孩子与各组赢家；下游依赖解析到组赢家快照。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dag_agent.graph import Graph, GraphError
from dag_agent.repository import Repository
from dag_agent.scheduler import Scheduler

CHK = "python -c 'pass'"


class ScriptedChecker:
    """按节点标题脚本化判决：pass/fail/error 可切换。"""

    def __init__(self):
        self.script: dict[str, str] = {}

    def __call__(self, cmd, cwd):
        for title, verdict in self.script.items():
            if title in str(cmd) + str(cwd):
                return (verdict == "pass", f"scripted:{verdict}")
        return True, "default pass"


class PassChecker:
    def __call__(self, cmd, cwd):
        return True, "fixture pass"


@pytest.fixture
def env(tmp_path):
    repo = Repository(tmp_path / "mission.db")
    g = Graph(repo, checker=PassChecker())
    yield g, repo, tmp_path
    repo.close()


def _run(g, mid, nid, files=None):
    aid = g.start_attempt(mid, nid)
    if files:
        wd = Path(g.repo.get_attempt(aid).workdir)
        for name, content in files.items():
            (wd / name).write_text(content, encoding="utf-8")
    return aid, g.submit_attempt(mid, nid, aid, artifact_paths=list(files or ()))


def _mk_group(g, mid, root, names=("方案A", "方案B"), switch_after=1):
    ids = {}
    for i, name in enumerate(names):
        ids[name] = g.add_node(
            mid, name, f"{name} 实现", parent_id=root, check_cmd=CHK,
            declared_artifacts=("impl.txt",), group_id="route1",
            group_order=i, switch_after=switch_after)
    return ids


def test_a_fails_b_wins_unblocks_integration(env):
    """A 失败（达阈值）→ B 激活 → B 过 → A 取消留史、组有赢家、父整合解锁。"""
    g, repo, tmp = env
    mid, root = g.create_mission("A败B成", root_check=CHK)
    ids = _mk_group(g, mid, root)
    a, b = ids["方案A"], ids["方案B"]
    g.checker = lambda cmd, cwd: (False, "产物不合格")
    _aid1, r1 = _run(g, mid, a, {"impl.txt": "A的尝试"})
    assert r1["verdict"] == "fail"
    # B 现在可认领（A 已达 switch_after=1）；A 未耗尽时 B 不可认领已由前置验证
    g.checker = PassChecker()
    _aid2, r2 = _run(g, mid, b, {"impl.txt": "B的实现"})
    assert r2["verdict"] == "pass"
    assert repo.get_node(a).status == "cancelled"      # 败者取消（留工件与证据）
    assert repo.get_node(b).status == "accepted"
    assert repo.assessments_of(a)                       # 败者证据保留
    types = [e.type for e in repo.events_of(mid)]
    assert "group_won" in types
    # 父整合解锁并只绑定赢家 B
    iaid = g.start_attempt(mid, root)
    bind = json.loads(repo.get_attempt(iaid).input_binding_json)
    assert [b_["source"] for b_ in bind] == [b]
    (Path(repo.get_attempt(iaid).workdir) / "final.txt").write_text("F",
                                                                    encoding="utf-8")
    rf = g.submit_attempt(mid, root, iaid, artifact_paths=["final.txt"])
    assert rf["verdict"] == "pass"
    assert repo.get_mission(mid).status == "done"


def test_b_not_dispatchable_before_threshold(env):
    """A 未达切换阈值时 B 不可认领；A 第二次成功则无需切 B。"""
    g, repo, tmp = env
    mid, root = g.create_mission("激活门", root_check=CHK)
    ids = _mk_group(g, mid, root, switch_after=2)
    a, b = ids["方案A"], ids["方案B"]
    g.checker = lambda cmd, cwd: (False, "产物不合格")
    _run(g, mid, a, {"impl.txt": "第1败"})   # A 只败 1 次 < switch_after=2
    with pytest.raises(GraphError, match="激活规则"):
        g.start_attempt(mid, b)               # B 不激活
    g.checker = PassChecker()
    _aid2, r2 = _run(g, mid, a, {"impl.txt": "A第二次成功"})
    assert r2["verdict"] == "pass"            # 组内赢家=A，B 未派即取消
    assert repo.get_node(b).status == "cancelled"
    assert repo.get_node(a).status == "accepted"


def test_error_does_not_switch_route(env):
    """成员 ERROR（考卷故障）→ 维修通道，不切支不耗路线阈值。"""
    g, repo, tmp = env
    mid, root = g.create_mission("ERROR维修", root_check=CHK)
    ids = _mk_group(g, mid, root)
    a, b = ids["方案A"], ids["方案B"]
    g.checker3 = lambda cmd, cwd: ("error", "考卷故障")
    aid, r = _run(g, mid, a, {"impl.txt": "x"})
    assert r["verdict"] == "error" and repo.get_node(a).status == "blocked"
    with pytest.raises(GraphError, match="激活规则|不可开始"):
        g.start_attempt(mid, b)  # error 不算路线失败——B 不激活
    g.checker3 = None
    g.checker = PassChecker()
    g.unblock(mid, a)
    _aid2, r2 = _run(g, mid, a, {"impl.txt": "修好后交付"})
    assert r2["verdict"] == "pass" and repo.get_node(b).status == "cancelled"


def test_race_winner_atomic_late_submit_rejected(env):
    """竞速+取消后迟到提交：A 第二次尝试在途期间 B 赢家落定 →
    A 在途被取消（lost_race）→ A 迟到提交被拒。"""
    g, repo, tmp = env
    mid, root = g.create_mission("竞速", root_check=CHK)
    ids = _mk_group(g, mid, root)
    a, b = ids["方案A"], ids["方案B"]
    g.checker = lambda cmd, cwd: (False, "产物不合格")
    _run(g, mid, a, {"impl.txt": "A第1败"})   # A 达阈值，B 可激活
    g.checker = PassChecker()
    aid_a2 = g.start_attempt(mid, a)           # A 重试仍在途（组规则允许自身重试）
    aid_b = g.start_attempt(mid, b)            # B 并行在途——真实竞速
    (Path(repo.get_attempt(aid_a2).workdir) / "impl.txt").write_text("A迟到")
    (Path(repo.get_attempt(aid_b).workdir) / "impl.txt").write_text("B")
    r_b = g.submit_attempt(mid, b, aid_b, artifact_paths=["impl.txt"])
    assert r_b["verdict"] == "pass"            # B 先到——赢家原子落定
    assert repo.get_attempt(aid_a2).status == "cancelled"   # A 在途被取消
    assert repo.get_node(a).status == "cancelled"
    with pytest.raises(GraphError, match="不可提交"):        # 迟到提交被拒
        g.submit_attempt(mid, a, aid_a2, artifact_paths=["impl.txt"])
    assert repo.get_node(b).status == "accepted"


def test_downstream_dep_binds_group_winner_snapshot(env):
    """下游依赖组成员 → 绑定解析到组赢家快照（稳定输出=赢家输出）。"""
    g, repo, tmp = env
    mid, root = g.create_mission("下游绑定", root_check=CHK)
    ids = _mk_group(g, mid, root)
    a, b = ids["方案A"], ids["方案B"]
    g.checker = lambda cmd, cwd: (False, "A 永远差一点")
    _run(g, mid, a, {"impl.txt": "A"})
    g.checker = PassChecker()
    _run(g, mid, b, {"impl.txt": "B"})
    down = g.add_node(mid, "下游", "用组输出", parent_id=root, deps=(a,),
                      check_cmd=CHK, declared_artifacts=("use.txt",))
    aid = g.start_attempt(mid, down)
    bind = json.loads(repo.get_attempt(aid).input_binding_json)
    assert bind[0]["source"] == b          # 依赖写在 A 上，解析到赢家 B
    assert bind[0]["files"] == ["impl.txt"]
    wd = Path(repo.get_attempt(aid).workdir)
    s = Scheduler(repo, g, type("BE", (), {
        "name": "be", "run": staticmethod(
            lambda statement, w, c, title="": (
                (Path(w) / "use.txt").write_text("用B", encoding="utf-8"),
                {"ok": True, "summary": "DONE", "session_id": "",
                 "resources": {"cost_usd": 0.0}})[-1])})(), reap_enabled=False)
    s._work(mid, down, aid, integration=False)
    assert (wd / "inputs" / b / "impl.txt").read_text(encoding="utf-8") == "B"
    assert repo.get_attempt(aid).status == "accepted"


def test_scheduler_switches_route_on_threshold(env):
    """调度器端到端：A 耗尽阈值后自动切 B，全链闭合（零模型）。"""
    g, repo, tmp = env
    fail = {"on": True}

    class BE:
        name = "stub"  # A3 闸门豁免：测试桩
        def run(self, statement, workdir, check_cmd, title=""):
            (Path(workdir) / "impl.txt").write_text(
                "A" if title == "方案A" else "B", encoding="utf-8")
            if title == "方案A" and fail["on"]:
                return {"ok": False, "summary": "fail", "session_id": "",
                        "resources": {"cost_usd": 0.01}}
            return {"ok": True, "summary": "DONE", "session_id": "",
                    "resources": {"cost_usd": 0.01}}

    # A 的考卷判 fail（产物不合格）模拟：用真检查器+BITES
    BITES = ("python -c \"import pathlib,sys; sys.exit(0 if any("
             "pathlib.Path('.').glob('impl.txt')) else 1)\"")
    g2 = Graph(repo)  # 真检查器
    mid, root = g2.create_mission("调度切支", root_check=BITES,
                                   root_declared_artifacts=("impl.txt",))
    a = g2.add_node(mid, "方案A", "A", parent_id=root, check_cmd="python -c 'pass'",
                    declared_artifacts=("impl.txt",), group_id="r", group_order=0)
    b = g2.add_node(mid, "方案B", "B", parent_id=root, check_cmd=BITES,
                    declared_artifacts=("impl.txt",), group_id="r", group_order=1)
    # 方案A 的考卷故意永红（等价产物失败）
    g2.repo.update_node(a, check_cmd="python -c 'import sys; sys.exit(1)'",
                        check_version=2)
    s = Scheduler(repo, g2, BE(), max_concurrency=1, reap_enabled=False)
    s.retry_policy.max_retries = 1  # A 败1次即耗尽 switch_after=1
    summary = s.run_until_done(mid, root, timeout=30, poll=0.05)
    assert summary["mission_status"] == "done", summary
    assert repo.get_node(a).status == "cancelled"
    assert repo.get_node(b).status == "accepted"
