# -*- coding: utf-8 -*-
"""v47：Codex 复核轮（2026-10-10）R1–R4 修复的正式回归。

对应 outputs/DAG-agent-自动生长八项修复-Codex复核-2026-10-10.md 的独立
反例，全部转正为行为回归（探针手法 → pytest）：
- R1(F1) ①体检→冻结窗口：preflight 通过后、冻结前改卷 → 出题拒绝；
  ②apply 哈希后事务前窗口：摘要改在**事务内重算**（算完即开事务之间
  的漂移必被抓）+ 冻结摘要持久 node_exam_freeze，认领时重算比对；
  ③.pyw 入口漏锁：business_entry_paths 不按扩展名猜，空清单拒绝。
- R2(F2) 停止串行化：run_control 旗标落库，gate 在 apply 写锁内读——
  gate 读真后 stop 的旗标写被写锁推迟（hard_stop Event 不得在 apply
  提交前置位，线性化边界）；恢复重放：新代际清旗标后可再应用。
- R3(F3) 完整绑定契约：dep_bindings 持久 manifest+files 清单；同 id
  换清单/清空 files/篡改 manifest → 认领拒绝；正例核对播种内容。
- R4(F4) 平台结算受理：自报 valid 而平台 legacy_incomplete → 不激活；
  平台 valid 完整回执 → 正常激活（对照）。
R5(F6) 转正见 test_v46::test_f6（生长闸门⑥断言+唯一标题正例）。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dag_agent.graph import Graph, GraphError  # noqa: E402
from dag_agent.repository import Repository  # noqa: E402
from dag_agent.scheduler import AutogrowConfig, Scheduler  # noqa: E402

from test_v45_autogrow import (  # noqa: E402
    BRIEF, CMD_OK, AutoGrowBackend, build_mission, make_proposal,
    stage_split)

STRICT = ("from pathlib import Path\nimport sys\n"
          "p=Path('deliverable.md')\n"
          "sys.exit(0 if p.is_file() and p.read_text(encoding='utf-8')"
          "=='GOOD' else 1)\n")
GREEN = "import sys\nsys.exit(0)\n"


def _file_spec(path: Path, title: str, local_id: str) -> dict:
    return {"title": title,
            "statement": BRIEF.format(body="only GOOD qualifies"),
            "source_local_id": local_id, "deps": [],
            "check_cmd": f'python "{path}"', "exam_files": [str(path)],
            "declared_artifacts": ["deliverable.md"]}


class _FilePlanner:
    """独立出题桩：登记了文件的 local_id 用绝对路径考卷脚本（可漂移），
    未登记的用内联 CMD_OK 考卷（无文件引用，合法形态）。"""

    name = "v47-file-planner"

    def __init__(self, files: dict[str, Path]):
        self.files = files

    def plan(self, ctx):
        ch = ctx["children"][0]
        f = self.files.get(ch["local_id"])
        if f is None:
            return {"children": [
                {"title": ch["title"],
                 "statement": BRIEF.format(body="inline"),
                 "source_local_id": ch["local_id"], "deps": [],
                 "check_cmd": CMD_OK, "exam_files": [],
                 "declared_artifacts": ["deliverable.md"]}]}
        return {"children": [_file_spec(f, ch["title"], ch["local_id"])]}


# ---------- R1 · 三个时序/语义窗口 ----------


def test_r1_preflight_window_drift_rejected(tmp_path, monkeypatch):
    """反例①（preflight_return）：体检通过后、冻结摘要前把 STRICT 改成
    永绿——修复后冻结先于体检且前后双摘要一致才认可，漂移必须拒绝。"""
    import dag_agent.acceptance as acceptance
    repo, graph, mid, parent = build_mission(tmp_path)
    aid, fp = stage_split(repo, graph, mid, parent, "sp-r1a")
    repo.update_split_proposal("sp-r1a", raw_json=make_proposal(pid="sp-r1a"))
    files = {}
    for local in ("c1", "c2"):
        p = tmp_path / f"{local}_judge.py"
        p.write_text(STRICT, encoding="utf-8")
        files[local] = p
    original_preflight = acceptance.preflight
    drifted = {"hit": False}

    def intercept(*args, **kwargs):
        result = original_preflight(*args, **kwargs)
        cmd = kwargs.get("check_cmd", "")
        # 体检刚验证完 c1 的 STRICT，返回前立刻换永绿（窗口正中）
        if result[0] and str(files["c1"]) in cmd and not drifted["hit"]:
            drifted["hit"] = True
            files["c1"].write_text(GREEN, encoding="utf-8")
        return result

    monkeypatch.setattr(acceptance, "preflight", intercept)
    sched = Scheduler(repo, graph, AutoGrowBackend(),
                      auto_split=AutogrowConfig(enabled=True),
                      planner=_FilePlanner(files))
    sched._plan_work(mid, "sp-r1a")
    row = repo.get_split_proposal("sp-r1a")
    assert row["status"] == "rejected", row["reject_reason"]
    assert "体检期间漂移" in (row["reject_reason"] or "")
    assert drifted["hit"], "探针未命中体检窗口（测试自身失效）"
    assert len(repo.list_nodes(mid)) == 1      # 无半图
    repo.close()


def test_r1_apply_hash_to_tx_window_closed(tmp_path):
    """反例②（hash_before_tx）：摘要算完到开事务之间改卷——修复后摘要
    在事务内重算，该窗口不可能再漏（重算值≠冻结值即拒绝）。用确定性
    手法：冻结后改卷再 apply，事务内重算必须拒绝。"""
    from dag_agent.acceptance import compute_check_digest as _ccd
    repo, graph, mid, parent = build_mission(tmp_path)
    aid, fp = stage_split(repo, graph, mid, parent, "sp-r1b")
    script = tmp_path / "child_judge.py"
    script.write_text(STRICT, encoding="utf-8")
    spec = _file_spec(script, "严格子卷", "c1")
    spec["_frozen_digest"] = _ccd(check_cmd=spec["check_cmd"],
                                  exam_files=(str(script),))
    filler = {"title": "补位", "statement": BRIEF.format(body="f"),
              "check_cmd": CMD_OK, "declared_artifacts": ["deliverable.md"],
              "deps": [], "source_local_id": "c2",
              "_frozen_digest": _ccd(check_cmd=CMD_OK, exam_files=())}
    # 出题冻结之后（事务外任意时刻）改卷为永绿
    script.write_text(GREEN, encoding="utf-8")
    with pytest.raises(GraphError, match="考卷内容已漂移"):
        graph.apply_auto_split(mid, parent, aid, "sp-r1b",
                               [spec, filler], fp)
    assert repo.get_split_proposal("sp-r1b")["status"] == "queued"
    assert len(repo.list_nodes(mid)) == 1
    repo.close()


def test_r1_pyw_entry_requires_exam_files(tmp_path):
    """反例③（.pyw）：入口识别不按扩展名猜——引用 .pyw 而清单为空，
    出题闸门与权威事务闸门都必须拒绝（旧正则 \\.py\\b 漏 .pyw）。"""
    from dag_agent.acceptance import compute_check_digest as _ccd
    repo, graph, mid, parent = build_mission(tmp_path)
    aid, fp = stage_split(repo, graph, mid, parent, "sp-r1c")
    pyw = tmp_path / "child_judge.pyw"
    pyw.write_text(GREEN, encoding="utf-8")
    spec = {"title": "pyw子卷", "statement": BRIEF.format(body="s"),
            "check_cmd": f'python "{pyw}"', "exam_files": [],
            "declared_artifacts": ["deliverable.md"], "deps": [],
            "source_local_id": "c1",
            "_frozen_digest": _ccd(check_cmd=f'python "{pyw}"',
                                   exam_files=())}
    filler = {"title": "补位", "statement": BRIEF.format(body="f"),
              "check_cmd": CMD_OK, "declared_artifacts": ["deliverable.md"],
              "deps": [], "source_local_id": "c2",
              "_frozen_digest": _ccd(check_cmd=CMD_OK, exam_files=())}
    # 出题闸门（scheduler 预检）：规划器产出"引用 .pyw 但清单为空"的
    # 规格——正是旧正则漏识别的形态
    class _PywPlanner:
        name = "v47-pyw-planner"

        def __init__(self, target: Path):
            self.target = target

        def plan(self, ctx):
            ch = ctx["children"][0]
            return {"children": [
                {"title": ch["title"], "statement": BRIEF.format(body="s"),
                 "source_local_id": ch["local_id"], "deps": [],
                 "check_cmd": f'python "{self.target}"', "exam_files": [],
                 "declared_artifacts": ["deliverable.md"]}]}

    sched = Scheduler(repo, graph, AutoGrowBackend(),
                      auto_split=AutogrowConfig(enabled=True),
                      planner=_PywPlanner(pyw))
    repo.update_split_proposal("sp-r1c", raw_json=make_proposal(pid="sp-r1c"))
    sched._plan_work(mid, "sp-r1c")
    row = repo.get_split_proposal("sp-r1c")
    assert row["status"] == "rejected"
    assert "exam_files" in (row["reject_reason"] or "")
    # 权威事务闸门（graph 直调，防绕过出题闸门）
    aid2, fp2 = stage_split(repo, graph, mid, parent, "sp-r1c2")
    with pytest.raises(GraphError, match="未声明\\s*exam_files"):
        graph.apply_auto_split(mid, parent, aid2, "sp-r1c2",
                               [spec, filler], fp2)
    assert len(repo.list_nodes(mid)) == 1
    repo.close()


def test_r1_freeze_digest_blocks_claim_after_drift(tmp_path):
    """apply 验过→孩子认领前漂移：冻结摘要持久 node_exam_freeze，
    认领时重算比对——漂移考卷不得作为新合法考卷被认领。"""
    repo, graph, mid, parent = build_mission(tmp_path)
    aid, fp = stage_split(repo, graph, mid, parent, "sp-r1d")
    repo.update_split_proposal("sp-r1d", raw_json=make_proposal(pid="sp-r1d"))
    script = tmp_path / "judge_r1d.py"
    script.write_text(STRICT, encoding="utf-8")
    planner = _FilePlanner({"c1": script, "c2": tmp_path / "j2.py"})
    (tmp_path / "j2.py").write_text(STRICT, encoding="utf-8")
    sched = Scheduler(repo, graph, AutoGrowBackend(),
                      auto_split=AutogrowConfig(enabled=True),
                      planner=planner)
    sched._plan_work(mid, "sp-r1d")
    row = repo.get_split_proposal("sp-r1d")
    assert row["status"] == "applied"
    kids = json.loads(row["children_created_json"])
    frz = repo._conn.execute(
        "SELECT exam_digest FROM node_exam_freeze WHERE node_id=?",
        (kids[0],)).fetchone()
    assert frz is not None
    n_att = repo._conn.execute(
        "SELECT COUNT(*) AS c FROM attempts WHERE node_id=?",
        (kids[0],)).fetchone()["c"]
    # 认领前把考卷改成永绿——认领必须拒绝
    script.write_text(GREEN, encoding="utf-8")
    with pytest.raises(GraphError, match="冻结摘要不一致"):
        graph.start_attempt(mid, kids[0], backend="stub")
    n_att2 = repo._conn.execute(
        "SELECT COUNT(*) AS c FROM attempts WHERE node_id=?",
        (kids[0],)).fetchone()["c"]
    assert n_att2 == n_att, "拒绝认领不得留下 attempt"
    repo.close()


# ---------- R2 · 停止串行化 ----------


def test_r2_gate_then_kill_serialized(tmp_path):
    """反例（kill_after_gate）：apply 事务内（gate 已读真）暂停，
    公开 stop(kill)——修复后 stop 的旗标写必须等写锁：hard_stop Event
    不得在 apply 提交前置位（旧实现 Event.set 在锁外，暂停期内即置位）。
    释放后 apply 先提交（撤销前已完成的合法应用），旗标随后落库，
    stop 如实返回。"""
    from dag_agent.daemon import RunManager

    def writer(title, workdir, statement):
        if "v47" not in title:
            return
        (Path(workdir) / ".dag").mkdir(exist_ok=True)
        (Path(workdir) / ".dag" / "proposal.json").write_text(
            make_proposal(pid="sp-r2-serial"), encoding="utf-8")

    home = tmp_path / "home"
    home.mkdir()
    repo = Repository(home / "mission.db")
    graph = Graph(repo)
    from test_v45_autogrow import AutoGrowBackend as _AGB
    backend = _AGB(on_write=writer, deliver="skip")
    mid, root = graph.create_mission(
        "v47 R2 serial", brief=BRIEF.format(body="root"),
        root_check=CMD_OK, root_declared_artifacts=("deliverable.md",))
    mgr = RunManager(home)
    r = mgr.start(mid, root=root, max_concurrency=1, real=False,
                  project=False, auto_split=True, backend_factory=lambda: backend,
                  timeout_s=120)
    assert r.get("ok")
    sched = mgr._runs[mid]["scheduler"]
    gate_passed = threading.Event()
    transaction_release = threading.Event()
    old_active = sched.graph._mission_active

    def pause_inside_apply(*args, **kw):
        import inspect
        if (inspect.currentframe().f_back.f_code.co_name
                == "apply_auto_split" and not gate_passed.is_set()):
            gate_passed.set()
            assert transaction_release.wait(30), "事务探针超时"
        return old_active(*args, **kw)

    sched.graph._mission_active = pause_inside_apply
    assert gate_passed.wait(30), "apply 未进入事务（gate 之后暂停点未达）"
    n_before = len(repo.list_nodes(mid))
    stop_result: dict = {}

    def stopper():
        stop_result["r"] = mgr.stop(mid, mode="kill")

    th = threading.Thread(target=stopper, daemon=True)
    th.start()
    time.sleep(1.5)
    # 线性化边界（核心断言）：apply 事务仍开着 → stop 的旗标写被写锁
    # 推迟 → hard_stop 不得置位（旧实现在此处已是 True=反例）
    assert not sched.hard_stop.is_set(), (
        "hard_stop 在 apply 提交前置位——停止未与事务串行化（R2 复现）")
    assert th.is_alive(), "stop 未等 apply 提交（线性化点缺失）"
    transaction_release.set()
    th.join(timeout=30)
    assert not th.is_alive(), "stop 未返回"
    assert stop_result["r"].get("stopped") is True
    assert sched.hard_stop.is_set()
    # 撤销前已提交的 apply 合法保留（公开停止结果如实反映）
    assert len(repo.list_nodes(mid)) == n_before + 2
    rows = repo.list_split_proposals(mid)
    assert rows and rows[0]["status"] == "applied"
    run = mgr._runs[mid]
    run["thread"].join(timeout=60)
    assert not run["thread"].is_alive()
    n_final = len(repo.list_nodes(mid))
    time.sleep(0.5)
    assert len(repo.list_nodes(mid)) == n_final, "停止后不得再有新派发落图"
    repo.close()


def test_r2_replay_after_revoke_can_apply(tmp_path):
    """恢复重放：撤销期 gate 拒绝（库内旗标）；新代际清旗标后同一提案
    可重新规划应用（旧运行的撤销不约束新代际）。"""
    repo, graph, mid, parent = build_mission(tmp_path)
    aid, fp = stage_split(repo, graph, mid, parent, "sp-r2-replay")
    sched = Scheduler(repo, graph, AutoGrowBackend(),
                      auto_split=AutogrowConfig(enabled=True))
    spec = {"title": "重放子卷", "statement": BRIEF.format(body="r"),
            "check_cmd": CMD_OK, "declared_artifacts": ["deliverable.md"],
            "deps": [], "source_local_id": "c1"}
    from dag_agent.acceptance import compute_check_digest as _ccd
    spec["_frozen_digest"] = _ccd(check_cmd=CMD_OK, exam_files=())
    spec2 = dict(spec, title="重放子卷二", source_local_id="c2")
    # 撤销期：库内旗标使 gate 拒绝
    sched.request_stop("kill", reason="v47 replay")
    with pytest.raises(GraphError, match="运行权限已撤销"):
        graph.apply_auto_split(
            mid, parent, aid, "sp-r2-replay", [spec, spec2], fp,
            apply_gate=lambda: not sched._stop_revoked_db())
    assert repo.get_split_proposal("sp-r2-replay")["status"] == "queued"
    # 新代际：清旗标 → 同一提案可应用
    sched.clear_stop_flags()
    kids = graph.apply_auto_split(
        mid, parent, aid, "sp-r2-replay", [spec, spec2], fp,
        apply_gate=lambda: not sched._stop_revoked_db())
    assert len(kids) == 2
    assert repo.get_split_proposal("sp-r2-replay")["status"] == "applied"
    repo.close()


# ---------- R3 · 完整绑定契约 ----------


def _accept_upstream(repo, graph, mid, up) -> int:
    """上游节点走真实提交，产出 snapshot（含 manifest+files）。"""
    aid = graph.start_attempt(mid, up, backend="stub")
    wd = Path(repo.get_attempt(aid).workdir)
    (wd / "deliverable.md").write_text(
        "ORIGINAL upstream payload long enough", encoding="utf-8")
    result = graph.submit_attempt(mid, up, aid, artifact_paths=["deliverable.md"])
    assert result["verdict"] == "pass"
    snap = repo._conn.execute(
        "SELECT id FROM snapshots WHERE node_id=? AND valid=1"
        " ORDER BY id DESC LIMIT 1", (up,)).fetchone()
    return snap["id"]


def test_r3_binding_contract_tamper_rejected(tmp_path):
    """R3 反例（binding_manifest_drift）：同快照 id、同 manifest_hash、
    files_json 被清空 → 孩子认领必须拒绝（不按损坏清单播种零件）；
    manifest 篡改同理。恢复后正例认领成功且播种内容与父绑定一致。"""
    from dag_agent.scheduler import Scheduler as _S
    repo, graph, mid, root = build_mission(tmp_path)
    up = graph.add_node(mid, title="上游", statement=BRIEF.format(body="u"),
                        parent_id=root, check_cmd=CMD_OK,
                        declared_artifacts=("deliverable.md",))
    parent = graph.add_node(mid, title="父任务P",
                            statement=BRIEF.format(body="P"), parent_id=root,
                            deps=(up,), check_cmd=CMD_OK,
                            declared_artifacts=("deliverable.md",))
    sid = _accept_upstream(repo, graph, mid, up)
    pa = graph.start_attempt(mid, parent, backend="stub")
    binding = json.loads(repo.get_attempt(pa).input_binding_json or "[]")
    dep_b = next(b for b in binding if b.get("role") == "dep")
    assert dep_b["snapshot_id"] == sid
    orig_files_json = repo._conn.execute(
        "SELECT files_json FROM snapshots WHERE id=?", (sid,)).fetchone()["files_json"]
    aid_split, fp_split = stage_split(repo, graph, mid, parent, "sp-r3",
                                      input_binding_json=json.dumps(
                                          binding, ensure_ascii=False))
    from dag_agent.acceptance import compute_check_digest as _ccd

    def _fz(spec):
        spec = dict(spec)
        spec["_frozen_digest"] = _ccd(check_cmd=spec["check_cmd"],
                                      exam_files=())
        return spec

    kids = graph.apply_auto_split(
        mid, parent, aid_split, "sp-r3",
        [_fz({"title": "契约子卷一", "statement": BRIEF.format(body="a"),
              "check_cmd": CMD_OK, "declared_artifacts": ["deliverable.md"],
              "deps": [], "source_local_id": "c1"}),
         _fz({"title": "契约子卷二", "statement": BRIEF.format(body="b"),
              "check_cmd": CMD_OK, "declared_artifacts": ["deliverable.md"],
              "deps": [], "source_local_id": "c2"})],
        fp_split, source_binding_json=json.dumps(binding, ensure_ascii=False))
    ctr = repo._conn.execute(
        "SELECT manifest_hash, files_json FROM dep_bindings"
        " WHERE node_id=? AND depends_on=?", (kids[0], up)).fetchone()
    assert ctr is not None, "apply 必须持久完整绑定契约"
    assert json.loads(ctr["files_json"]) == dep_b["files"]
    # 篡改①：清空 files（同 id 同 manifest）→ 认领拒绝
    with repo.tx() as cur:
        cur.execute("UPDATE snapshots SET files_json='[]' WHERE id=?", (sid,))
    with pytest.raises(GraphError, match="契约不符"):
        graph.start_attempt(mid, kids[0], backend="stub")
    # 篡改②：manifest 换值 → 认领拒绝
    with repo.tx() as cur:
        cur.execute("UPDATE snapshots SET files_json=?, manifest_hash='x'"
                    " WHERE id=?", (orig_files_json, sid))
    with pytest.raises(GraphError, match="契约不符"):
        graph.start_attempt(mid, kids[0], backend="stub")
    # 正例：契约一致 → 认领成功，播种内容与父绑定核对（不只对 ID）
    with repo.tx() as cur:
        cur.execute("UPDATE snapshots SET manifest_hash=? WHERE id=?",
                    (dep_b["manifest_hash"], sid))
    ca = graph.start_attempt(mid, kids[0], backend="stub")
    cw = Path(repo.get_attempt(ca).workdir)
    sched = _S(repo, graph, AutoGrowBackend())
    sched._seed_bound(ca, cw)
    seeded = list(cw.rglob("deliverable.md"))
    assert seeded, "绑定输入必须播种"
    assert seeded[0].read_text(encoding="utf-8") \
        == "ORIGINAL upstream payload long enough", "播种内容须与上游一致"
    repo.close()


# ---------- R4 · 平台结算受理 ----------


class _SelfValidIncompleteBackend:
    """自报 receipt_status=valid 但缺核心/时序字段——平台结算判
    legacy_incomplete（Codex R4 反例形态）。"""

    name = "stub"

    def run(self, statement, workdir, check_cmd, title="", cancel_event=None):
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / ".dag").mkdir(exist_ok=True)
        (workdir / ".dag" / "proposal.json").write_text(
            make_proposal(pid="sp-r4-neg"), encoding="utf-8")
        return {"ok": True, "session_id": "r4-neg",
                "summary": "self-valid missing contract fields",
                "resources": {"receipt_status": "valid",
                              "cost": {"amount": 0.0, "basis": "reported"}}}


class _PlatformValidBackend:
    """完整契约回执（对照）：平台结算 valid → 提案正常受理激活。"""

    name = "stub"

    def run(self, statement, workdir, check_cmd, title="", cancel_event=None):
        from datetime import datetime, timezone
        iso = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / ".dag").mkdir(exist_ok=True)
        (workdir / ".dag" / "proposal.json").write_text(
            make_proposal(pid="sp-r4-pos"), encoding="utf-8")
        return {"ok": True, "session_id": "r4-pos", "summary": "ok",
                "resources": {"origin": "stub", "agent_id": "r4-pos",
                              "tokens": 0, "duration_ms": 1,
                              "cost_currency": "USD",
                              "executor_started_at": iso,
                              "executor_ended_at": iso,
                              "cost": {"amount": 0.0, "basis": "reported"},
                              "receipt_status": "valid"}}


def _run_one_work(repo, graph, mid, root, backend):
    sched = Scheduler(repo, graph, backend, max_concurrency=1,
                      auto_split=AutogrowConfig(enabled=True))
    aid = graph.start_attempt(mid, root, backend="stub")
    sched._work(mid, root, aid, False)
    return sched, aid


def test_r4_self_valid_incomplete_no_growth(tmp_path):
    """自报 valid 而平台 legacy_incomplete → 提案不受理不激活；
    资源证据照常结算保留；attempt 不转 split。"""
    repo, graph, mid, root = build_simple46(tmp_path)
    _run_one_work(repo, graph, mid, root, _SelfValidIncompleteBackend())
    assert not repo.list_split_proposals(mid), "平台未认可会话不得激活提案"
    types = {e.type for e in repo.events_of(mid)}
    assert "split_proposed" not in types
    assert "split_activated" not in types
    att = repo.get_attempt(_attempts_of(repo, root)[0])
    assert att.status != "split", "未受理会话不得转 split 终态"
    rj = json.loads(att.resources_json or "{}")
    assert rj.get("settled_valid") is not True  # 平台未打完整结算标
    repo.close()


def test_r4_platform_valid_activates(tmp_path):
    """对照：平台结算 contract_status=valid 的完整回执 → 提案受理激活
    （attempt→split、父→waiting_split），正常链路不受误伤。"""
    repo, graph, mid, root = build_simple46(tmp_path)
    _run_one_work(repo, graph, mid, root, _PlatformValidBackend())
    rows = repo.list_split_proposals(mid)
    assert rows, "平台认可会话应受理提案"
    assert rows[0]["status"] == "queued"
    assert repo.get_attempt(_attempts_of(repo, root)[0]).status == "split"
    assert repo.get_node(root).status == "waiting_split"
    types = {e.type for e in repo.events_of(mid)}
    assert "split_activated" in types
    repo.close()


# ---------- 共用小工具 ----------


def build_simple46(tmp_path):
    from test_v46_codex_review_fixes import build_simple
    return build_simple(tmp_path)


def _attempts_of(repo, nid):
    return [r["id"] for r in repo._conn.execute(
        "SELECT id FROM attempts WHERE node_id=?", (nid,)).fetchall()]
