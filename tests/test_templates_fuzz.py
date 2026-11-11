"""test_templates_fuzz.py — 模板库 + 随机操作浸泡测试（不变量保持）。"""
from __future__ import annotations

import json
import random

import pytest

from dag_agent.store import DagError, MissionStore
from dag_agent.templates import create_from_template


class Stub:
    def run(self, checks):
        if checks:
            return {"ok": True, "evidence": "stub"}
        return {"ok": True, "evidence": "no-checks"}


def test_template_research_end_to_end(tmp_path):
    s = MissionStore(home=tmp_path)
    r = create_from_template(s, "t1", "一次调研", "b", "research")
    assert r["children"] == ["n1", "n2", "n3"]
    fr = [x["id"] for x in s.frontier("t1")]
    assert fr == ["n1", "n2", "n3"]
    # 逐个真实做工作产物并提交——通用校验器按模板关键词断言
    ws = s.mission_dir("t1") / "workspace"
    artifacts = {"n1": ("draft.md", "资料：收集了三篇"),
                 "n2": ("facts_check.md", "核对：全部通过"),
                 "n3": ("report.md", "结论：可行。信息源：三篇")}
    for nid, (fn, content) in artifacts.items():
        (ws / fn).write_text(content, encoding="utf-8")
        res = s.submit("t1", nid, f"{fn}完成", checker=Stub())
        assert res["verdict"] == "PASS"
    assert s.status("t1")["mission_done"], "research模板三步完成应级联闭合根"


def test_template_missing_keyword_fails(tmp_path):
    s = MissionStore(home=tmp_path)
    create_from_template(s, "t2", "审计", "b", "audit")
    (s.mission_dir("t2") / "workspace" / "readback.txt").write_text(
        "什么关键信息都没有", encoding="utf-8")
    res = s.submit("t2", "n1", "草率提交")   # 真实CheckRunner跑模板校验器
    assert res["verdict"] == "FAIL" and "缺关键节" in res["evidence"]


def test_template_unknown_rejected(tmp_path):
    s = MissionStore(home=tmp_path)
    with pytest.raises(ValueError, match="未知模板"):
        create_from_template(s, "t3", "x", "b", "nope")


def test_fuzz_soak_300_ops(tmp_path):
    """300次随机操作浸泡：只允许预期内拒绝；终态不变量全保持。"""
    rng = random.Random(20260929)
    s = MissionStore(home=tmp_path)
    mids = ["f1", "f2", "f3"]
    for mid in mids:
        create_from_template(s, mid, f"浸泡{mid}", "b", "research")
    expected_rejects = {"已done", "aggregate", "依赖未完成", "不存在",
                        "冻结", "无需解锁", "不可再分解", "无需重试闭合",
                        "只能分解"}
    accepted = 0
    for op_no in range(300):
        mid = rng.choice(mids)
        nodes = s.load(mid)["nodes"]
        try:
            choice = rng.random()
            if choice < 0.30:  # 认领提交前沿首个
                fr = s.frontier(mid)
                if not fr:
                    continue
                r = s.submit(mid, fr[0]["id"], f"op{op_no}", checker=Stub())
                accepted += 1
                if r.get("auto_closed"):
                    pass
            elif choice < 0.45:  # 造工作产物再提交
                fr = s.frontier(mid)
                if not fr:
                    continue
                (s.mission_dir(mid) / "workspace" / f"w{op_no}.md").write_text(
                    f"内容{op_no} 资料 结论 信息源 边界 差距 用法 实现 核对 passed",
                    encoding="utf-8")
                r = s.submit(mid, fr[0]["id"], f"op{op_no}", checker=Stub())
                accepted += 1
            elif choice < 0.60:  # 分解已done节点→预期拒绝
                done = [nid for nid, n in nodes.items() if n["status"] == "done"]
                if done:
                    s.decompose(mid, rng.choice(done),
                                [{"title": "x", "statement": "y"}])
            elif choice < 0.72:  # block随机节点
                nid = rng.choice(list(nodes))
                s.block(mid, nid, f"op{op_no}")
            elif choice < 0.84:  # unblock随机节点
                nid = rng.choice(list(nodes))
                s.unblock(mid, nid)
            elif choice < 0.92:  # 附读回随机节点
                nid = rng.choice(list(nodes))
                s.set_readback(mid, nid, f"证词{op_no}。边界：略。", model="fuzz")
            else:  # retry
                s.retry(mid)
        except DagError as exc:
            assert any(k in str(exc) for k in expected_rejects), \
                f"op{op_no}出现预期外拒绝: {exc}"
        except ValueError:
            pass

    # 终态不变量
    for mid in mids:
        state = s.load(mid)
        for nid, n in state["nodes"].items():
            if n["status"] == "done":
                assert isinstance(n.get("result"), dict), f"{nid} done无结果"
            if n["deps"]:
                for d in n["deps"]:
                    assert d in state["nodes"], f"{nid}悬空依赖{d}"
        board = s.board_path(mid).read_text(encoding="utf-8")
        assert "```mermaid" in board
        for line in s.read_events(mid, tail=1000):
            assert isinstance(line, dict)
        assert (Path := __import__("pathlib").Path(s.board_path(mid))).exists()
    done_total = sum(
        s.status(m)["counts"]["done"] for m in mids)
    assert done_total >= 3, f"浸泡后至少应有一部分推进，实际done={done_total}"
