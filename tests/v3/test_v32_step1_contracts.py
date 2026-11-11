"""v3.2 第1步回归：四个隔离反例翻转为「错误被拒绝」+ 其余四类源码缺口覆盖。

反例来源：work/2026-10-03-design-review/probe_platform_contract.py（Codex 复审）。
通过口径（任务书）：合法多文件输入完整送达；非法/旧提交被拒；
ERROR 不耗产物重试额度；逐条聚合不掩盖；路径组件包含；心跳/收割；
重复提交幂等；检查=发布（候选冻结）。全部零模型调用。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from dag_agent.artifacts import _safe_rel, _within
from dag_agent.graph import Graph, GraphError
from dag_agent.recovery import RetryPolicy, cancel, reap_orphans
from dag_agent.repository import Repository
from dag_agent.scheduler import Scheduler

CHK = "python -c 'pass'"


class PassChecker:
    def __call__(self, cmd, cwd):
        return True, "fixture pass"


@pytest.fixture
def env(tmp_path):
    repo = Repository(tmp_path / "mission.db")
    g = Graph(repo, checker=PassChecker())
    yield g, repo, tmp_path
    repo.close()


# ---- 反例1+2：deps 叶子与多文件整合收到完整绑定输入 ----

def _produce_two_files(g, mid, parent):
    n = g.add_node(mid, "producer", "两件交付", parent_id=parent, check_cmd=CHK,
                   declared_artifacts=("data.csv", "schema.json"))
    aid = g.start_attempt(mid, n)
    wd = Path(g.repo.get_attempt(aid).workdir)
    (wd / "data.csv").write_text("id\n1\n", encoding="utf-8")
    (wd / "schema.json").write_text('{"id":"integer"}', encoding="utf-8")
    r = g.submit_attempt(mid, n, aid, artifact_paths=["data.csv", "schema.json"])
    assert r["verdict"] == "pass"
    return n


def test_regression_dep_leaf_receives_full_bound_inputs(env):
    """反例1翻转：deps 叶子起跑可见输入文件[]（旧缺陷）→ 现在两件全量送达。"""
    g, repo, tmp = env
    mid, root = g.create_mission("反例1", root_check=CHK)
    producer = _produce_two_files(g, mid, root)
    consumer = g.add_node(mid, "consumer", "消费产物", parent_id=root,
                          deps=(producer,), check_cmd=CHK,
                          declared_artifacts=("receipt.txt",))
    seen = {}

    class CaptureBackend:
        name = "capture"
        def run(self, statement, workdir, check_cmd, title=""):
            seen["files"] = sorted(
                p.relative_to(workdir).as_posix()
                for p in Path(workdir).rglob("*") if p.is_file())
            (Path(workdir) / "receipt.txt").write_text("ok", encoding="utf-8")
            return {"ok": True, "summary": "DONE", "session_id": "",
                    "resources": {"cost_usd": 0.0}}

    s = Scheduler(repo, g, CaptureBackend(), reap_enabled=False)
    aid = g.start_attempt(mid, consumer)
    s._work(mid, consumer, aid, integration=False)
    assert seen["files"] == [f"inputs/{producer}/data.csv",
                             f"inputs/{producer}/schema.json"], seen
    # 绑定记录在案且指向同一快照（协议02）
    att = repo.get_attempt(aid)
    bindings = json.loads(att.input_binding_json)
    assert bindings == [{"source": producer, "snapshot_id": bindings[0]["snapshot_id"],
                         "manifest_hash": bindings[0]["manifest_hash"],
                         "files": ["data.csv", "schema.json"],
                         "artifact_version": 1, "role": "dep"}]
    snap = repo.snapshot_by_id(bindings[0]["snapshot_id"])
    assert snap and snap.valid == 1 and snap.artifact_version == 1


def test_regression_multi_file_integration_gets_all_files(env):
    """反例2翻转：父整合只收到末件（旧缺陷）→ 现在两件全量播种。"""
    g, repo, tmp = env
    mid, root = g.create_mission("反例2", root_check=CHK)
    producer = _produce_two_files(g, mid, root)
    seen = {}

    class CaptureBackend:
        name = "capture"
        def run(self, statement, workdir, check_cmd, title=""):
            seen["files"] = sorted(
                p.relative_to(Path(workdir) / "inputs").as_posix()
                for p in (Path(workdir) / "inputs").rglob("*") if p.is_file())
            (Path(workdir) / "final.txt").write_text("整合", encoding="utf-8")
            return {"ok": True, "summary": "DONE", "session_id": "",
                    "resources": {"cost_usd": 0.0}}

    s = Scheduler(repo, g, CaptureBackend(), reap_enabled=False)
    iaid = g.start_attempt(mid, root)  # 整合尝试
    s._work(mid, root, iaid, integration=True)
    assert seen["files"] == [f"{producer}/data.csv", f"{producer}/schema.json"], seen
    r = g.repo.get_attempt(iaid)
    assert r.status == "accepted"  # 整合交付真实过验收


# ---- 反例3+4：验收中取消/改目标被旧提交覆写 → 现在拒绝 ----

def test_regression_cancel_during_check_rejected(tmp_path):
    """反例3翻转：验收中取消 → 旧提交不得覆写为 accepted/mission done。"""
    repo = Repository(tmp_path / "cancel.db")
    g = Graph(repo, checker=PassChecker())
    original = g.checker

    def racing(cmd, cwd):
        cancel(repo, g, *racing.args)  # 检查期间取消
        return original(cmd, cwd)
    mid, root = g.create_mission("反例3", root_check=CHK)
    racing.args = (mid, root, "cancel during acceptance")
    g.checker = racing
    aid = g.start_attempt(mid, root)
    r = g.submit_attempt(mid, root, aid)
    assert r["verdict"] == "rejected", r
    assert repo.get_attempt(aid).status == "cancelled"  # 保持取消真相
    assert repo.get_node(root).status == "cancelled"
    assert repo.get_mission(mid).status == "active"  # 未被旧提交闭合
    assert "submit_rejected" in [e.type for e in repo.events_of(mid)]
    repo.close()


def test_regression_goal_change_during_check_rejected(tmp_path):
    """反例4翻转：验收中改目标 → 拒收旧证据，mission 不闭合。"""
    repo = Repository(tmp_path / "goal.db")
    g = Graph(repo, checker=PassChecker())
    mid, root = g.create_mission("反例4", root_check=CHK)
    aid = g.start_attempt(mid, root)
    original = g.checker
    fired = {"done": False}  # 只触发一次（P2 与根重放会各调一次检查器）

    def racing(cmd, cwd):
        if not fired["done"]:
            fired["done"] = True
            g.change_goal(mid, "check 期间改目标")
        return original(cmd, cwd)
    g.checker = racing
    r = g.submit_attempt(mid, root, aid)
    assert r["verdict"] == "rejected", r
    assert repo.get_mission(mid).status == "active"
    assert repo.get_mission(mid).goal_version == 2
    assert repo.get_attempt(aid).status == "rejected"
    # 目标已变——新会话须在新目标下重新认领（指纹不匹配旧提交一律拒收）
    repo.close()


def test_duplicate_submit_idempotent(env):
    g, repo, tmp = env
    mid, root = g.create_mission("幂等", root_check=CHK)
    a = g.add_node(mid, "A", parent_id=root, check_cmd=CHK,
                   declared_artifacts=("out.txt",))
    aid = g.start_attempt(mid, a)
    (Path(repo.get_attempt(aid).workdir) / "out.txt").write_text("v1")
    r1 = g.submit_attempt(mid, a, aid, artifact_paths=["out.txt"])
    assert r1["verdict"] == "pass"
    r2 = g.submit_attempt(mid, a, aid, artifact_paths=["out.txt"])
    assert r2.get("replayed") is True and r2["verdict"] == "pass"
    assert len(repo.artifacts_of(a)) == 1  # 无重复发布
    assert len(repo.assessments_of(a)) == 1  # 无重复证据


# ---- 缺口3：ERROR 不耗产物重试额度（分类记账） ----

def test_error_attempts_do_not_consume_fail_budget(tmp_path):
    repo = Repository(tmp_path / "budget.db")
    g = Graph(repo, checker3=lambda cmd, cwd: ("error", "考卷故障"))
    mid, root = g.create_mission("error记账", root_check=CHK)
    a = g.add_node(mid, "A", parent_id=root, check_cmd=CHK)
    pol = RetryPolicy(max_retries=2)
    for _ in range(3):  # 三次全 error（blocked 需解锁后才能再试）
        aid = g.start_attempt(mid, a)
        g.submit_attempt(mid, a, aid)
        assert repo.get_attempt(aid).status == "error"
        assert repo.get_attempt(aid).category == "error"
        g.unblock(mid, a)
    assert pol.allows(repo, a) is True  # error 不耗额度
    g2 = Graph(repo, checker3=lambda cmd, cwd: ("fail", "产物不合格"))
    for i in range(3):  # 三次产物失败=首试+两次重试耗尽
        aid = g2.start_attempt(mid, a)
        g2.submit_attempt(mid, a, aid)
        assert repo.get_attempt(aid).status == "failed"
        assert repo.get_attempt(aid).category == "fail"
    assert pol.allows(repo, a) is False  # 第4次失败尝试被拒
    repo.close()


# ---- 缺口4：逐条聚合——前条 FAIL 不掩盖后条 ERROR ----

def test_aggregation_fail_then_error_is_error(tmp_path):
    from dag_agent.acceptance import compile_all, run_acceptance
    items = [
        {"action": "cmd", "cmd": 'python -c "import sys; sys.exit(1)"'},   # fail
        {"action": "cmd", "cmd": 'python -c "import sys; sys.exit(2)"'},   # error
    ]
    v, ev = run_acceptance(compile_all(items), tmp_path / "agg")
    assert v == "error", (v, ev)  # ERROR 优先，不被 && 短路掩盖


def test_submit_uses_itemized_aggregation(env):
    """正式提交走逐条三分聚合（acceptance_json），证据保留逐条结果。"""
    g, repo, tmp = env
    mid, root = g.create_mission("逐条", root_check=CHK)
    a = g.add_node(
        mid, "A", parent_id=root,
        acceptance=[{"action": "cmd",
                     "cmd": 'python -c "import pathlib,sys;'
                            ' sys.exit(0 if any(pathlib.Path(\'.\').glob(\'x.txt\'))'
                            ' else 1)"'}])
    aid = g.start_attempt(mid, a)
    r = g.submit_attempt(mid, a, aid)  # 空目录 → fail（有牙齿）
    assert r["verdict"] == "fail"
    m = repo.assessments_of(a)[-1]
    assert m.method == "items" and m.check_digest  # 考卷包摘要入证据


# ---- 缺口5：路径组件包含 ----

def test_path_component_containment(tmp_path):
    home = tmp_path
    (home / "n1").mkdir()
    (home / "n1x").mkdir()
    (home / "n1x" / "sneak.txt").write_text("x")
    (home / "n1" / "ok.txt").write_text("ok")
    assert _within(home / "n1" / "ok.txt", home / "n1")
    assert not _within(home / "n1x" / "sneak.txt", home / "n1")  # 同前缀兄弟拒绝
    with pytest.raises(ValueError):
        _safe_rel("../sib.txt")
    with pytest.raises(ValueError):
        _safe_rel("C:/abs.txt" if str(tmp_path.drive) else "/abs.txt")


def test_publish_escape_rejected_at_store(tmp_path):
    from dag_agent.artifacts import ArtifactStore
    repo = Repository(tmp_path / "m.db")
    store = ArtifactStore(tmp_path)
    wd = tmp_path / "wd"
    (wd / "sub").mkdir(parents=True)
    (wd / "sub" / "..data.txt").write_text("x")  # 文件名含..但非路径段
    with pytest.raises(ValueError):
        store.publish_prepared(repo, "m1", "n1", "a1", wd,
                               ["../escape.txt"], 1)
    with pytest.raises(ValueError):
        store.publish_prepared(repo, "m1", "n1", "a1", wd,
                               ["sub/../../escape2.txt"], 1)
    repo.close()


# ---- 缺口6：心跳与收割竞态 ----

def test_reap_protects_fresh_and_inflight_and_reaps_stale(env):
    """收割竞态（确定性造数，无 sleep）：非保护+心跳过期→收割；
    心跳新鲜→跳过；在途保护集→不收割。"""
    g, repo, tmp = env
    mid, root = g.create_mission("收割", root_check=CHK)
    a = g.add_node(mid, "A", parent_id=root, check_cmd=CHK)
    past = (datetime.now() - timedelta(minutes=10)).isoformat(timespec="seconds")
    fresh_hb = datetime.now().isoformat(timespec="seconds")

    def _mk(aid: str, hb: str):
        with repo.tx() as cur:
            cur.execute(
                "INSERT INTO attempts(id,node_id,mission_id,backend,status,"
                "workdir,heartbeat_at,started_at) VALUES(?,?,?,?, 'running',?,?,?)",
                (aid, a, mid, "stub", str(tmp / aid), hb, past))
    _mk("a-stale-unprot", past)    # 过期且未保护 → 应收割
    _mk("a-stale-prot", past)      # 过期但在保护集 → 跳过
    _mk("a-fresh", fresh_hb)       # 心跳新鲜 → 跳过
    reaped = reap_orphans(repo, g, mid, stale_seconds=300,
                          protected={"a-stale-prot"})
    assert reaped == ["a-stale-unprot"], reaped
    assert repo.get_attempt("a-stale-unprot").status == "orphaned"
    assert repo.get_attempt("a-stale-prot").status == "running"
    assert repo.get_attempt("a-fresh").status == "running"
    assert "orphan_reaped" in [e.type for e in repo.events_of(mid)]


# ---- 候选冻结：检查的=发布的 ----

def test_checked_content_is_published_content(tmp_path):
    repo = Repository(tmp_path / "freeze.db")
    seen = {}

    def hash_checker(cmd, cwd):
        seen["hash_at_check"] = (Path(cwd) / "out.txt").read_bytes()
        # 检查期间改"工作目录"（非候选）——发布仍应是检查时内容
        return True, "pass"

    g = Graph(repo, checker=hash_checker)
    mid, root = g.create_mission("冻结", root_check=CHK)
    a = g.add_node(mid, "A", parent_id=root, check_cmd=CHK,
                   declared_artifacts=("out.txt",))
    aid = g.start_attempt(mid, a)
    wd = Path(repo.get_attempt(aid).workdir)
    (wd / "out.txt").write_bytes(b"CHECKED")
    r = g.submit_attempt(mid, a, aid, artifact_paths=["out.txt"])
    assert r["verdict"] == "pass"
    pub = repo.snapshot_by_id(r["snapshot_id"])
    acc = (repo.db_path.parent / "accepted" / a /
           f"v{pub.artifact_version}" / "out.txt")
    assert acc.read_bytes() == seen["hash_at_check"] == b"CHECKED"
    repo.close()


# ---- 预算持久：重启不重置 ----

def test_budget_persists_across_restart(env):
    g, repo, tmp = env
    mid, root = g.create_mission("预算持久", root_check=CHK)
    a = g.add_node(mid, "A", parent_id=root, check_cmd=CHK)

    class BE:
        name = "be"
        def run(self, statement, workdir, check_cmd, title=""):
            return {"ok": True, "summary": "DONE", "session_id": "",
                    "resources": {"cost_usd": 0.5}}

    s1 = Scheduler(repo, g, BE(), budget=None, reap_enabled=False)
    # Codex N1：预算持久写统一走 apply_budget_delta（增量读-改-写，
    # 无内存全量覆写入口）；内存以持久账为准
    from dag_agent.executor_contract import apply_budget_delta
    with repo.tx() as cur:
        apply_budget_delta(cur, mid, calls_delta=3, cost_delta=1.25)
    s2 = Scheduler(repo, g, BE(), reap_enabled=False)
    s2._budget_load(mid)
    assert s2.budget.calls_used == 3 and abs(s2.budget.cost_used - 1.25) < 1e-9
