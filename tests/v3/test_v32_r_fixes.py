"""v3.2-R 修复回归：Codex 独立复核十项负例的"已修复"行为断言。

来源：work/2026-10-03-v32-review/probe_v32_contracts.py（10 项缺陷复现）。
通过口径：缺陷行为不再发生（赢家工件保留/崩溃可恢复/考卷内容锁定/
绑定强制/闭包完整/恢复续跑/回执可重试/执行位必释放/投影版本不丢）。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from dag_agent.artifacts import ArtifactStore, sha256_file
from dag_agent.daemon import RunManager
from dag_agent.graph import Graph, GraphError
from dag_agent.recovery import guarded_action, pause, resume
from dag_agent.repository import Repository
from dag_agent.scheduler import Scheduler

CHK = "python -c 'pass'"
BITES = ("python -c \"import pathlib,sys; sys.exit(0 if any("
         "pathlib.Path('.').glob('impl.txt')) else 1)\"")


def _wait(cond, timeout=20.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.02)
    return False


class Pass:
    def __call__(self, cmd, cwd):
        return True, "pass"


def _run(g, mid, nid, files):
    aid = g.start_attempt(mid, nid)
    wd = Path(g.repo.get_attempt(aid).workdir)
    for n, c in files.items():
        (wd / n).write_text(c, encoding="utf-8")
    return g.submit_attempt(mid, nid, aid, artifact_paths=list(files))


# ---- 1. 并发提交：赢家工件不被拒收方删除 ----

def test_concurrent_publish_keeps_winner_files(tmp_path):
    repo = Repository(tmp_path / "m" / "d.db")
    g = Graph(repo, checker=Pass())
    mid, root = g.create_mission("并发", root_check=CHK,
                                 root_declared_artifacts=("impl.txt",))
    a = g.add_node(mid, "A", "A", parent_id=root, check_cmd=CHK,
                   declared_artifacts=("impl.txt",))
    aid = g.start_attempt(mid, a)
    wd = Path(repo.get_attempt(aid).workdir)
    (wd / "impl.txt").write_text("WINNER", encoding="utf-8")
    # 屏障：两个提交线程都完成 staging 后再放行 P4
    real_stage = ArtifactStore.stage_prepared
    barrier = threading.Barrier(2, timeout=15)

    def staged(self, mid_, nid_, att_, workdir_, rels):
        out = real_stage(self, mid_, nid_, att_, workdir_, rels)
        barrier.wait()
        return out
    ArtifactStore.stage_prepared = staged
    results = {}

    def submit(tag):
        try:
            results[tag] = g.submit_attempt(mid, a, aid,
                                            artifact_paths=["impl.txt"])
        except Exception as e:  # noqa: BLE001
            results[tag] = {"verdict": "raise", "error": str(e)}
    try:
        t1 = threading.Thread(target=submit, args=("t1",))
        t2 = threading.Thread(target=submit, args=("t2",))
        t1.start(); t2.start(); t1.join(30); t2.join(30)
    finally:
        ArtifactStore.stage_prepared = real_stage
    verdicts = sorted(r.get("verdict", "?") for r in results.values())
    assert verdicts in (["pass", "raise"], ["pass", "rejected"]), results
    # 赢家工件与登记完好（旧缺陷：文件被败者删除）
    arts = repo.artifacts_of(a)
    assert len(arts) == 1
    f = repo.db_path.parent / "accepted" / a / f"v{arts[0].version}" / "impl.txt"
    assert f.read_text(encoding="utf-8") == "WINNER"
    assert sha256_file(f) == arts[0].content_hash
    assert repo.get_node(a).status == "accepted"
    repo.close()


# ---- 2. P3 后崩溃：rename 已发生但未提交 → 孤儿被幂等替换，可恢复闭合 ----

def test_prepared_crash_recovers(tmp_path):
    repo = Repository(tmp_path / "m" / "d.db")
    g = Graph(repo, checker=Pass())
    mid, root = g.create_mission("崩溃", root_check=CHK,
                                 root_declared_artifacts=("impl.txt",))
    a = g.add_node(mid, "A", "A", parent_id=root, check_cmd=CHK,
                   declared_artifacts=("impl.txt",))
    # 模拟：尝试1 已 staging 并 rename 到 v1，随后崩溃（无 DB 行）
    aid1 = g.start_attempt(mid, a)
    wd1 = Path(repo.get_attempt(aid1).workdir)
    (wd1 / "impl.txt").write_text("OLD", encoding="utf-8")
    store = g.store
    staging, _rows = store.stage_prepared(mid, a, aid1, wd1, ["impl.txt"])
    dest = store._version_dir(a, 1)
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging.rename(dest)                      # rename 完成、事务未发生
    with repo.tx() as cur:                    # 尝试1 作废（模拟进程死亡）
        cur.execute("UPDATE attempts SET status='orphaned' WHERE id=?", (aid1,))
        cur.execute("UPDATE nodes SET status='pending' WHERE id=?", (a,))
    # 尝试2：正常认领提交——孤儿 v1 被替换，不再 blocked
    r = _run(g, mid, a, {"impl.txt": "NEW"})
    assert r["verdict"] == "pass", r
    f = repo.db_path.parent / "accepted" / a / "v1" / "impl.txt"
    assert f.read_text(encoding="utf-8") == "NEW"
    assert repo.get_node(a).status == "accepted"
    repo.close()


# ---- 3. 考卷内容锁定：认领后改脚本内容 → 提交被拒 ----

def test_mutable_exam_rejected(tmp_path):
    repo = Repository(tmp_path / "m" / "d.db")
    script = tmp_path / "chk.py"
    script.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    g = Graph(repo)  # 真检查器
    from dag_agent.acceptance import validate
    acc = [{"action": "script", "path": str(script)}]
    ok, why = validate(acc)
    assert ok, why
    mid, root = g.create_mission("锁卷", root_check=CHK)
    a = g.add_node(mid, "A", "A", parent_id=root, acceptance=acc,
                   declared_artifacts=("x.txt",))
    aid = g.start_attempt(mid, a)
    wd = Path(repo.get_attempt(aid).workdir)
    (wd / "x.txt").write_text("BAD", encoding="utf-8")
    script.write_text("import sys\nsys.exit(0)\n# lenient now\n",
                      encoding="utf-8")       # 内容变、版本号不变
    with pytest.raises(GraphError, match="考卷内容已变更"):
        g.submit_attempt(mid, a, aid, artifact_paths=["x.txt"])
    repo.close()


# ---- 4. expected_version 缺失：拒绝认领，禁止空绑定 ----

def test_missing_expected_version_refused(tmp_path):
    repo = Repository(tmp_path / "m" / "d.db")
    g = Graph(repo, checker=Pass())
    mid, root = g.create_mission("强制绑定", root_check=CHK)
    src = g.add_node(mid, "S", "S", parent_id=root, check_cmd=CHK,
                     declared_artifacts=("s.txt",))
    _run(g, mid, src, {"s.txt": "v1"})
    with repo.tx() as cur:
        cur.execute("UPDATE dependencies SET expected_version=999"
                    " WHERE 0")  # 占位：依赖在建点时声明
    consumer = g.add_node(mid, "C", "C", parent_id=root, deps=(src,),
                          check_cmd=CHK)
    with repo.tx() as cur:
        cur.execute("UPDATE dependencies SET expected_version=999"
                    " WHERE node_id=?", (consumer,))
    with pytest.raises(GraphError, match="拒绝认领"):
        g.start_attempt(mid, consumer)
    repo.close()


# ---- 5. 跨父失效：消费者在另一父下，其整合父必须一并失效 ----

def test_nested_ancestor_invalidated(tmp_path):
    repo = Repository(tmp_path / "m" / "d.db")
    g = Graph(repo, checker=Pass())
    mid, root = g.create_mission("跨父", root_check=CHK)
    p_src = g.add_node(mid, "P源", "整合", parent_id=root, check_cmd=CHK)
    x = g.add_node(mid, "X", "X", parent_id=p_src, check_cmd=CHK,
                   declared_artifacts=("x.txt",))
    _run(g, mid, x, {"x.txt": "v1"})
    _run(g, mid, p_src, {"px.txt": "整合v1"})
    p_q = g.add_node(mid, "Q整", "整合", parent_id=root, check_cmd=CHK)
    c = g.add_node(mid, "C", "C", parent_id=p_q, deps=(x,), check_cmd=CHK,
                   declared_artifacts=("c.txt",))
    _run(g, mid, c, {"c.txt": "用v1"})
    _run(g, mid, p_q, {"q.txt": "整合Q"})
    g.revise_node(mid, x, "上游变更")
    assert repo.get_node(c).status == "pending"
    # 修复点：C 的整合父 Q 也必须失效（旧缺陷：Q 仍 accepted+快照 valid）
    assert repo.get_node(p_q).status == "waiting_children"
    assert all(s.valid == 0 for s in repo.snapshots_of(p_q))
    assert all(a.invalid == 1 for a in repo.assessments_of(p_q))
    repo.close()


# ---- 6. OR 赢家失效：下游 deps 写在成员上，修订赢家也级联 ----

def test_group_winner_revision_cascades(tmp_path):
    repo = Repository(tmp_path / "m" / "d.db")
    g = Graph(repo, checker=Pass())
    mid, root = g.create_mission("OR失效", root_check=CHK)
    a = g.add_node(mid, "路线A", "A", parent_id=root, check_cmd=CHK,
                   declared_artifacts=("impl.txt",), group_id="r",
                   group_order=0)
    b = g.add_node(mid, "路线B", "B", parent_id=root, check_cmd=CHK,
                   declared_artifacts=("impl.txt",), group_id="r",
                   group_order=1)
    g.checker = lambda cmd, cwd: (False, "A 不行")
    _run(g, mid, a, {"impl.txt": "A"})
    g.checker = Pass()
    _run(g, mid, b, {"impl.txt": "B"})       # 赢家 B
    d = g.add_node(mid, "下游", "D", parent_id=root, deps=(a,),  # deps 写在 A 上
                   check_cmd=CHK, declared_artifacts=("d.txt",))
    _run(g, mid, d, {"d.txt": "用赢家B"})
    g.revise_node(mid, b, "赢家修订")
    # 修复点：按真实绑定（赢家B）级联——下游 D 失效（旧缺陷：D 仍 accepted）
    assert repo.get_node(d).status == "pending"
    assert all(s.valid == 0 for s in repo.snapshots_of(d))
    assert repo.get_mission(mid).status == "active"
    repo.close()


# ---- 7. 全暂停→恢复：kick 自动续跑直至闭合 ----

def test_pause_all_resume_kicks(tmp_path):
    repo = Repository(tmp_path / "m" / "mission.db")
    g = Graph(repo, checker=Pass())

    class BE:
        name = "stub"  # A3 闸门豁免：测试桩
        def run(self, statement, workdir, check_cmd, title=""):
            (Path(workdir) / "impl.txt").write_text(title or "x",
                                                    encoding="utf-8")
            return {"ok": True, "summary": "D", "session_id": "",
                    "resources": {"cost_usd": 0.0}}
    mid, root = g.create_mission("暂停续跑", root_check=BITES,
                                 root_declared_artifacts=("impl.txt",))
    n1 = g.add_node(mid, "N1", "1", parent_id=root, check_cmd=BITES,
                    declared_artifacts=("impl.txt",))
    n2 = g.add_node(mid, "N2", "2", parent_id=root, check_cmd=BITES,
                    declared_artifacts=("impl.txt",))
    mgr = RunManager(tmp_path)
    pause(repo, g, mid, n2)                  # 暂停未派发节点 n2 → n1 完成后死局退出（预期）
    assert mgr.start(mid, max_concurrency=1, backend_factory=BE,
                     project=False, timeout_s=30)["ok"]
    assert _wait(lambda: repo.get_node(n1).status == "accepted")  # 先完成在途 n1
    assert _wait(lambda: not mgr.status(mid)["running"])
    resume(repo, g, mid, n2)
    kick = mgr.kick(mid)                     # 修复点：恢复后自动续跑
    assert kick.get("ok") and kick.get("kicked"), kick
    assert _wait(lambda: mgr.status(mid)["mission_status"] == "done")
    assert repo.get_node(n1).status == "accepted"
    assert repo.get_node(n2).status == "accepted"
    repo.close()


# ---- 8. 回执：失败可重试；resume 可重复生效不假成功 ----

def test_receipt_failure_retry_and_repeatable_resume(tmp_path):
    repo = Repository(tmp_path / "m" / "d.db")
    g = Graph(repo, checker=Pass())
    mid, root = g.create_mission("回执", root_check=CHK)
    a = g.add_node(mid, "A", "A", parent_id=root, check_cmd=CHK)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("第一次失败")
        return {"ok": True, "call": calls["n"]}
    with pytest.raises(RuntimeError):
        guarded_action(repo, "act:1", flaky)   # 失败落 failed 回执
    out = guarded_action(repo, "act:1", flaky)  # 同 key 重试真实执行
    assert out == {"ok": True, "call": 2}       # 修复点：不再被失败回执阻断
    # resume：两次 blocked→resume 都真实生效（旧缺陷：第二次假成功）
    pause(repo, g, mid, a, "r1")
    resume(repo, g, mid, a)
    assert repo.get_node(a).status == "pending"
    pause(repo, g, mid, a, "r2")
    resume(repo, g, mid, a)                  # 再次 blocked 后再次恢复：真实生效
    assert repo.get_node(a).status == "pending"
    repo.close()


# ---- 9. worker 异常：执行位必释放、节点可恢复重派 ----

def test_worker_exception_releases_slot(tmp_path):
    repo = Repository(tmp_path / "m" / "d.db")
    g = Graph(repo, checker=Pass())

    class Boom:
        name = "stub"  # A3 闸门豁免：测试桩
        def __init__(self):
            self.fuse = True
        def run(self, statement, workdir, check_cmd, title=""):
            if self.fuse:
                raise RuntimeError("backend 崩溃")
            (Path(workdir) / "impl.txt").write_text("ok", encoding="utf-8")
            return {"ok": True, "summary": "D", "session_id": "",
                    "resources": {"cost_usd": 0.0}}
    be = Boom()
    mid, root = g.create_mission("异常", root_check=BITES,
                                 root_declared_artifacts=("impl.txt",))
    a = g.add_node(mid, "A", "A", parent_id=root, check_cmd=BITES,
                   declared_artifacts=("impl.txt",))
    s = Scheduler(repo, g, be, max_concurrency=1, reap_enabled=False)
    aid = g.start_attempt(mid, a)
    s._work(mid, a, aid, integration=False)
    # 修复点：结算+释放（旧缺陷：future 挂死、节点 in_work 僵尸）
    assert repo.get_attempt(aid).status == "error"
    assert repo.get_node(a).status == "blocked"
    with s._inflight_lock:
        assert aid not in s._futures
    assert "worker_error" in [e.type for e in repo.events_of(mid)]
    be.fuse = False
    g.unblock(mid, a)
    _run(g, mid, a, {"impl.txt": "修复后"})
    assert repo.get_node(a).status == "accepted"
    repo.close()


# ---- 10. 投影版本：旧渲染覆盖后，版本化渲染恢复版本标注与单调 ----

def test_projection_version_restored_after_plain_render(tmp_path):
    repo = Repository(tmp_path / "m" / "d.db")
    g = Graph(repo, checker=Pass())
    from dag_agent.projection import (render_mission,
                                      render_mission_versioned)
    mid, root = g.create_mission("投影", root_check=CHK)
    a = g.add_node(mid, "A", "A", parent_id=root, check_cmd=CHK,
                   declared_artifacts=("impl.txt",))
    _run(g, mid, a, {"impl.txt": "x"})
    board = repo.db_path.parent / "board"
    ov = board / "任务总览.md"
    assert render_mission_versioned(repo, g, mid) is not None
    marker = board / ".projection_version"
    v1 = int(marker.read_text(encoding="utf-8"))
    render_mission(repo, g, mid)               # 模拟旧路径无版本渲染（覆盖）
    assert "投影版本：事件#" not in ov.read_text(encoding="utf-8")
    with repo.tx() as cur:                     # 新事件推进版本
        repo.append_event(cur, mid, "probe_event", a)
    assert render_mission_versioned(repo, g, mid) is not None
    text = ov.read_text(encoding="utf-8")
    assert "投影版本：事件#" in text           # 修复点：版本标注恢复
    assert int(marker.read_text(encoding="utf-8")) > v1  # 单调不回退
    repo.close()


# ---- 11. 同默认原因第二次 pause 必须真实生效（R2+，Codex 复核P1） ----

def test_same_reason_pause_twice_blocks_again(tmp_path):
    from dag_agent.recovery import pause as _pause, resume as _resume
    repo = Repository(tmp_path / "m" / "mission.db")
    g = Graph(repo, checker=Pass())
    mid, root = g.create_mission("同因暂停", root_check=CHK)
    a = g.add_node(mid, "A", "A", parent_id=root, check_cmd=CHK)
    _pause(repo, g, mid, a)            # 默认 reason="paused"
    assert repo.get_node(a).status == "blocked"
    _resume(repo, g, mid, a)
    assert repo.get_node(a).status == "pending"
    _pause(repo, g, mid, a)            # 同原因第二次——必须再次 blocked
    assert repo.get_node(a).status == "blocked"
    repo.close()


# ---- 12. check_cmd 入口脚本包闭包：改入口/导入/资源均拒收（R2+） ----

def test_check_cmd_entry_package_locked(tmp_path):
    from dag_agent.acceptance import compute_check_digest
    d = tmp_path / "pkg"
    d.mkdir()
    entry = d / "chk.py"
    entry.write_text("import helper\nimport sys\n"
                     "sys.exit(0 if helper.strict() else 1)\n",
                     encoding="utf-8")
    (d / "helper.py").write_text("def strict():\n    return False\n",
                                 encoding="utf-8")
    (d / "data_ref.txt").write_text("v1", encoding="utf-8")
    cmd = f'python "{entry}" bad.txt'
    base = compute_check_digest(check_cmd=cmd)
    repo = Repository(tmp_path / "m" / "mission.db")
    from dag_agent.assessment import default_checker3
    g = Graph(repo, checker3=default_checker3)
    mid, root = g.create_mission("锁包", root_check=CHK)
    a = g.add_node(mid, "A", "A", parent_id=root, check_cmd=cmd)
    aid = g.start_attempt(mid, a)
    wd = Path(repo.get_attempt(aid).workdir)
    (wd / "bad.txt").write_text("BAD", encoding="utf-8")
    # ① 放宽入口导入的 helper → 摘要变 → 提交拒收
    (d / "helper.py").write_text("def strict():\n    return True\n",
                                 encoding="utf-8")
    assert compute_check_digest(check_cmd=cmd) != base
    with pytest.raises(GraphError, match="考卷内容已变更"):
        g.submit_attempt(mid, a, aid, artifact_paths=["bad.txt"])
    # ② 改同目录资源指针 → 摘要变（v1→v2 数据切换会正确过期旧尝试）
    (d / "helper.py").write_text("def strict():\n    return False\n",
                                 encoding="utf-8")
    (d / "data_ref.txt").write_text("v2", encoding="utf-8")
    assert compute_check_digest(check_cmd=cmd) != base
    with pytest.raises(GraphError, match="考卷内容已变更"):
        g.submit_attempt(mid, a, aid, artifact_paths=["bad.txt"])
    repo.close()
