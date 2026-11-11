# -*- coding: utf-8 -*-
"""v46：Codex 独立验收（2026-10-10）六项生长缺陷的负例回归。

对应验收报告 F1–F6，全部为行为负例转正式回归：
- F1 子考卷体检后漂移：出题产物冻结 child digest，apply 重核；
  出题产物引用本地脚本必须显式 exam_files（内容进摘要）。
- F2 规划期间 kill 撤销应用权：apply 事务内运行权限 gate；
  正式 stop(kill) + Event 精确控制"停止早于应用"。
- F3 孩子固定输入绑定：dependencies 携带 expected_version=父认领快照；
  旧快照失效后孩子认领必须拒绝（不改绑最新）。
- F4 异常结束会话不触发生长：超时/非零退出/取消回执三类均不激活提案。
- F5 留审提案（有交付+父 blocked）持久休眠：不进可规划查询、不占规划位；
  kick 恢复 waiting_split。
- F6 同提案内同名孩子：apply 批内规范化查重拒绝。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dag_agent import autogrow  # noqa: E402
from dag_agent.graph import Graph, GraphError  # noqa: E402
from dag_agent.recovery import RetryPolicy  # noqa: E402
from dag_agent.repository import Repository  # noqa: E402
from dag_agent.scheduler import AutogrowConfig, Scheduler  # noqa: E402

CMD_OK = ('python -c "import sys;from pathlib import Path;'
          "p=Path('deliverable.md');"
          'sys.exit(0 if p.is_file() and p.stat().st_size>10 else 1)"')

BRIEF = ("【输入】无外部输入。\n【交付清单】deliverable.md\n"
         "【接口签名】deliverable.md 为 UTF-8 文本，>10 字节。\n"
         "【样例】示例行\n【自测入口】运行考卷。\n【任务】{body}")


def make_proposal(children=None, pid="prop-v46", **overrides) -> str:
    if children is None:
        children = [
            {"local_id": "c1", "title": "子任务一",
             "statement": BRIEF.format(body="分件 A"),
             "check_hint": "交付 autogrow-c1-deliverable.md",
             "depends_on": [], "input_refs": [], "deliverables": []},
            {"local_id": "c2", "title": "子任务二",
             "statement": BRIEF.format(body="分件 B"),
             "check_hint": "交付 autogrow-c2-deliverable.md",
             "depends_on": ["c1"], "input_refs": [], "deliverables": []},
        ]
    doc = {
        "schema_version": autogrow.SCHEMA_VERSION,
        "proposal_id": pid,
        "source_attempt_id": "platform", "parent_node_id": "platform",
        "goal_version": -1, "node_revision": -1, "check_version": -1,
        "check_digest": "platform", "input_binding_hash": "platform",
        "reason": "v46 负例", "children": children,
    }
    doc.update(overrides)
    return json.dumps(doc, ensure_ascii=False)


def stage_split(repo, graph, mid: str, nid: str, pid: str,
                content: str = "c",
                input_binding_json: str | None = None) -> tuple[str, dict]:
    """造激活态（父 waiting_split + queued 提案 + split 源尝试）。"""
    from dag_agent.acceptance import compute_check_digest
    from dag_agent.repository import now
    node = repo.get_node(nid)
    fp = {"goal_version": repo.get_mission(mid).goal_version,
          "node_revision": node.revision,
          "check_version": node.check_version,
          "check_digest": compute_check_digest(
              check_cmd=node.check_cmd,
              acceptance=json.loads(node.acceptance_json)
              if node.acceptance_json else None,
              exam_files=graph._exam_files_of(node)),
          "input_binding_hash": "test-binding-hash"}
    aid = f"a-src-{pid}"
    ts = now()
    with repo.tx() as cur:
        cur.execute(
            "INSERT INTO attempts(id,node_id,mission_id,backend,status,"
            "workdir,input_binding_json,claim_fingerprint_json,category,"
            "started_at,ended_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (aid, nid, mid, "stub", "split", "",
             input_binding_json or "[]",
             json.dumps(fp), "split", ts, ts))
        repo.insert_split_proposal({
            "id": pid, "mission_id": mid, "node_id": nid,
            "source_attempt_id": aid, "raw_sha256": "r",
            "content_sha256": content, "raw_json": "{}",
            "fingerprint_json": json.dumps(fp), "status": "queued",
            "created_at": ts}, cur)
        cur.execute("UPDATE nodes SET status='waiting_split' WHERE id=?",
                    (nid,))
    return aid, fp


# ---------- F1 · 子考卷体检后漂移 ----------


def test_f1_child_exam_drift_rejected(tmp_path):
    """出题产物冻结 child digest：体检后到 apply 前考卷内容漂移 →
    apply 拒绝、无半图、无计数。引用本地脚本的出题产物必须显式
    exam_files（内容进摘要）——未声明即拒绝出题产物。"""
    repo, graph, mid, root = build_simple(tmp_path)
    aid, fp = stage_split(repo, graph, mid, root, "sp-f1", "c1")
    spec = {
        "title": "脚本子卷", "statement": BRIEF.format(body="s"),
        # 引用本地脚本但未声明 exam_files → 出题产物即拒绝
        "check_cmd": "python exam_child.py",
        "declared_artifacts": ["deliverable.md"], "deps": [],
        "source_local_id": "c1", "exam_files": [],
    }
    filler = {"title": "补位", "statement": BRIEF.format(body="f"),
              "check_cmd": CMD_OK,
              "declared_artifacts": ["deliverable.md"], "deps": [],
              "source_local_id": "c2", "exam_files": []}
    from dag_agent.acceptance import compute_check_digest as _ccd
    filler["_frozen_digest"] = _ccd(check_cmd=filler["check_cmd"],
                                    exam_files=())
    with pytest.raises(Exception, match="考卷包显式清单|脚本|exam_files"):
        graph.apply_auto_split(mid, root, aid, "sp-f1", [spec, filler], fp)
    # 合规形态：显式 exam_files + 脚本冻结
    exam = tmp_path / "exam_child.py"
    exam.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    spec2 = dict(spec, exam_files=[str(exam)])
    # digest 冻结（出题时刻）
    from dag_agent.acceptance import compute_check_digest
    frozen = compute_check_digest(check_cmd=spec2["check_cmd"],
                                  exam_files=tuple(spec2["exam_files"]))
    spec2["_frozen_digest"] = frozen
    filler2 = dict(filler, title="补位二", source_local_id="c2")
    kids = graph.apply_auto_split(mid, root, aid, "sp-f1",
                                  [dict(spec2), filler2], fp)
    assert len(kids) == 2
    # 出题→apply 窗口内脚本漂移（改成永绿）→ 再次应用同 digest 校验拒绝
    exam.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    aid2, fp2 = stage_split(repo, graph, mid, kids[0], "sp-f1b", "c2")
    spec3 = dict(spec2, source_local_id="c1", deps=[])
    spec4 = dict(filler2, title="孙补位", deps=[])
    with pytest.raises(Exception, match="漂移|摘要|digest|考卷内容已变更"):
        graph.apply_auto_split(mid, kids[0], aid2, "sp-f1b", [spec3, spec4],
                               fp2)
    row = repo.get_split_proposal("sp-f1b")
    assert row["status"] == "queued"      # 无半图：未计成功
    repo.close()


# ---------- F2 · kill 撤销应用权 ----------


class _BlockingPlanner(autogrow.DeterministicPlannerStub):
    """plan() 阻塞在 release 事件上——精确控制"停止早于应用"。"""

    def __init__(self, release: threading.Event):
        super().__init__()
        self._release = release
        self.entered = threading.Event()

    def plan(self, ctx):
        self.entered.set()
        self._release.wait(timeout=30)
        return super().plan(ctx)


def test_f2_kill_revokes_apply_right(tmp_path):
    """正式 RunManager.start/stop(kill)：规划被 Event 阻塞在出题阶段，
    kill 置位后释放规划——最终应用必须被运行权限 gate 拒绝：
    节点数不变、无 split_applied、提案留 queued。"""
    from dag_agent.daemon import RunManager

    def writer(title, workdir, statement):
        if "v46" not in title:
            return
        (Path(workdir) / ".dag").mkdir(exist_ok=True)
        (Path(workdir) / ".dag" / "proposal.json").write_text(
            make_proposal(), encoding="utf-8")

    home = tmp_path / "home"
    home.mkdir()
    repo = Repository(home / "mission.db")
    graph = Graph(repo)
    from test_v45_autogrow import BRIEF as _B  # noqa: E402  # 五段简报
    mid, root = graph.create_mission(
        "v46 F2", brief=_B.format(body="root"),
        root_check=CMD_OK, root_declared_artifacts=("deliverable.md",))

    release = threading.Event()
    planner = _BlockingPlanner(release)
    from test_v45_autogrow import AutoGrowBackend
    backend = AutoGrowBackend(on_write=writer, deliver="skip")

    class _B(backend.__class__):
        pass

    mgr = RunManager(home)   # 直实例，避免进程级单例污染
    r = mgr.start(mid, root=root, max_concurrency=1, real=False,
                  project=False, auto_split=True, planner=planner,
                  backend_factory=lambda: backend, timeout_s=120)
    assert r.get("ok")
    # 等规划 worker 进入出题（阻塞中）
    t0 = time.time()
    while time.time() - t0 < 20 and not planner.entered.is_set():
        time.sleep(0.05)
    assert planner.entered.is_set(), "规划未进入出题阶段"
    n_before = len(repo.list_nodes(mid))
    # 正式 kill：hard_stop 先置位（撤销应用权），stop 的 join 会等阻塞中
    # 的规划 worker 至 60s 超时（worker 仍挂在 release 上）——stopped=False
    # 如实；随后释放规划，apply 必须被运行权限 gate 拒绝。
    stop = mgr.stop(mid, mode="kill")
    release.set()
    run = mgr._runs[mid]
    run["thread"].join(timeout=90)
    assert not run["thread"].is_alive()
    # 断言：kill 后未落图
    n_after = len(repo.list_nodes(mid))
    assert n_after == n_before, f"kill 后节点数变化：{n_before}→{n_after}"
    assert not events_of_type(repo, mid, "split_applied")
    rows = repo.list_split_proposals(mid)
    # kill 撤销应用权：提案被 gate 显式拒绝（rejected 留痕）——断言核心
    # =未落图（无 split_applied、节点数不变）
    assert rows and rows[0]["status"] in ("queued", "rejected")
    if rows[0]["status"] == "rejected":
        assert "运行权限已撤销" in (rows[0]["reject_reason"] or "")
    repo.close()


class _NullishBackend:
    name = "stub"

    def run(self, statement, workdir, check_cmd, title="", cancel_event=None):
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat(
            timespec="milliseconds")
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "deliverable.md").write_text(
            "v46 nullish\n" + "x" * 40, encoding="utf-8")
        return {"ok": True, "summary": "ok", "session_id": "s-v46",
                "resources": {"origin": "stub", "agent_id": "s-v46",
                              "tokens": 0, "duration_ms": 1,
                              "cost_currency": "USD",
                              "executor_started_at": now_iso,
                              "executor_ended_at": now_iso,
                              "cost": {"amount": 0.0, "basis": "reported"},
                              "receipt_status": "valid"}}


def events_of_type(repo, mid, etype):
    return [e for e in repo.events_of(mid) if e.type == etype]


def build_simple(tmp_path, root_check=CMD_OK):
    from dag_agent.assessment import default_checker3
    repo = Repository(tmp_path / "mission.db")
    graph = Graph(repo, checker3=default_checker3)
    mid, root = graph.create_mission(
        "v46", brief=BRIEF.format(body="root"), root_check=root_check,
        root_declared_artifacts=("deliverable.md",))
    return repo, graph, mid, root


# ---------- F3 · 孩子固定输入绑定 ----------


def test_f3_children_pin_parent_snapshot(tmp_path):
    """孩子外部依赖继承父认领快照（expected_version）；旧快照失效后
    孩子认领必须拒绝，不改绑最新快照。"""
    repo, graph, mid, root = build_simple(tmp_path)
    up = graph.add_node(mid, title="上游", statement=BRIEF.format(body="u"),
                        parent_id=root, check_cmd=CMD_OK,
                        declared_artifacts=("deliverable.md",))
    p_node = graph.add_node(mid, title="父任务P",
                            statement=BRIEF.format(body="P"),
                            parent_id=root, deps=(up,), check_cmd=CMD_OK,
                            declared_artifacts=("deliverable.md",))
    b1 = _NullishBackend()
    s1 = Scheduler(repo, graph, b1, max_concurrency=2,
                   auto_split=AutogrowConfig(enabled=False),
                   retry_policy=RetryPolicy(max_retries=0))
    from concurrent.futures import ThreadPoolExecutor
    s1._pool = ThreadPoolExecutor(max_workers=2)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 15:
        s1.tick_dispatch(mid)
        if repo.get_node(up).status == "accepted":
            break
        time.sleep(0.05)
    assert repo.get_node(up).status == "accepted"
    s1._pool.shutdown(wait=True)
    if repo.get_node(p_node).status != "pending":
        graph.revise_node(mid, p_node, reason="v46 重置 P")
    aid = graph.start_attempt(mid, p_node, backend="stub")
    fp = json.loads(repo.get_attempt(aid).claim_fingerprint_json or "{}")
    binding = json.loads(repo.get_attempt(aid).input_binding_json or "[]")
    dep_snaps = {b["source"]: b.get("snapshot_id") for b in binding
                 if b.get("role") == "dep"}
    assert dep_snaps and all(v for v in dep_snaps.values())
    # 用源 attempt（真实认领）的绑定——apply 须把 snapshot id 固定进孩子依赖
    src_att = repo.get_attempt(aid)
    from dag_agent.acceptance import compute_check_digest as _ccd

    def _frozen(spec):
        spec = dict(spec)
        spec["_frozen_digest"] = _ccd(check_cmd=spec["check_cmd"],
                                      exam_files=())
        return spec

    aid_split, _fp_split = stage_split(repo, graph, mid, p_node,
                                       "sp-f3", "c",
                                       input_binding_json=src_att.input_binding_json)
    kids = graph.apply_auto_split(
        mid, p_node, aid_split, "sp-f3",
        [_frozen({"title": "pin-child", "statement": BRIEF.format(body="pc"),
                  "check_cmd": CMD_OK,
                  "declared_artifacts": ["deliverable.md"], "deps": [],
                  "source_local_id": "c1"}),
         _frozen({"title": "pin-child2", "statement": BRIEF.format(body="pc2"),
                  "check_cmd": CMD_OK,
                  "declared_artifacts": ["deliverable.md"], "deps": ["c1"],
                  "source_local_id": "c2"})],
        json.loads(src_att.claim_fingerprint_json or "{}"),
        source_binding_json=src_att.input_binding_json)
    by_title = {n.title: n.id for n in repo.list_nodes(mid)}
    c1 = by_title["pin-child"]
    deps = repo.deps_of(c1)
    assert deps, "孩子应有外部依赖"
    for d in deps:
        assert d.depends_on == up
        assert d.expected_version == dep_snaps[up], (
            f"孩子依赖须锁定父认领快照 {dep_snaps[up]}，"
            f"实际 {d.expected_version}")
    # 受控失效旧快照并提供新快照 → 孩子认领必须拒绝（不改绑）
    with repo.tx() as cur:
        cur.execute("UPDATE snapshots SET valid=0 WHERE id=?",
                    (dep_snaps[up],))
        cur.execute(
            "INSERT INTO snapshots(mission_id,node_id,attempt_id,"
            "artifact_version,node_revision,goal_version,manifest_hash,"
            "files_json,valid,created_at) VALUES(?,?,?,?,?,?,?,?,1,?)",
            (mid, up, "a-x", 2, 1, 1, "h2", "[]",
             __import__("dag_agent.repository", fromlist=["now"]).now()))
    with pytest.raises(Exception):
        graph.start_attempt(mid, c1, backend="stub")   # 拒绝改绑
    repo.close()


# ---------- F4 · 异常结束会话不触发生长 ----------


class _AbnormalBackend:
    """三类异常回执可编程：timeout / nonzero / cancelled。"""

    name = "stub"

    def __init__(self, mode: str, write_proposal: bool = True):
        self.mode = mode
        self.write_proposal = write_proposal

    def run(self, statement, workdir, check_cmd, title="", cancel_event=None):
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat(
            timespec="milliseconds")
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        if self.write_proposal:
            (workdir / ".dag").mkdir(exist_ok=True)
            (workdir / ".dag" / "proposal.json").write_text(
                make_proposal(), encoding="utf-8")
        base = {"origin": "stub", "agent_id": "s-ab", "tokens": 0,
                "duration_ms": 5, "cost_currency": "USD",
                "executor_started_at": now_iso,
                "executor_ended_at": now_iso,
                "cost": {"amount": None, "basis": "unknown"}}
        if self.mode == "timeout":
            return {"ok": False, "summary": "执行超时", "session_id": "",
                    "resources": {**base, "receipt_status": "incomplete",
                                  "timeout": True}}
        if self.mode == "nonzero":
            return {"ok": False, "summary": "exit=3", "session_id": "",
                    "resources": {**base, "receipt_status": "incomplete",
                                  "exit_code": 3}}
        # cancelled
        return {"ok": False, "summary": "被取消", "session_id": "",
                "resources": {**base, "receipt_status": "incomplete",
                              "cancelled": True}}


@pytest.mark.parametrize("mode", ["timeout", "nonzero", "cancelled"],
                         ids=["timeout", "nonzero", "cancelled"])
def test_f4_abnormal_sessions_no_growth(tmp_path, mode):
    """超时/非零退出/取消回执（均携带合法提案文件）不得触发生长：
    无 split_activated/applied；无交付按冻结运行协议 fail-closed。"""
    repo, graph, mid, root = build_simple(tmp_path)
    backend = _AbnormalBackend(mode)
    sched = Scheduler(repo, graph, backend, max_concurrency=1,
                      auto_split=AutogrowConfig(enabled=True),
                      retry_policy=RetryPolicy(max_retries=0))
    summary = sched.run_until_done(mid, root, timeout=40, poll=0.05)
    assert not events_of_type(repo, mid, "split_proposed")
    assert not events_of_type(repo, mid, "split_activated")
    assert not events_of_type(repo, mid, "split_applied")
    assert not repo.list_split_proposals(mid)
    assert summary["halt_reason"] and "死局" in summary["halt_reason"]
    repo.close()


# ---------- F5 · 留审提案不占规划位 ----------


def test_f5_error_wait_proposal_sleeps(tmp_path):
    """有交付+判卷 ERROR（父 blocked）的提案：持久休眠，不进可规划查询、
    不占规划位（30 tick 零派发零运行）、plan_attempts 不动。"""
    from dag_agent.executor_contract import record_execution
    # 父节点带合法提案文件交付，但考卷 ERROR（exit 2）——建卷即设
    # （repair 通道正确拒绝 exit2 坏卷：体检"能跑起"关在岗的旁证）
    repo, graph, mid, root = build_simple(
        tmp_path, root_check='python -c "import sys;sys.exit(2)"')
    aid = graph.start_attempt(mid, root, backend="stub")
    fp = json.loads(repo.get_attempt(aid).claim_fingerprint_json or "{}")
    backend = _AbnormalBackend("ok")
    # 手工走检测路径：交付存在（有交付）→判卷 ERROR→提案 queued 留审
    workdir = Path(repo.get_attempt(aid).workdir)
    (workdir / ".dag").mkdir(parents=True, exist_ok=True)
    (workdir / ".dag" / "proposal.json").write_text(
        make_proposal(), encoding="utf-8")
    (workdir / "deliverable.md").write_text("content\n" + "x" * 40,
                                            encoding="utf-8")
    record_execution(repo, aid, {
        "ok": True, "summary": "x", "session_id": "s5",
        "resources": {"origin": "stub", "agent_id": "s5", "tokens": 0,
                      "duration_ms": 1, "cost_currency": "USD",
                      "executor_started_at": "2026-10-10T00:00:00+00:00",
                      "executor_ended_at": "2026-10-10T00:00:01+00:00",
                      "cost": {"amount": 0.0, "basis": "reported"},
                      "receipt_status": "valid"}})
    sched = Scheduler(repo, graph, backend, max_concurrency=1,
                      auto_split=AutogrowConfig(enabled=True),
                      retry_policy=RetryPolicy(max_retries=0))
    from concurrent.futures import ThreadPoolExecutor
    sched._pool = ThreadPoolExecutor(max_workers=1)
    # 提案检测（模拟 _work_inner 的检测点调用）
    node = repo.get_node(root)
    handled = sched._detect_split_proposal(mid, root, aid, node, workdir)
    assert handled == "queued_keep"
    # 正常判卷 ERROR 提交
    result = graph.submit_attempt(mid, root, aid,
                                  artifact_paths=["deliverable.md"])
    assert result["verdict"] == "error"
    assert repo.get_node(root).status == "blocked"
    rows = repo.list_split_proposals(mid)
    assert rows[0]["status"] == "queued" and rows[0]["has_delivery"] == 1
    # 30 tick：不得派发规划、不得运行规划 worker、plan_attempts 不动
    dispatch_calls = {"n": 0}
    orig_plan = sched._plan_work

    def counting_plan(m, p):
        dispatch_calls["n"] += 1
        return orig_plan(m, p)

    sched._plan_work = counting_plan
    plan_keys = []
    for _ in range(30):
        before = len(sched._futures)
        sched.tick_dispatch(mid)
        if len(sched._futures) > before:
            plan_keys.append("dispatched")
        time.sleep(0.02)
    assert dispatch_calls["n"] == 0, (
        f"留审提案被派发 {dispatch_calls['n']} 次（F5 复现）")
    rows = repo.list_split_proposals(mid)
    assert rows[0]["status"] == "queued"
    assert rows[0]["plan_attempts"] == 0
    repo.close()


def test_f5b_kick_recovers_waiting_split(tmp_path):
    """kick 恢复条件覆盖 waiting_split（有 queued 提案的任务可续跑）。"""
    from dag_agent.daemon import RunManager
    home = tmp_path / "h"
    home.mkdir(exist_ok=True)
    repo, graph, mid, root = build_simple(home)
    stage_split(repo, graph, mid, root, "sp-kick", "c1")
    mgr = RunManager(home)
    r0 = mgr.start(mid, root=root, max_concurrency=1, real=False,
                   project=False, auto_split=True,
                   backend_factory=lambda: _NullishBackend(), timeout_s=60)
    assert r0.get("ok")
    mgr.stop(mid, mode="kill")   # 提案留 queued、父仍 waiting_split
    rows = repo.list_split_proposals(mid)
    assert rows and rows[0]["status"] == "queued"
    r = mgr.kick(mid)            # F5：kick 恢复 waiting_split
    assert r.get("ok") and r.get("kicked"), (
        f"kick 应恢复 waiting_split 任务：{r}")
    mgr.stop(mid, mode="kill")
    repo.close()


# ---------- F6 · 同提案内同名孩子 ----------


def test_f6_duplicate_sibling_titles_rejected(tmp_path):
    """同提案内两个同名孩子（local_id 不同）：apply 批内规范化查重拒绝，
    无半图、不计数。R5（Codex 复核）：规格必须带合法 _frozen_digest 走到
    目标闸门（缺摘要会先报冻结错误，标题含"同名"曾使 match 误匹配假绿）；
    断言锁定"生长闸门⑥"明确错误类别，并附唯一标题正例对照。"""
    repo, graph, mid, root = build_simple(tmp_path)
    from dag_agent.acceptance import compute_check_digest as _ccd

    def _frozen(title, body, local_id):
        return {"title": title, "statement": BRIEF.format(body=body),
                "check_cmd": CMD_OK, "declared_artifacts": ["deliverable.md"],
                "deps": [], "source_local_id": local_id,
                "_frozen_digest": _ccd(check_cmd=CMD_OK, exam_files=())}

    aid, fp = stage_split(repo, graph, mid, root, "sp-f6", "c")
    specs = [_frozen("同名孩子", "a", "c1"), _frozen("同名 孩子", "b", "c2")]
    n_before = len(repo.list_nodes(mid))
    with pytest.raises(GraphError, match="生长闸门⑥.*批内查重拒绝"):
        graph.apply_auto_split(mid, root, aid, "sp-f6", specs, fp)
    assert len(repo.list_nodes(mid)) == n_before
    row = repo.get_split_proposal("sp-f6")
    assert row["status"] == "queued"
    # 唯一标题正例对照：同形态合法规格应用成功（负例拒绝非误伤）
    aid2, fp2 = stage_split(repo, graph, mid, root, "sp-f6b", "d")
    ok_specs = [_frozen("标题甲", "a", "c1"), _frozen("标题乙", "b", "c2")]
    kids = graph.apply_auto_split(mid, root, aid2, "sp-f6b", ok_specs, fp2)
    assert len(kids) == 2
    repo.close()
