"""v3.1 设计点检验：每个 HTML 定稿设计一项测试。

对应《设计覆盖度审计-我的节点设计vs费马原设计》§3 增量四条 + §4 运营补齐 +
§5 三个裁定：五动作枚举/考卷体检三关/三分判决/维修协议/强度记账/
closure_basis/authoritative 分级/生长闸门六条/外部记忆检索/优先级调度。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dag_agent.acceptance import (ACTIONS, compile_all, preflight,  # noqa: E402
                                  run_acceptance, validate)
from dag_agent.assessment import run_check3  # noqa: E402
from dag_agent.graph import Graph, GraphError  # noqa: E402
from dag_agent.memory import find_similar, recall, tokenize  # noqa: E402
from dag_agent.recovery import RetryPolicy  # noqa: E402
from dag_agent.repository import Repository  # noqa: E402

# ---- 夹具 ----


class Stub3:
    """三分检查器替身：ERR→error，FAIL→fail，空→fail-closed，其余 pass。"""

    def __call__(self, cmd, cwd):
        if not cmd:
            return "fail", "未配置验收命令：fail-closed"
        if "ERR" in cmd:
            return "error", "stub error：环境起不来"
        if "FAIL" in cmd:
            return "fail", "stub fail"
        return "pass", "stub pass"


@pytest.fixture
def g3(tmp_path):
    repo = Repository(tmp_path / "mission.db")
    g = Graph(repo, checker3=Stub3())
    yield g
    repo.close()


BITING = 'python -c "import sys; sys.exit(1)"'          # 空目录必 FAIL（有牙齿）
ALWAYS_GREEN = 'python -c "print(1)"'                    # 空目录也过（废卷）


def _node(g, mid, root, title, check, statement="标准陈述", **kw):
    return g.add_node(mid, title, statement=statement, parent_id=root,
                      check_cmd=check, **kw)


# ---- §3.2 五动作枚举（受限语法） ----


def test_acceptance_enum_rejects_unknown_action():
    ok, why = validate([{"action": "docker", "cmd": "x"}])
    assert not ok and "不在枚举" in why and "docker" in why


def test_acceptance_enum_rejects_missing_params():
    ok, why = validate([{"action": "scan", "dir": "src"}])
    assert not ok and "banned" in why
    ok, why = validate([{"action": "http", "url": "http://x"}])
    assert not ok and "expect_status" in why


def test_acceptance_compile_whitelisted():
    cmds = compile_all([
        {"action": "test", "args": "tests/ -q"},
        {"action": "scan", "dir": "src", "banned": ["TODO", "FIXME"]},
        {"action": "http", "url": "http://127.0.0.1:1/x", "expect_status": 200},
    ])
    assert len(cmds) == 3
    for c in cmds:
        assert c.startswith("python ")   # 全部落在白名单入口内


def test_run_acceptance_aggregates_three_way(tmp_path):
    v, ev = run_acceptance([ALWAYS_GREEN, BITING], tmp_path)
    assert v == "fail"                    # 任一 fail → fail
    v, _ = run_acceptance(["nonexist_cmd_x -v"], tmp_path)
    assert v == "error"                   # 任一 error → error（考卷故障）


# ---- §3.1 考卷体检三关 ----


def test_preflight_gate1_syntax_rejects_non_whitelist():
    ok, why, detail = preflight(check_cmd="curl http://x")
    assert not ok and detail["gate1_syntax"] is False


def test_preflight_gate3_rejects_always_green_exam():
    ok, why, detail = preflight(check_cmd=ALWAYS_GREEN)
    assert not ok and "废卷" in why
    assert detail["gate1_syntax"] and not detail["gate3_bites"]


def test_preflight_gate2_rejects_broken_exam():
    ok, why, detail = preflight(check_cmd="python not_exist_entry.py")
    assert not ok and detail["gate2_runnable"] is False


def test_preflight_accepts_biting_exam():
    ok, why, detail = preflight(check_cmd=BITING)
    assert ok and all(detail.values())


def test_preflight_acceptance_path():
    # 有牙齿的结构化清单（cmd 动作承载退出码断言）→ 体检通过
    ok, why, detail = preflight(acceptance=[{"action": "cmd", "cmd": BITING}])
    assert ok and all(detail.values()), why
    # scan 单用在空目录必绿（无禁用词可命中）→ 废卷拒绝
    ok2, why2, _ = preflight(acceptance=[
        {"action": "scan", "dir": ".", "banned": ["TODO"]}])
    assert not ok2 and "废卷" in why2


# ---- §3.1/三分判决 run_check3 ----


def test_run_check3_three_way(tmp_path):
    assert run_check3('python -c "print(1)"', tmp_path)[0] == "pass"
    assert run_check3(BITING, tmp_path)[0] == "fail"
    assert run_check3("bash -c ls", tmp_path)[0] == "error"      # 白名单外
    v, ev = run_check3('python -c "import time; time.sleep(3)"', tmp_path, timeout=1)
    assert v == "error" and "超时" in ev


def test_run_check3_zero_write_is_error(tmp_path):
    v, ev = run_check3('python -c "open(\'x.txt\',\'w\').write(\'1\')"', tmp_path)
    assert v == "error" and "咬坏现场" in ev


def test_scan_checker_bites(tmp_path):
    (tmp_path / "a.py").write_text("x = 1  # TODO fix", encoding="utf-8")
    v, _ = run_check3(
        f'python "{Path(__file__).resolve().parents[2] / "dag_agent" / "checkers" / "check_scan.py"}"'
        f' "{tmp_path}" "TODO"', tmp_path)
    assert v == "fail"
    (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")
    v, _ = run_check3(
        f'python "{Path(__file__).resolve().parents[2] / "dag_agent" / "checkers" / "check_scan.py"}"'
        f' "{tmp_path}" "TODO"', tmp_path)
    assert v == "pass"


def test_http_checker_unreachable_is_error(tmp_path):
    v, ev = run_check3(
        f'python "{Path(__file__).resolve().parents[2] / "dag_agent" / "checkers" / "check_http.py"}"'
        f' "http://127.0.0.1:1/x" 200', tmp_path)
    assert v == "error"                     # 网络故障=考卷/环境故障，非产物不合格


# ---- §3.1 三分判决在语义层：ERROR 不打回执行者 ----


def test_submit_error_blocks_and_marks_error_attempt(g3):
    mid, root = g3.create_mission("三分")
    a = _node(g3, mid, root, "甲", "CHECK ERR HERE")
    aid = g3.start_attempt(mid, a)
    r = g3.submit_attempt(mid, a, aid)
    assert r["verdict"] == "error"
    assert g3.repo.get_node(a).status == "blocked"        # 等维修，不判 failed
    assert g3.repo.get_attempt(aid).status == "error"
    assert "check_error" in [e.type for e in g3.repo.events_of(mid)]


def test_error_does_not_consume_retry_budget(g3):
    mid, root = g3.create_mission("重试预算")
    a = _node(g3, mid, root, "甲", "CHECK ERR")
    aid = g3.start_attempt(mid, a)
    g3.submit_attempt(mid, a, aid)
    policy = RetryPolicy(max_retries=1)
    assert policy.allows(g3.repo, a) is True              # error 不算 failed 次数


def test_fail_still_marks_failed(g3):
    mid, root = g3.create_mission("失败路径")
    a = _node(g3, mid, root, "甲", "CHECK FAIL")
    aid = g3.start_attempt(mid, a)
    assert g3.submit_attempt(mid, a, aid)["verdict"] == "fail"
    assert g3.repo.get_node(a).status == "failed"


# ---- §3.1 考卷维修协议 ----


def test_repair_check_flow(g3):
    mid, root = g3.create_mission("维修")
    a = _node(g3, mid, root, "甲", "CHECK ERR")
    aid = g3.start_attempt(mid, a)
    g3.submit_attempt(mid, a, aid)                        # → blocked
    v = g3.repair_check(mid, a, new_check_cmd=BITING, reason="路径拼错")
    assert v == 2                                         # 版本+1
    assert g3.repo.get_node(a).status == "pending"        # 回队重验
    assert all(s.invalid == 1 for s in g3.repo.assessments_of(a))  # 旧证据作废
    evs = [e.type for e in g3.repo.events_of(mid)]
    assert "check_repaired" in evs


def test_repair_check_refuses_terminal_and_bad_exam(g3):
    mid, root = g3.create_mission("维修拒绝")
    a = _node(g3, mid, root, "甲", "CHECK OK")
    aid = g3.start_attempt(mid, a)
    g3.submit_attempt(mid, a, aid)                        # accepted
    with pytest.raises(GraphError, match="终态"):
        g3.repair_check(mid, a, new_check_cmd=BITING)
    b = _node(g3, mid, root, "乙", "CHECK FAIL")
    bid = g3.start_attempt(mid, b)
    g3.submit_attempt(mid, b, bid)                        # failed
    with pytest.raises(GraphError, match="体检未过"):
        g3.repair_check(mid, b, new_check_cmd=ALWAYS_GREEN)  # 废卷拒绝


# ---- §3 强度记账 / closure_basis / authoritative ----


def test_strength_declaration(g3):
    mid, root = g3.create_mission("强度")
    a = _node(g3, mid, root, "甲", "CHECK OK")
    with pytest.raises(GraphError, match="不可为空"):
        g3.declare_strength(mid, a, "  ")
    g3.declare_strength(mid, a, "只做了X；Y未做（依赖Z尚未闭合）")
    assert "Y未做" in g3.repo.get_node(a).strength_actual
    assert "strength_declared" in [e.type for e in g3.repo.events_of(mid)]


def test_closure_basis_machine_by_default(g3):
    mid, root = g3.create_mission("依据")
    assert g3.repo.get_mission(mid).closure_basis == "machine"
    a = _node(g3, mid, root, "甲", "CHECK OK")
    aid = g3.start_attempt(mid, a)
    g3.submit_attempt(mid, a, aid)                        # 根整合 → done
    assert g3.repo.get_mission(mid).closure_basis == "machine"


def test_subjective_closure(g3):
    mid, root = g3.create_mission("主观闭合")
    # v3.2 规格演进：主观闭合=显式签收，必须记录签收依据（协议03/任务书第2步）
    g3.close_mission_subjective(mid, note="文案质感无法机器判定，人审签收",
                                basis="对照任务书三条文风要求逐条人工核对")
    m = g3.repo.get_mission(mid)
    assert m.status == "done" and m.closure_basis == "subjective"
    assert "mission_done_subjective" in [e.type for e in g3.repo.events_of(mid)]


def test_self_check_is_non_authoritative(g3):
    mid, root = g3.create_mission("自检")
    a = _node(g3, mid, root, "甲", "CHECK OK")
    out = g3.self_check(mid, a)
    assert out["authoritative"] == 0 and out["verdict"] == "pass"
    assert g3.repo.get_node(a).status == "pending"        # 状态不受自检影响
    assess = g3.repo.assessments_of(a)
    assert len(assess) == 1 and assess[0].authoritative == 0


# ---- 生长闸门六条 ----


def test_gate_orphan_rejected(g3):
    mid, root = g3.create_mission("无主")
    with pytest.raises(GraphError, match="无主节点禁止"):
        g3.add_node(mid, "野节点", statement="没有归属")


def test_gate_duplicate_title_rejected(g3):
    mid, root = g3.create_mission("查重")
    _node(g3, mid, root, "实现登录", "CHECK OK")
    with pytest.raises(GraphError, match="等价节点已存在"):
        _node(g3, mid, root, "实现登录", "CHECK OK", statement="换个陈述也没用")
    _node(g3, mid, root, "实现登出", "CHECK OK")          # 不同名可过


def test_gate_granularity(g3):
    mid, root = g3.create_mission("粒度")
    with pytest.raises(GraphError, match="粒度过大"):
        g3.add_node(mid, "巨节点", statement="字" * 2001, parent_id=root,
                    check_cmd="CHECK OK")
    with pytest.raises(GraphError, match="超上限"):
        g3.add_node(mid, "多依赖", statement="依赖过多", parent_id=root,
                    check_cmd="CHECK OK",
                    deps=tuple(_node(g3, mid, root, f"垫{i}", "CHECK OK")
                               for i in range(9)))


def test_gate_unsatisfiable_dep_rejected(g3):
    mid, root = g3.create_mission("悬空")
    a = _node(g3, mid, root, "甲", "CHECK OK")
    g3.cancel_node(mid, a, reason="测试取消")
    with pytest.raises(GraphError, match="不可满足"):
        g3.add_node(mid, "乙", statement="依赖已取消者", parent_id=root,
                    check_cmd="CHECK OK", deps=(a,))


def test_gate_statement_or_check_required(g3):
    mid, root = g3.create_mission("三件套")
    with pytest.raises(GraphError, match="三件套不齐"):
        g3.add_node(mid, "裸节点", parent_id=root)


# ---- 外部记忆检索（查重先于生长的软通道） ----


def test_recall_finds_by_tokens(g3):
    mid, root = g3.create_mission("记忆")
    _node(g3, mid, root, "调研竞品", "CHECK OK", statement="收集竞品资料成表")
    _node(g3, mid, root, "写周报", "CHECK OK", statement="汇总本周进展")
    hits = recall(g3.repo, mid, "竞品 资料收集")
    assert hits and hits[0]["title"] == "调研竞品"
    assert recall(g3.repo, mid, "毫不相干的查询词组") == []


def test_find_similar_normalized_equality(g3):
    mid, root = g3.create_mission("同名")
    _node(g3, mid, root, "实现 登录", "CHECK OK")
    assert find_similar(g3.repo, mid, "实现登录")          # 空白差异不算不同名
    assert not find_similar(g3.repo, mid, "实现登出")


# ---- Anthropic 原版补齐：优先级调度 ----


def test_priority_dispatch_order(tmp_path):
    from dag_agent.scheduler import Budget, Scheduler
    from test_step4_scheduler import FakeBackend
    repo = Repository(tmp_path / "mission.db")
    g = Graph(repo)
    s = Scheduler(repo, g, FakeBackend(delay=0.01), max_concurrency=1,
                  budget=Budget(max_calls=16))
    mid, root = g.create_mission("优先级", root_check="python -c 'pass'")
    low = g.add_node(mid, "低优先", statement="普通任务", parent_id=root,
                     check_cmd="python -c 'pass'")
    high = g.add_node(mid, "高优先", statement="紧急任务", parent_id=root,
                      check_cmd="python -c 'pass'")
    g.set_priority(mid, high, 9)
    g.set_priority(mid, low, 0)
    summary = s.run_until_done(mid, root, timeout=30)
    assert summary["mission_status"] == "done"
    first_start = next(t["node"] for t in summary["timeline"] if t["phase"] == "start")
    assert first_start == high                      # 大者优先派发
    repo.close()


# ---- 受影响面（增量重验依据） ----


def test_affected_subgraph(g3):
    mid, root = g3.create_mission("影响面")
    a = _node(g3, mid, root, "甲", "CHECK OK")
    b = g3.add_node(mid, "乙", statement="依赖甲", parent_id=root,
                    check_cmd="CHECK OK", deps=(a,))
    c = g3.add_node(mid, "丙", statement="依赖乙", parent_id=root,
                    check_cmd="CHECK OK", deps=(b,))
    kid = g3.add_node(mid, "甲崽", statement="甲的孩子", parent_id=a,
                      check_cmd="CHECK OK")
    affected = g3.affected_subgraph(mid, a)
    assert set(affected) == {b, c, kid}             # 依赖闭包+归属闭包
    assert g3.affected_subgraph(mid, c) == []


# ---- 主观层审阅 ----


def test_attach_review_and_verdict_validation(g3):
    mid, root = g3.create_mission("审阅")
    a = _node(g3, mid, root, "甲", "CHECK OK")
    rid = g3.attach_review(mid, a, "concern", "命名与实现有偏差", blind=True)
    assert rid >= 1
    rs = g3.repo.reviews_of(a)
    assert len(rs) == 1 and rs[0].blind == 1 and rs[0].verdict == "concern"
    with pytest.raises(GraphError, match="accept|concern|reject"):
        g3.attach_review(mid, a, "excellent")


# ---- 外部权威锚 ----


def test_anchor_stored(g3):
    mid, root = g3.create_mission("锚", anchor="OpenAPI 3.1 规范 / RFC9110")
    assert "RFC9110" in g3.repo.get_mission(mid).anchor
