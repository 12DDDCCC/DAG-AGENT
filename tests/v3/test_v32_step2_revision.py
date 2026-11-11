"""v3.2 第2步回归：修订/级联失效/重开协议 + 证据引用 + 主观签收版本绑定。

对应任务书第2步：历史 accepted 与原始证据保留、新修订生新证据、失效原因留痕、
done 任务重开、机器/主观分账、subjective 显式签收绑定版本与依据。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dag_agent.graph import Graph, GraphError
from dag_agent.repository import Repository

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


def _run_pass(g, mid, nid, files: dict):
    aid = g.start_attempt(mid, nid)
    wd = Path(g.repo.get_attempt(aid).workdir)
    for name, content in files.items():
        (wd / name).write_text(content, encoding="utf-8")
    r = g.submit_attempt(mid, nid, aid, artifact_paths=list(files))
    assert r["verdict"] == "pass", r
    return r


def test_revise_invalidates_closure_and_reopens_mission(env):
    """上游修订 → 反向依赖+整合祖先全失效 → mission 重开 → 链条重建后重新闭合。"""
    g, repo, tmp = env
    mid, root = g.create_mission("修订链", root_check=CHK)
    src = g.add_node(mid, "数据源", parent_id=root, check_cmd=CHK,
                     declared_artifacts=("data.txt",))
    proc = g.add_node(mid, "加工", parent_id=root, deps=(src,),
                      check_cmd=CHK, declared_artifacts=("out.txt",))
    root_decl = ("final.txt",)
    g.repo.update_node(root, declared_artifacts_json=json.dumps(list(root_decl)))
    _run_pass(g, mid, src, {"data.txt": "v1"})
    _run_pass(g, mid, proc, {"out.txt": "P(v1)"})
    _run_pass(g, mid, root, {"final.txt": "F(v1)"})
    assert repo.get_mission(mid).status == "done"
    old_src_snap = repo.latest_valid_snapshot(src)
    assert old_src_snap is not None and old_src_snap.artifact_version == 1

    # 修订上游数据源
    new_rev = g.revise_node(mid, src, "数据口径变更")
    assert new_rev == 2
    assert repo.get_node(src).status == "pending"
    # 下游与根：证据作废、快照失效、状态回退
    assert all(s.valid == 0 for s in repo.snapshots_of(src))
    assert all(s.valid == 0 for s in repo.snapshots_of(proc))
    assert all(a.invalid == 1 for a in repo.assessments_of(proc))
    assert repo.get_node(proc).status == "pending"
    assert repo.get_node(root).status == "waiting_children"
    assert repo.get_mission(mid).status == "active"  # 自动重开
    types = [e.type for e in repo.events_of(mid)]
    assert "node_revised" in types and "evidence_invalidated" in types
    assert "mission_reopened" in types

    # 历史保留：旧文件仍在 accepted（可审计），只是 valid=0
    p_old = (repo.db_path.parent / "accepted" / src / "v1" / "data.txt")
    assert p_old.is_file()

    # 重建链条：v2 快照 + 下游新证据
    r2 = _run_pass(g, mid, src, {"data.txt": "v2"})
    assert r2["snapshot_id"] != old_src_snap.id
    assert repo.latest_valid_snapshot(src).artifact_version == 2
    _run_pass(g, mid, proc, {"out.txt": "P(v2)"})
    _run_pass(g, mid, root, {"final.txt": "F(v2)"})
    assert repo.get_mission(mid).status == "done"
    assert not [a for a in repo.assessments_of(proc) if a.invalid == 0
                and a.verdict == "pass"] is None


def test_stale_submit_after_revision_rejected(env):
    """修订后在途旧尝试提交：P0 指纹（node_revision）不匹配 → 拒。"""
    g, repo, tmp = env
    mid, root = g.create_mission("旧提交拒", root_check=CHK)
    a = g.add_node(mid, "A", parent_id=root, check_cmd=CHK,
                   declared_artifacts=("x.txt",))
    aid = g.start_attempt(mid, a)
    (Path(repo.get_attempt(aid).workdir) / "x.txt").write_text("old")
    g.revise_node(mid, a, "口径变更")
    # revise 会取消在途——重新手工置 running 模拟"迟到提交"竞争
    repo.update_attempt(aid, status="running")
    with pytest.raises(GraphError, match="修订已变更"):
        g.submit_attempt(mid, a, aid, artifact_paths=["x.txt"])


def test_repair_during_attempt_rejects_old_submit(env):
    """考卷维修（check_version+1）后旧尝试提交被拒。"""
    g, repo, tmp = env
    BITES = ("python -c \"import pathlib,sys; sys.exit(0 if any("
             "pathlib.Path('.').glob('x.txt')) else 1)\"")
    mid, root = g.create_mission("维修竞争", root_check=CHK)
    a = g.add_node(mid, "A", parent_id=root, check_cmd=BITES,
                   declared_artifacts=("x.txt",))
    aid = g.start_attempt(mid, a)
    (Path(repo.get_attempt(aid).workdir) / "x.txt").write_text("ok")
    g.repair_check(mid, a, new_check_cmd=BITES + " ", reason="修考卷")
    repo.update_attempt(aid, status="running")  # 模拟迟到提交
    with pytest.raises(GraphError, match="考卷已维修"):
        g.submit_attempt(mid, a, aid, artifact_paths=["x.txt"])


def test_review_binds_snapshot_and_rejects_foreign(env):
    """主观签收绑定本节点快照；他节点快照拒收。"""
    g, repo, tmp = env
    mid, root = g.create_mission("签收", root_check=CHK)
    a = g.add_node(mid, "A", parent_id=root, check_cmd=CHK,
                   declared_artifacts=("x.txt",))
    b = g.add_node(mid, "B", parent_id=root, check_cmd=CHK,
                   declared_artifacts=("y.txt",))
    ra = _run_pass(g, mid, a, {"x.txt": "内容"})
    rb = _run_pass(g, mid, b, {"y.txt": "别家"})
    rid = g.attach_review(mid, a, "accept", note="核过", snapshot_id=ra["snapshot_id"],
                          basis="对照任务书逐条核对")
    rv = repo.reviews_of(a)[-1]
    assert rv.snapshot_id == ra["snapshot_id"] and "逐条核对" in rv.basis
    with pytest.raises(GraphError, match="不属于"):
        g.attach_review(mid, a, "accept", snapshot_id=rb["snapshot_id"], basis="x")
    assert rid > 0


def test_subjective_closure_requires_basis(env):
    """主观闭合必须带签收依据；closure_basis=subjective 永不冒充机器。"""
    g, repo, tmp = env
    mid, root = g.create_mission("主观", root_check=CHK)
    with pytest.raises(GraphError, match="签收依据"):
        g.close_mission_subjective(mid, note="差不多")
    g.close_mission_subjective(mid, note="人审通过", basis="审阅 3 篇产物快照后签收")
    m = repo.get_mission(mid)
    assert m.status == "done" and m.closure_basis == "subjective"
    ev = [e for e in repo.events_of(mid) if e.type == "mission_done_subjective"]
    assert ev and "签收" in ev[-1].payload_json


def test_reopen_mission_explicit(env):
    """done 任务显式重开：根回 waiting_children，事件留痕。"""
    g, repo, tmp = env
    mid, root = g.create_mission("重开", root_check=CHK)
    kid = g.add_node(mid, "K", parent_id=root, check_cmd=CHK,
                     declared_artifacts=("k.txt",))
    _run_pass(g, mid, kid, {"k.txt": "v1"})
    _run_pass(g, mid, root, {"final.txt": "F"})
    assert repo.get_mission(mid).status == "done"
    g.reopen_mission(mid, "需求变更重跑")
    assert repo.get_mission(mid).status == "active"
    assert repo.get_node(root).status == "waiting_children"
    with pytest.raises(GraphError, match="无须重开"):
        g.reopen_mission(mid, "重复重开")
