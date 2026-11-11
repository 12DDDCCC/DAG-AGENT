"""test_audit_prepare.py — 盲读回自动派发：结构性盲性（prompt绝不含意图）。"""
from __future__ import annotations

import pytest

from dag_agent.store import MissionStore


@pytest.fixture()
def store(tmp_path):
    return MissionStore(home=tmp_path)


def test_prompt_is_blind(store, tmp_path):
    """核心断言：audit_prepare的prompt含陈述+产物路径+审计规则，
    绝不含mission brief/意图——盲性由结构保证而非自觉。"""
    s = store
    s.create_mission("a1", "秘密意图任务名", "这是绝密意图简报BRIEF-XYZ",
                     "交付", "总陈述")
    (s.mission_dir("a1") / "workspace" / "out.txt").write_text(
        "产物内容", encoding="utf-8")
    s.decompose("a1", "n0", [{"title": "子任务", "statement": "写out.txt"}])
    p = s.audit_prepare("a1", "n1")
    assert "写out.txt" in p["prompt"], "应含节点陈述"
    assert "out.txt" in p["prompt"] and "workspace" in p["prompt"], "应含产物路径"
    assert "译物不译意" in p["prompt"], "应含AUDITOR规则"
    assert "BRIEF-XYZ" not in p["prompt"], "绝不能泄漏brief"
    assert "秘密意图" not in p["prompt"], "绝不能泄漏任务名意图"
    assert "不要附加brief" in p["note"]


def test_prompt_covers_all_deliverables(store):
    s = store
    s.create_mission("a2", "t", "b", "交付", "完成")
    (s.mission_dir("a2") / "workspace").mkdir(parents=True, exist_ok=True)
    for i in range(3):
        (s.mission_dir("a2") / "workspace" / f"f{i}.txt").write_text(
            "x", encoding="utf-8")
    p = s.audit_prepare("a2", "n0")
    for i in range(3):
        assert any(d.endswith(f"f{i}.txt") for d in p["deliverables"]), p["deliverables"]
