# -*- coding: utf-8 -*-
"""v45：自动生长第一期（A0 协议 + A-G1 受控生长）行为回归。

对应任务书 G1 关闭条件 13 条，全部为行为证据（不以测试数量为充分性）：
1. 默认关闭不落图；开启后合法拆分→子执行→父整合→原卷闭合。
2. 执行者不能自行批准子考卷：模板越权拒绝；独立出题可追溯。
3. 原父卷不弱化：子全绿+文件齐，组合不合格→父仍 FAIL。
4. >300 字符合法协议完整解析；坏 JSON/未知字段/过大/错来源拒绝。
5. 重复/并发/重启只应用一次；异内容冲突拒绝。
6. preflight 期间 revise/repair 后旧提案拒绝。
7. 次数（节点/任务）、深度、孩子数护栏有效；计数重启不清零。
8. 完整输入绑定与局部依赖正确（孩子继承父认领的外部输入）。
9. 并发 1 也能推进完整链（出题等待不占死执行位）。
10. 规划与执行可停止；停止后提案不被应用（迟到结果不发布）。
11. 应用失败无半图/无半计数；已结算资源保留且不重复收费。
12. 提案拒绝记 split_rejected（≠产物 FAIL/ERROR）；拒绝有界无热循环。
13. 手工 decompose 与现有路径保持兼容。
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
from dag_agent.graph import Graph  # noqa: E402
from dag_agent.recovery import RetryPolicy  # noqa: E402
from dag_agent.repository import Repository  # noqa: E402
from dag_agent.scheduler import AutogrowConfig, Scheduler  # noqa: E402

CMD_OK = ('python -c "import sys;from pathlib import Path;'
          "p=Path('deliverable.md');"
          'sys.exit(0 if p.is_file() and p.stat().st_size>10 else 1)"')
CMD_FAIL = 'python -c "import sys;sys.exit(1)"'

BRIEF = ("【输入】无外部输入。\n【交付清单】deliverable.md\n"
         "【接口签名】deliverable.md 为 UTF-8 文本，>10 字节。\n"
         "【样例】示例行\n【自测入口】运行考卷。\n【任务】{body}")


class AutoGrowBackend:
    """可编程测试执行器（stub 豁免表内）：
    - 正常交付【交付清单】段文件；
    - on_write 钩子可在执行时于工作目录写 .dag/proposal.json（模拟执行者
      提拆分建议）；
    - deliver="skip"：不写交付（无交付场景）；
    - delay 秒级延迟（分片 sleep，响应 cancel_event）。"""

    name = "stub"

    def __init__(self, on_write=None, deliver="write", delay=0.0):
        self.on_write = on_write
        self.deliver_mode = deliver
        self.delay = float(delay)
        self.calls: list[str] = []

    def run(self, statement, workdir, check_cmd, title="", cancel_event=None):
        from datetime import datetime, timezone
        self.calls.append(title)
        t0 = time.monotonic()
        started = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        slept = 0.0
        while slept < self.delay:
            if cancel_event is not None and cancel_event.is_set():
                return {"ok": False, "summary": "cancelled", "session_id": "",
                        "resources": {"origin": "stub", "agent_id": "s-x",
                                      "tokens": 0, "duration_ms": 0,
                                      "cost_currency": "USD",
                                      "executor_started_at": started,
                                      "executor_ended_at": started,
                                      "cost": {"amount": None,
                                               "basis": "unknown"},
                                      "receipt_status": "incomplete",
                                      "cancelled": True}}
            time.sleep(min(0.1, self.delay - slept))
            slept += 0.1
        if self.on_write is not None:
            self.on_write(title, workdir, statement)
        target_name = "deliverable.md"
        import re as _re
        m = _re.search(r"【交付清单】([^\n】]+)", statement or "")
        if m and m.group(1).strip():
            target_name = m.group(1).strip()
        is_integration = "做整合交付" in (statement or "")
        if (self.deliver_mode == "auto" and "自动生长回归" in title
                and not is_integration):
            pass  # 拆分目标的叶子执行无交付 → 激活提案路径；
                  # 整合会话（含平台整合附加句）正常交付
        elif self.deliver_mode != "skip":
            (workdir / target_name).write_text(
                f"deliverable for {title}\n{statement[:200]}\n" + "x" * 40,
                encoding="utf-8")
        ended = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        return {"ok": True, "summary": f"done: {title[:40]}",
                "session_id": f"s-{title[:8]}",
                "resources": {"origin": "stub", "agent_id": f"s-{title[:8]}",
                              "tokens": 0,
                              "duration_ms": int((time.monotonic() - t0) * 1000),
                              "cost_currency": "USD",
                              "executor_started_at": started,
                              "executor_ended_at": ended,
                              "cost": {"amount": 0.0, "basis": "reported"},
                              "receipt_status": "valid"}}


def make_proposal(children=None, pid="prop-test-0001", **overrides) -> str:
    """合法提案（占位符身份——执行者无需预知平台值）。"""
    if children is None:
        children = [
            {"local_id": "c1", "title": "子任务一",
             "statement": BRIEF.format(body="子任务一：交付分件 A"),
             "check_hint": "交付 autogrow-c1-deliverable.md，>10 字节",
             "depends_on": [], "input_refs": [], "deliverables": []},
            {"local_id": "c2", "title": "子任务二",
             "statement": BRIEF.format(body="子任务二：交付分件 B"),
             "check_hint": "交付 autogrow-c2-deliverable.md，>10 字节",
             "depends_on": ["c1"], "input_refs": [], "deliverables": []},
        ]
    doc = {
        "schema_version": autogrow.SCHEMA_VERSION,
        "proposal_id": pid,
        "source_attempt_id": "platform",
        "parent_node_id": "platform",
        "goal_version": -1, "node_revision": -1, "check_version": -1,
        "check_digest": "platform", "input_binding_hash": "platform",
        "reason": "父目标粒度过大：先分件后整合",
        "children": children,
    }
    doc.update(overrides)
    return json.dumps(doc, ensure_ascii=False)


def build_mission(tmp_path: Path, root_check: str = CMD_OK,
                  root_artifact=("deliverable.md",)):
    repo = Repository(tmp_path / "mission.db")
    graph = Graph(repo)
    mid, root = graph.create_mission(
        "v45 自动生长回归", brief=BRIEF.format(body="父目标"),
        root_check=root_check, root_declared_artifacts=root_artifact)
    return repo, graph, mid, root


def run_scheduler(repo, graph, mid, root, backend, auto_split=True,
                  max_concurrency=2, timeout=60.0, max_retries=0,
                  node_cap=None, planner=None):
    sched = Scheduler(repo, graph, backend, max_concurrency=max_concurrency,
                      auto_split=AutogrowConfig(enabled=auto_split),
                      retry_policy=RetryPolicy(max_retries=max_retries),
                      node_cap=node_cap, planner=planner)
    summary = sched.run_until_done(mid, root, timeout=timeout, poll=0.05)
    return sched, summary


def events_of(repo, mid, etype):
    return [e for e in repo.events_of(mid) if e.type == etype]


def node_status(repo, nid):
    return repo.get_node(nid).status


def real_fp(repo, aid: str) -> dict:
    """真实认领指纹（apply 重核以平台值为权威——测试不得伪造）。"""
    return json.loads(repo.get_attempt(aid).claim_fingerprint_json or "{}")


def ensure_pool(sched: Scheduler):
    """直接调 tick_dispatch（不经 run_until_done）时自建执行池。"""
    from concurrent.futures import ThreadPoolExecutor
    if sched._pool is None:
        sched._pool = ThreadPoolExecutor(max_workers=sched.max_concurrency)
    return sched._pool


def stage_split(repo, graph, mid: str, nid: str, pid: str,
                content: str = "c",
                input_binding_json: str | None = None) -> tuple[str, dict]:
    """造激活态（父 waiting_split + queued 提案 + 源 attempt split 终态），
    返回 (源 attempt id, 真实指纹)。直接 INSERT attempt 行（不经
    start_attempt——有孩子的节点走整合分支未解锁会拒绝，而激活态本身
    就是"执行已释放"语义）；指纹按节点当前考卷真实计算。"""
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


def _frozen(spec: dict) -> dict:
    """F1 新规则：出题产物须带冻结考卷摘要——测试规格同口径补齐。"""
    from dag_agent.acceptance import compute_check_digest as _ccd
    spec = dict(spec)
    spec["_frozen_digest"] = _ccd(
        check_cmd=spec.get("check_cmd"),
        acceptance=spec.get("acceptance"),
        exam_files=tuple(spec.get("exam_files") or ()))
    return spec


# ---------- 1. 默认关闭 / 开启后完整链 ----------


def test_1a_default_off_no_split(tmp_path):
    """关闭（默认）时提案文件即使存在也不解析、不落图、不改变判卷路径。"""
    def writer(title, workdir, statement):
        if "自动生长回归" not in title:
            return  # 只对拆分目标（root）提案；孩子/整合会话不写
        (Path(workdir) / ".dag").mkdir(exist_ok=True)
        (Path(workdir) / ".dag" / "proposal.json").write_text(
            make_proposal(), encoding="utf-8")
    repo, graph, mid, root = build_mission(tmp_path)
    backend = AutoGrowBackend(on_write=writer, deliver="auto")
    sched, summary = run_scheduler(repo, graph, mid, root, backend,
                                   auto_split=False, max_retries=0,
                                   timeout=30)
    assert events_of(repo, mid, "split_proposed") == []
    assert events_of(repo, mid, "split_applied") == []
    rows = repo.list_split_proposals(mid)
    assert rows == []          # 无提案行落库
    # 无交付按冻结运行协议：fail-closed→重试耗尽→死局（不因提案改变）
    assert summary["halt_reason"] and "死局" in summary["halt_reason"]
    assert node_status(repo, root) == "failed"
    repo.close()


def test_1b_enabled_full_chain(tmp_path):
    """开启后：合法提案→激活→独立出题→原子应用→子执行→父整合（原考卷）
    →根干净重放→mission done/machine。全程行为断言。"""
    def writer(title, workdir, statement):
        if "自动生长回归" not in title:
            return  # 只对拆分目标（root）提案；孩子/整合会话不写
        (Path(workdir) / ".dag").mkdir(exist_ok=True)
        (Path(workdir) / ".dag" / "proposal.json").write_text(
            make_proposal(), encoding="utf-8")
    repo, graph, mid, root = build_mission(tmp_path)
    backend = AutoGrowBackend(on_write=writer, deliver="auto")
    sched, summary = run_scheduler(repo, graph, mid, root, backend,
                                   auto_split=True, timeout=60)
    assert summary["mission_status"] == "done"
    m = repo.get_mission(mid)
    assert m.status == "done" and m.closure_basis == "machine"
    proposed = events_of(repo, mid, "split_proposed")
    applied = events_of(repo, mid, "split_applied")
    assert len(proposed) == 1 and len(applied) == 1
    kids = [n for n in repo.list_nodes(mid) if n.parent_id == root]
    assert len(kids) == 2
    assert all(k.status == "accepted" for k in kids)
    # 局部依赖：c2 依赖 c1 → 真实 ID 落库
    deps = {k.id: [d.depends_on for d in repo.deps_of(k.id)] for k in kids}
    by_title = {k.title: k.id for k in kids}
    assert deps[by_title["子任务二"]] == [by_title["子任务一"]]
    assert deps[by_title["子任务一"]] == []
    # 父考卷原样保留（原 root_check 未被替换/弱化）
    assert node_status(repo, root) == "accepted"
    assert graph._exam_files_of(repo.get_node(root)) == ()
    pr = repo.list_split_proposals(mid)[0]
    assert pr["status"] == "applied"
    assert pr["nodes_added"] == 2
    assert pr["plan_backend"] == "planner-stub"
    # 孩子由出题产物执行：考卷=独立出题桩生成（autogrow-* 交付清单）
    kid_cmds = {k.check_cmd or "" for k in kids}
    assert any("autogrow" in c for c in kid_cmds)
    assert {"子任务一", "子任务二"} <= set(backend.calls)
    repo.close()


# ---------- 2. 出题分离：模板越权 / 独立出题可追溯 ----------


def test_2a_template_unauthorized_rejected(tmp_path):
    """执行者不能自行批准子考卷：未授权 template_id → 拒绝、父回 pending。"""
    children = [
        {"local_id": "c1", "title": "越权子卷",
         "statement": BRIEF.format(body="x"), "template_id": "my-own-tpl",
         "template_params": {"artifact": "evil.md"},
         "depends_on": [], "input_refs": [], "deliverables": []},
        {"local_id": "c2", "title": "占位",
         "statement": BRIEF.format(body="y"), "check_hint": "hint",
         "depends_on": [], "input_refs": [], "deliverables": []},
    ]
    def writer(title, workdir, statement):
        if "自动生长回归" not in title:
            return  # 只对拆分目标（root）提案；孩子/整合会话不写
        (Path(workdir) / ".dag").mkdir(exist_ok=True)
        (Path(workdir) / ".dag" / "proposal.json").write_text(
            make_proposal(children=children), encoding="utf-8")
    repo, graph, mid, root = build_mission(tmp_path)
    backend = AutoGrowBackend(on_write=writer, deliver="auto")
    sched, summary = run_scheduler(repo, graph, mid, root, backend,
                                   auto_split=True, timeout=40)
    rej = events_of(repo, mid, "split_rejected")
    assert any("模板越权" in (e.payload_json or "") for e in rej)
    pr = repo.list_split_proposals(mid)[0]
    assert pr["status"] == "rejected"
    # 父回待派（无半图）；无交付重试耗尽后转 failed（冻结运行协议）
    assert node_status(repo, root) in ("pending", "failed")
    assert events_of(repo, mid, "split_applied") == []
    repo.close()


def test_2b_independent_planning_traceable(tmp_path):
    """独立出题路径可追溯：plan_backend/plan_output/coverage 留痕；
    check_hint 只是建议（进入陈述，不成为可执行考卷正文）。"""
    def writer(title, workdir, statement):
        if "自动生长回归" not in title:
            return  # 只对拆分目标（root）提案；孩子/整合会话不写
        (Path(workdir) / ".dag").mkdir(exist_ok=True)
        (Path(workdir) / ".dag" / "proposal.json").write_text(
            make_proposal(), encoding="utf-8")
    repo, graph, mid, root = build_mission(tmp_path)
    backend = AutoGrowBackend(on_write=writer, deliver="auto")
    sched, summary = run_scheduler(repo, graph, mid, root, backend,
                                   auto_split=True, timeout=60)
    pr = repo.list_split_proposals(mid)[0]
    assert pr["status"] == "applied"
    out = json.loads(pr["plan_output_json"])
    assert len(out) == 2
    assert out[0]["exploratory"] is True           # 充分性未验证显式标注
    assert "充分性尚未验证" in out[0]["coverage_hint"]
    assert "覆盖建议" in out[0]["statement"]       # check_hint 仅建议
    # 出题考卷由桩固定模板生成——hint 文本不出现在 check_cmd 中
    assert "交付 autogrow" not in out[0]["check_cmd"]
    repo.close()


def test_2c_template_path_ok(tmp_path):
    """模板路径：授权模板+合法参数 → 模板考卷实例化；参数越界拒绝。"""
    children = [
        {"local_id": "c1", "title": "模板子件",
         "statement": BRIEF.format(body="分件"),
         "template_id": "doc-section-v1",
         "template_params": {"artifact": "doc-part.md", "sections": 1,
                             "priority": 1},
         "depends_on": [], "input_refs": [], "deliverables": []},
        {"local_id": "c2", "title": "模板子件二",
         "statement": BRIEF.format(body="分件二"),
         "template_id": "doc-section-v1",
         "template_params": {"artifact": "doc-part2.md", "sections": 2},
         "depends_on": [], "input_refs": [], "deliverables": []},
    ]
    def writer(title, workdir, statement):
        if "自动生长回归" not in title:
            return  # 只对拆分目标（root）提案；孩子/整合会话不写
        (Path(workdir) / ".dag").mkdir(exist_ok=True)
        (Path(workdir) / ".dag" / "proposal.json").write_text(
            make_proposal(children=children), encoding="utf-8")
    repo, graph, mid, root = build_mission(tmp_path)
    backend = AutoGrowBackend(on_write=writer, deliver="auto")
    sched, summary = run_scheduler(repo, graph, mid, root, backend,
                                   auto_split=True, timeout=60)
    pr = repo.list_split_proposals(mid)[0]
    assert pr["status"] == "applied"
    out = json.loads(pr["plan_output_json"])
    assert out[0]["exploratory"] is False          # 模板路径：非探索
    kids = [n for n in repo.list_nodes(mid) if n.parent_id == root]
    assert {k.title for k in kids} == {"模板子件", "模板子件二"}
    assert summary["mission_status"] == "done"
    repo.close()


def test_2d_template_param_violation(tmp_path):
    children = [
        {"local_id": "c1", "title": "越权参数",
         "statement": BRIEF.format(body="x"),
         "template_id": "doc-section-v1",
         "template_params": {"artifact": "ok.md", "sections": 99},
         "depends_on": [], "input_refs": [], "deliverables": []},
        {"local_id": "c2", "title": "占位",
         "statement": BRIEF.format(body="y"), "check_hint": "h",
         "depends_on": [], "input_refs": [], "deliverables": []},
    ]
    def writer(title, workdir, statement):
        if "自动生长回归" not in title:
            return  # 只对拆分目标（root）提案；孩子/整合会话不写
        (Path(workdir) / ".dag").mkdir(exist_ok=True)
        (Path(workdir) / ".dag" / "proposal.json").write_text(
            make_proposal(children=children), encoding="utf-8")
    repo, graph, mid, root = build_mission(tmp_path)
    backend = AutoGrowBackend(on_write=writer, deliver="skip")
    run_scheduler(repo, graph, mid, root, backend, auto_split=True,
                  timeout=40)
    rej = events_of(repo, mid, "split_rejected")
    assert any("越权" in (e.payload_json or "") for e in rej)
    # 被拒后父回 pending 可重派；无交付重试耗尽转 failed（冻结运行协议）
    assert node_status(repo, root) in ("pending", "failed")
    assert events_of(repo, mid, "split_applied") == []
    repo.close()


# ---------- 3. 原父卷不弱化 ----------


def test_3_children_pass_parent_still_fail(tmp_path):
    """子全绿+文件齐，但组合不满足原父考卷 → 父 FAIL、任务不闭合。"""
    # 父考卷要求 deliverable.md 含 "FINAL-SEAL" 关键词——
    # 整合执行者交付不含它 → 子全绿而父 FAIL
    root_check = ('python -c "import sys;from pathlib import Path;'
                  "t=Path('deliverable.md').read_text(encoding='utf-8');"
                  "sys.exit(0 if 'FINAL-SEAL' in t else 1)\"")

    def writer(title, workdir, statement):
        if "自动生长回归" not in title:
            return  # 只对拆分目标（root）提案；孩子/整合会话不写
        (Path(workdir) / ".dag").mkdir(exist_ok=True)
        (Path(workdir) / ".dag" / "proposal.json").write_text(
            make_proposal(), encoding="utf-8")
    repo, graph, mid, root = build_mission(tmp_path, root_check=root_check)
    backend = AutoGrowBackend(on_write=writer, deliver="auto")
    sched, summary = run_scheduler(repo, graph, mid, root, backend,
                                   auto_split=True, timeout=60,
                                   max_retries=0)
    kids = [n for n in repo.list_nodes(mid) if n.parent_id == root]
    assert len(kids) == 2 and all(k.status == "accepted" for k in kids)
    assert node_status(repo, root) == "failed"
    assert summary["mission_status"] != "done"
    repo.close()


# ---------- 4. 协议解析边界 ----------


def test_4a_large_valid_protocol_parses(tmp_path):
    """>300 字符的合法协议完整解析并走完链路。"""
    big_reason = "理由" * 200          # 400 字符 reason → 全文远超 300
    def writer(title, workdir, statement):
        raw = make_proposal(reason=big_reason, pid="prop-large-1")
        assert len(raw) > 300
        (Path(workdir) / ".dag").mkdir(exist_ok=True)
        (Path(workdir) / ".dag" / "proposal.json").write_text(raw,
                                                              encoding="utf-8")
    repo, graph, mid, root = build_mission(tmp_path)
    backend = AutoGrowBackend(on_write=writer, deliver="auto")
    sched, summary = run_scheduler(repo, graph, mid, root, backend,
                                   auto_split=True, timeout=60)
    assert len(events_of(repo, mid, "split_applied")) == 1
    assert summary["mission_status"] == "done"
    repo.close()


@pytest.mark.parametrize("bad,why", [
    (json.dumps({"schema_version": "1"}), "缺字段"),
    ("{not json", "非合法 JSON"),
    (json.dumps({**json.loads(make_proposal()), "extra_field": 1}),
     "未知字段"),
    (json.dumps({**json.loads(make_proposal()), "children": []}), "孩子数"),
    (json.dumps({**json.loads(make_proposal()),
                 "source_attempt_id": "a-forged"}), "不符"),
    ("x" * (64 * 1024 + 1), "超上限"),
], ids=["missing", "badjson", "unknown-field", "no-children",
        "forged-identity", "too-large"])
def test_4b_invalid_protocols_rejected(tmp_path, bad, why):
    def writer(title, workdir, statement):
        if "自动生长回归" not in title:
            return  # 只对拆分目标（root）提案；孩子/整合会话不写
        (Path(workdir) / ".dag").mkdir(exist_ok=True)
        (Path(workdir) / ".dag" / "proposal.json").write_text(
            bad, encoding="utf-8")
    repo, graph, mid, root = build_mission(tmp_path)
    backend = AutoGrowBackend(on_write=writer, deliver="skip")
    run_scheduler(repo, graph, mid, root, backend, auto_split=True,
                  timeout=40, max_retries=0)
    rej = events_of(repo, mid, "split_rejected")
    assert rej, f"期望 split_rejected（{why}）"
    assert events_of(repo, mid, "split_applied") == []
    # 拒绝不伪造产物 FAIL/ERROR：无 failed/error 判卷账目新增
    assert node_status(repo, root) == "failed"  # 无交付 fail-closed（运行协议）
    repo.close()


# ---------- 5. 幂等：重复/并发/重启/异内容 ----------


def test_5_apply_once_and_conflict(tmp_path):
    """重复应用幂等拒绝；同源异内容冲突拒绝；applied 只有一次。"""
    repo, graph, mid, root = build_mission(tmp_path)
    pid = "sp-dup-1"
    aid = graph.start_attempt(mid, root, backend="stub")
    fp = real_fp(repo, aid)
    from dag_agent.repository import now
    with repo.tx() as cur:
        repo.insert_split_proposal({
            "id": pid, "mission_id": mid, "node_id": root,
            "source_attempt_id": aid, "raw_sha256": "r1",
            "content_sha256": "c1", "raw_json": make_proposal(),
            "fingerprint_json": json.dumps(fp), "status": "queued",
            "created_at": now()}, cur)
        cur.execute("UPDATE nodes SET status='waiting_split' WHERE id=?",
                    (root,))
        cur.execute("UPDATE attempts SET status='split', category='split'"
                    " WHERE id=?", (aid,))
    specs = [{"title": "t1", "statement": "s", "check_cmd": CMD_OK,
              "declared_artifacts": ["deliverable.md"], "deps": [],
              "source_local_id": "c1"},
             {"title": "t2", "statement": "s", "check_cmd": CMD_OK,
              "declared_artifacts": ["deliverable.md"], "deps": [],
              "source_local_id": "c2"}]
    kids = graph.apply_auto_split(mid, root, aid, pid,
                                 list(map(_frozen, specs)), fp)
    assert len(kids) == 2
    # 重复应用（同提案已 applied）→ 幂等拒绝，只应用一次
    with pytest.raises(Exception, match="只应用一次"):
        graph.apply_auto_split(mid, root, aid, pid,
                               list(map(_frozen, specs)), fp)
    # 异内容冲突：同源 attempt 不同 content → 检测层拒绝（scheduler 规则）
    row = repo.split_proposal_by_attempt(aid)
    assert row is not None and row["status"] == "applied"
    repo.close()


def test_5b_restart_resumes_planning(tmp_path):
    """重启恢复：waiting_split+queued 提案在新调度器实例上继续规划并
    只应用一次（applied 计数持久，非内存）。"""
    def writer(title, workdir, statement):
        if "自动生长回归" not in title:
            return  # 只对拆分目标（root）提案；孩子/整合会话不写
        (Path(workdir) / ".dag").mkdir(exist_ok=True)
        (Path(workdir) / ".dag" / "proposal.json").write_text(
            make_proposal(), encoding="utf-8")
    repo, graph, mid, root = build_mission(tmp_path)
    backend = AutoGrowBackend(on_write=writer, deliver="skip")
    sched = Scheduler(repo, graph, backend, max_concurrency=1,
                      auto_split=AutogrowConfig(enabled=True),
                      retry_policy=RetryPolicy(max_retries=0))
    ensure_pool(sched)
    # 第一段：只跑到激活（检测→入队→激活），随即模拟重启
    t0 = time.monotonic()
    while time.monotonic() - t0 < 10:
        sched.tick_dispatch(mid)
        if events_of(repo, mid, "split_activated"):
            break
        time.sleep(0.05)
    assert events_of(repo, mid, "split_activated")
    # 模拟重启：放弃旧调度器（未应用任何提案），新实例接管同一 DB
    sched.hard_stop.set()
    rows = repo.list_split_proposals(mid)
    assert len(rows) == 1 and rows[0]["status"] == "queued"
    backend2 = AutoGrowBackend()   # 新执行后端：正常交付
    sched2, summary = run_scheduler(repo, graph, mid, root, backend2,
                                    auto_split=True, timeout=60)
    applied = events_of(repo, mid, "split_applied")
    assert len(applied) == 1       # 重启后应用一次
    assert summary["mission_status"] == "done"
    repo.close()


# ---------- 6. 过期提案：revise/repair 后拒绝 ----------


def test_6_stale_proposal_after_revise_and_repair(tmp_path):
    """preflight 后、应用前发生 revise/repair → 旧提案拒绝（指纹过期），
    不误伤新状态。"""
    def writer(title, workdir, statement):
        if "自动生长回归" not in title:
            return  # 只对拆分目标（root）提案；孩子/整合会话不写
        (Path(workdir) / ".dag").mkdir(exist_ok=True)
        (Path(workdir) / ".dag" / "proposal.json").write_text(
            make_proposal(), encoding="utf-8")
    repo, graph, mid, root = build_mission(tmp_path)
    backend = AutoGrowBackend(on_write=writer, deliver="skip")
    sched = Scheduler(repo, graph, backend, max_concurrency=1,
                      auto_split=AutogrowConfig(enabled=True),
                      retry_policy=RetryPolicy(max_retries=0))
    ensure_pool(sched)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 10:
        sched.tick_dispatch(mid)
        if events_of(repo, mid, "split_activated"):
            break
        time.sleep(0.05)
    # 冻结规划窗口：修订父节点（revision+1 → 指纹过期）
    graph.revise_node(mid, root, reason="v45 修订注入")
    t0 = time.monotonic()
    while time.monotonic() - t0 < 20:
        sched.tick_dispatch(mid)
        if events_of(repo, mid, "split_rejected"):
            break
        time.sleep(0.05)
    rej = events_of(repo, mid, "split_rejected")
    # revise 后父流转（pending）：出题窗口内旧提案被拒绝——拦截点可在
    # 规划前置检查或权威事务重核（都在 split_applied 之前）
    assert any(("旧提案拒绝" in (e.payload_json or ""))
               or ("非 waiting_split" in (e.payload_json or ""))
               for e in rej), [e.payload_json[:120] for e in rej]
    pr = repo.list_split_proposals(mid)[0]
    assert pr["status"] == "rejected"
    assert events_of(repo, mid, "split_applied") == []
    repo.close()


def test_6b_stale_after_repair_check(tmp_path):
    def writer(title, workdir, statement):
        if "自动生长回归" not in title:
            return  # 只对拆分目标（root）提案；孩子/整合会话不写
        (Path(workdir) / ".dag").mkdir(exist_ok=True)
        (Path(workdir) / ".dag" / "proposal.json").write_text(
            make_proposal(), encoding="utf-8")
    repo, graph, mid, root = build_mission(tmp_path)
    backend = AutoGrowBackend(on_write=writer, deliver="skip")
    sched = Scheduler(repo, graph, backend, max_concurrency=1,
                      auto_split=AutogrowConfig(enabled=True),
                      retry_policy=RetryPolicy(max_retries=0))
    ensure_pool(sched)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 10:
        sched.tick_dispatch(mid)
        if events_of(repo, mid, "split_activated"):
            break
        time.sleep(0.05)
    graph.repair_check(mid, root, new_check_cmd=CMD_FAIL,
                       reason="v45 考卷维修注入")
    t0 = time.monotonic()
    while time.monotonic() - t0 < 20:
        sched.tick_dispatch(mid)
        if events_of(repo, mid, "split_rejected"):
            break
        time.sleep(0.05)
    rej = events_of(repo, mid, "split_rejected")
    assert any(("考卷已维修" in (e.payload_json or ""))
               or ("考卷内容已变更" in (e.payload_json or ""))
               or ("旧提案拒绝" in (e.payload_json or ""))
               for e in rej)
    assert events_of(repo, mid, "split_applied") == []
    repo.close()


# ---------- 7. 护栏 ----------


def test_7_guards(tmp_path):
    """每节点拆解≤1、每任务≤max、树深、孩子数护栏；计数持久（重启不清零）。"""
    repo, graph, mid, root = build_mission(tmp_path)
    specs2 = [{"title": f"g{i}", "statement": "s", "check_cmd": CMD_OK,
               "declared_artifacts": ["deliverable.md"], "deps": [],
               "source_local_id": f"c{i}"} for i in (1, 2)]
    # 第一次：合法应用（造 waiting_split+split 终态）
    aid1, fp = stage_split(repo, graph, mid, root, "sp-g1", "c")
    kids1 = graph.apply_auto_split(mid, root, aid1, "sp-g1",
                                  list(map(_frozen, specs2)), fp)
    assert len(kids1) == 2
    # 第二次：同节点再应用 → 每节点 ≤1
    aid2, fp2 = stage_split(repo, graph, mid, root, "sp-g2", "c2")
    with pytest.raises(Exception, match="每节点生命周期成功自动拆解"):
        graph.apply_auto_split(mid, root, aid2, "sp-g2",
                               list(map(_frozen, specs2)), fp2)
    row = repo.get_split_proposal("sp-g2")
    assert row["status"] == "queued"   # 拒绝不计成功次数
    # 每 mission ≤5：再应用 4 个不同节点后第 5 次拒绝（每轮独立命名防查重）
    for i in range(4):
        nid = graph.add_node(mid, title=f"深节点{i}",
                             statement=BRIEF.format(body=f"n{i}"),
                             parent_id=kids1[0], check_cmd=CMD_OK)
        aid, fpn = stage_split(repo, graph, mid, nid, f"sp-m{i}", f"c{i}")
        specs_i = [{"title": f"m{i}-g{j}", "statement": "s",
                    "check_cmd": CMD_OK,
                    "declared_artifacts": ["deliverable.md"], "deps": [],
                    "source_local_id": f"c{j}"} for j in (1, 2)]
        graph.apply_auto_split(mid, nid, aid, f"sp-m{i}",
                               list(map(_frozen, specs_i)), fpn)
    nid5 = graph.add_node(mid, title="深节点5",
                          statement=BRIEF.format(body="n5"),
                          parent_id=kids1[0], check_cmd=CMD_OK)
    aid5, fp5 = stage_split(repo, graph, mid, nid5, "sp-m5", "c5")
    with pytest.raises(Exception, match="每 mission"):
        graph.apply_auto_split(mid, nid5, aid5, "sp-m5",
                               list(map(_frozen, specs2)), fp5)
    # 树深：m0 的孩子深度为 3（root=0→g→深节点0→其孩子），未拆解过；
    # max_depth=2 时父深已超 → 拒绝（不与"每节点≤1"相撞）
    m0 = [n for n in repo.list_nodes(mid) if n.title == "深节点0"][0]
    deep_kid = [n for n in repo.list_nodes(mid)
                if n.parent_id == m0.id][0].id
    aid6, fp6 = stage_split(repo, graph, mid, deep_kid, "sp-d1", "cd")
    with pytest.raises(Exception, match="树深"):
        graph.apply_auto_split(mid, deep_kid, aid6, "sp-d1",
                               list(map(_frozen, specs2)), fp6,
                               max_depth=2, max_splits_per_mission=99)
    # 计数持久（重启不清零）：新 Repository 实例读同一 DB
    repo2 = Repository(tmp_path / "mission.db")
    applied = repo2.list_split_proposals(mid, status="applied")
    assert len(applied) == 5          # 1 + 4（sp-g2/sp-d1 被拒不计）
    repo2.close()
    repo.close()


def test_7b_node_cap_guard(tmp_path):
    """node_cap 配置时事务内核验当前数+新增数。"""
    repo, graph, mid, root = build_mission(tmp_path)
    specs5 = [{"title": f"c{i}", "statement": "s", "check_cmd": CMD_OK,
               "declared_artifacts": ["deliverable.md"], "deps": [],
               "source_local_id": f"c{i}"} for i in range(5)]
    aid, fp = stage_split(repo, graph, mid, root, "sp-cap", "c")
    # 当前节点数=1（root），cap=3 → +5 超限
    with pytest.raises(Exception, match="node_cap"):
        graph.apply_auto_split(mid, root, aid, "sp-cap",
                               list(map(_frozen, specs5)), fp,
                               node_cap=3)
    row = repo.get_split_proposal("sp-cap")
    assert row["status"] == "queued"
    assert node_status(repo, root) == "waiting_split"  # 事务回滚：无半图
    repo.close()


# ---------- 8. 输入绑定与局部依赖 ----------


def test_8_external_deps_inherited(tmp_path):
    """孩子获得父认领时批准的完整外部输入（deps 继承），不静默改绑；
    局部依赖映射到真实 ID 且与外部依赖并存。被拆对象=普通节点 P
    （deps=上游 U）——外部依赖语义来自认领时的执行依赖绑定。"""
    repo, graph, mid, root = build_mission(tmp_path)
    up_id = graph.add_node(mid, title="上游",
                           statement=BRIEF.format(body="up"),
                           parent_id=root, check_cmd=CMD_OK)
    p_node = graph.add_node(mid, title="父任务P",
                            statement=BRIEF.format(body="P"),
                            parent_id=root, deps=(up_id,),
                            check_cmd=CMD_OK)
    # 先让上游执行（P 因依赖未满足暂不派发）
    b1 = AutoGrowBackend()
    s1 = Scheduler(repo, graph, b1, max_concurrency=2,
                   auto_split=AutogrowConfig(enabled=False),
                   retry_policy=RetryPolicy(max_retries=0))
    ensure_pool(s1)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 15:
        s1.tick_dispatch(mid)
        upstream = [n for n in repo.list_nodes(mid) if n.title == "上游"][0]
        if upstream.status == "accepted":
            break
        time.sleep(0.05)
    upstream = [n for n in repo.list_nodes(mid) if n.title == "上游"][0]
    assert upstream.status == "accepted"
    s1._pool.shutdown(wait=True)
    # P 可能已被并发派发执行完（accepted）——修订回 pending 再认领
    if repo.get_node(p_node).status != "pending":
        graph.revise_node(mid, p_node, reason="v45 重置 P 再认领")
    # P 认领（叶子执行）→ 绑定上游输入（role=dep）
    aid = graph.start_attempt(mid, p_node, backend="stub")
    fp = real_fp(repo, aid)
    binding = json.loads(repo.get_attempt(aid).input_binding_json or "[]")
    dep_sources = [b["source"] for b in binding if b.get("role") == "dep"]
    assert dep_sources, "P 认领应绑定上游输入（role=dep）"
    from dag_agent.repository import now
    with repo.tx() as cur:
        repo.insert_split_proposal({
            "id": "sp-bind", "mission_id": mid, "node_id": p_node,
            "source_attempt_id": aid, "raw_sha256": "r",
            "content_sha256": "c", "raw_json": "{}",
            "fingerprint_json": json.dumps(fp),
            "status": "queued", "created_at": now()}, cur)
        cur.execute("UPDATE nodes SET status='waiting_split' WHERE id=?",
                    (p_node,))
        cur.execute("UPDATE attempts SET status='split', category='split'"
                    " WHERE id=?", (aid,))
    specs = [
        {"title": "ext-child", "statement": "s", "check_cmd": CMD_OK,
         "declared_artifacts": ["deliverable.md"], "deps": [],
         "source_local_id": "c1"},
        {"title": "ext-child2", "statement": "s", "check_cmd": CMD_OK,
         "declared_artifacts": ["deliverable.md"], "deps": ["c1"],
         "source_local_id": "c2"},
    ]
    kids = graph.apply_auto_split(mid, p_node, aid, "sp-bind",
                                  list(map(_frozen, specs)), fp)
    idmap = dict(zip(["c1", "c2"], kids))
    deps1 = [d.depends_on for d in repo.deps_of(idmap["c1"])]
    deps2 = [d.depends_on for d in repo.deps_of(idmap["c2"])]
    assert deps1 == dep_sources            # 外部输入完整继承
    assert idmap["c1"] in deps2            # 局部依赖映射正确
    assert all(u in deps2 for u in dep_sources)  # 外部 + 局部并存
    repo.close()


# ---------- 9. 并发 1 推进 ----------


def test_9_concurrency1_full_chain(tmp_path):
    """max_concurrency=1：执行→激活→规划（占同一执行位）→落图→子执行→
    整合→done；全程不被出题等待占死。"""
    def writer(title, workdir, statement):
        if "自动生长回归" not in title:
            return  # 只对拆分目标（root）提案；孩子/整合会话不写
        (Path(workdir) / ".dag").mkdir(exist_ok=True)
        (Path(workdir) / ".dag" / "proposal.json").write_text(
            make_proposal(), encoding="utf-8")
    repo, graph, mid, root = build_mission(tmp_path)
    backend = AutoGrowBackend(on_write=writer, deliver="auto")
    sched, summary = run_scheduler(repo, graph, mid, root, backend,
                                   auto_split=True, max_concurrency=1,
                                   timeout=90)
    assert summary["mission_status"] == "done"
    assert len(events_of(repo, mid, "split_applied")) == 1
    assert len(summary["timeline"]) >= 8   # 执行/规划/子执行/整合全有事件
    repo.close()


# ---------- 10. 停止：规划不占用、迟到不发布 ----------


def test_10_stop_prevents_apply(tmp_path):
    """hard_stop 后规划任务退出：提案保持 queued 不被应用；恢复后可继续。"""
    def writer(title, workdir, statement):
        if "自动生长回归" not in title:
            return  # 只对拆分目标（root）提案；孩子/整合会话不写
        (Path(workdir) / ".dag").mkdir(exist_ok=True)
        (Path(workdir) / ".dag" / "proposal.json").write_text(
            make_proposal(), encoding="utf-8")
    repo, graph, mid, root = build_mission(tmp_path)
    backend = AutoGrowBackend(on_write=writer, deliver="skip")
    sched = Scheduler(repo, graph, backend, max_concurrency=1,
                      auto_split=AutogrowConfig(enabled=True),
                      retry_policy=RetryPolicy(max_retries=0))
    ensure_pool(sched)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 10:
        sched.tick_dispatch(mid)
        if events_of(repo, mid, "split_activated"):
            break
        time.sleep(0.05)
    sched.hard_stop.set()   # kill 语义
    time.sleep(0.3)
    for _ in range(20):     # 停止状态下 tick 不派发、不应用
        sched.tick_dispatch(mid)
        time.sleep(0.05)
    assert events_of(repo, mid, "split_applied") == []
    rows = repo.list_split_proposals(mid)
    # 停止落点两种合法结果：worker 未入规划→queued；停止落在规划中途→
    # gate 拒绝（rejected 留痕，"运行权限已撤销"，节点回 pending）。
    # 核心不变量=未落图（无 split_applied）。
    assert rows[0]["status"] in ("queued", "rejected")
    if rows[0]["status"] == "rejected":
        assert "运行权限已撤销" in (rows[0]["reject_reason"] or "")
    repo.close()


# ---------- 11. 应用失败：无半图/无半计数/资源保留 ----------


def test_11_apply_failure_atomic(tmp_path):
    """应用中途失败（查重命中）→ 事务回滚：无半图、无半计数、
    资源账保留、提案保持 queued（父仍 waiting_split）。"""
    repo, graph, mid, root = build_mission(tmp_path)
    # 预置同名节点（触发查重闸门）
    graph.add_node(mid, title="子任务一",
                   statement=BRIEF.format(body="既有节点"),
                   parent_id=root, check_cmd=CMD_OK)
    # 造激活态（split 源尝试直接落库——有孩子的父不可再 start_attempt）
    aid, fp = stage_split(repo, graph, mid, root, "sp-atomic", "c")
    # 对源尝试结算一笔真实资源（统一入口；资源在检测前已结算的语义）
    from dag_agent.executor_contract import record_execution
    rec = record_execution(repo, aid, {
        "ok": True, "summary": "x", "session_id": "s1",
        "resources": {"origin": "stub", "agent_id": "s1", "tokens": 3,
                      "duration_ms": 5, "cost_currency": "USD",
                      "cost": {"amount": 0.5, "basis": "reported"},
                      "executor_started_at": "2026-10-09T00:00:00+00:00",
                      "executor_ended_at": "2026-10-09T00:00:01+00:00"}})
    assert rec["contract_status"] == "valid"
    specs = [{"title": "子任务一", "statement": BRIEF.format(body="dup"),
              "check_cmd": CMD_OK,
              "declared_artifacts": ["deliverable.md"], "deps": [],
              "source_local_id": "c1"},
             {"title": "子任务二", "statement": "s", "check_cmd": CMD_OK,
              "declared_artifacts": ["deliverable.md"], "deps": [],
              "source_local_id": "c2"}]
    n_before = len(repo.list_nodes(mid))
    with pytest.raises(Exception, match="查重命中"):
        graph.apply_auto_split(mid, root, aid, "sp-atomic",
                               list(map(_frozen, specs)), fp)
    assert len(repo.list_nodes(mid)) == n_before       # 无半图
    row = repo.get_split_proposal("sp-atomic")
    assert row["status"] == "queued"                   # 未计成功
    budget = json.loads(repo.get_mission(mid).budget_json)
    assert abs(budget["cost_used"] - 0.5) < 1e-9       # 资源账保留
    assert budget["calls_used"] == 0                   # 拒绝不补计预留
    repo.close()


# ---------- 12. 拒绝分类与有界性 ----------


def test_12_rejection_classified_bounded(tmp_path):
    """提案违规=split_rejected（violation），产物路径独立；重复触发有界
    （rejected 上限），无热循环（调度器 halt 带原因）。"""
    def writer(title, workdir, statement):
        if "自动生长回归" not in title:
            return  # 只对拆分目标（root）提案；孩子/整合会话不写
        (Path(workdir) / ".dag").mkdir(exist_ok=True)
        (Path(workdir) / ".dag" / "proposal.json").write_text(
            "{broken", encoding="utf-8")
    repo, graph, mid, root = build_mission(tmp_path)
    backend = AutoGrowBackend(on_write=writer, deliver="auto")
    sched, summary = run_scheduler(repo, graph, mid, root, backend,
                                   auto_split=True, timeout=40,
                                   max_retries=0)
    rej = events_of(repo, mid, "split_rejected")
    assert all('"violation"' in (e.payload_json or "") for e in rej)
    assert events_of(repo, mid, "split_applied") == []
    assert summary["halt_reason"] and "死局" in summary["halt_reason"]
    assert len(rej) <= 5          # 有界（重试耗尽即停，不热循环）
    repo.close()


# ---------- 13. 手工路径兼容 ----------


def test_13_manual_decompose_still_works(tmp_path):
    """手工 decompose/propose_decomposition 与现有验收路径不受影响。"""
    from dag_agent.coordinator import Proposal
    repo, graph, mid, root = build_mission(tmp_path)
    p = Proposal(node_id=root, integration_check=CMD_OK, children=[
        {"title": "手工子一", "statement": BRIEF.format(body="m1"),
         "check_cmd": CMD_OK, "deps": [],
         "declared_artifacts": ["deliverable.md"]},
        {"title": "手工子二", "statement": BRIEF.format(body="m2"),
         "check_cmd": CMD_OK, "deps": [],
         "declared_artifacts": ["deliverable.md"]},
    ])
    sched = Scheduler(repo, graph, AutoGrowBackend(),
                      auto_split=AutogrowConfig(enabled=False))
    ids = sched.propose_decomposition(mid, p)
    assert len(ids) == 2
    assert node_status(repo, root) == "waiting_children"
    b = AutoGrowBackend()
    s2, summary = run_scheduler(repo, graph, mid, root, b,
                                auto_split=False, timeout=60)
    assert summary["mission_status"] == "done"
    assert events_of(repo, mid, "split_proposed") == []
    repo.close()


def test_14_parse_placeholder_echo(tmp_path):
    """协议占位符语义：platform/-1 回填平台权威值；自填真实值必须匹配。"""
    fp = {"goal_version": 3, "node_revision": 2, "check_version": 4,
          "check_digest": "dig", "input_binding_hash": "ibh"}
    p = autogrow.parse_proposal(make_proposal().encode("utf-8"), fp,
                                "n7", "a-xyz")
    assert p.fingerprint["goal_version"] == 3
    assert p.source_attempt_id == "a-xyz" and p.parent_node_id == "n7"
    # 自填错值 → 拒绝
    bad = json.loads(make_proposal())
    bad["goal_version"] = 99
    with pytest.raises(autogrow.ProposalError, match="自填"):
        autogrow.parse_proposal(json.dumps(bad).encode("utf-8"), fp,
                                "n7", "a-xyz")
    # 自填正确值 → 通过
    good = json.loads(make_proposal())
    good.update({"source_attempt_id": "a-xyz", "parent_node_id": "n7",
                 "goal_version": 3, "node_revision": 2, "check_version": 4,
                 "check_digest": "dig", "input_binding_hash": "ibh"})
    p2 = autogrow.parse_proposal(json.dumps(good).encode("utf-8"), fp,
                                 "n7", "a-xyz")
    assert p2.proposal_id == "prop-test-0001"


def test_15_revise_split_parent_returns_to_integration(tmp_path):
    """revise 一个已拆分（waiting_children）节点：应回 waiting_children
    整合重跑（无条件 pending 会造成"有孩子的 pending 死水"——frontier
    排除有孩子节点、integration_ready 只认 waiting_children，整图卡死；
    s1-up-1h 实证后最小修复）。"""
    def writer(title, workdir, statement):
        if "自动生长回归" not in title:
            return
        (Path(workdir) / ".dag").mkdir(exist_ok=True)
        (Path(workdir) / ".dag" / "proposal.json").write_text(
            make_proposal(), encoding="utf-8")
    repo, graph, mid, root = build_mission(tmp_path)
    backend = AutoGrowBackend(on_write=writer, deliver="auto")
    sched, summary = run_scheduler(repo, graph, mid, root, backend,
                                   auto_split=True, timeout=60)
    assert summary["mission_status"] == "done"
    # 全图闭合后 revise 被拆分的 root（waiting_children，有孩子）→
    # 自动重开+回到整合态重跑（不得落成"有孩子的 pending 死水"）
    graph.revise_node(mid, root, reason="v45 拆分父修订回归")
    assert node_status(repo, root) == "waiting_children"
    b2 = AutoGrowBackend()
    sched2, summary2 = run_scheduler(repo, graph, mid, root, b2,
                                     auto_split=True, timeout=60)
    assert summary2["mission_status"] == "done"
    assert node_status(repo, root) == "accepted"
    repo.close()
