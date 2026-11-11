# -*- coding: utf-8 -*-
"""v3.8 T4/T5 回归：执行器契约 v1（时序上报）+ 快照血缘（per-file sources）。

T4（契约 v1）：merge_executor_resources 全字段 merge 无告警；缺
executor_started_at/executor_ended_at 落 executor_contract_warning 事件。
T5（血缘）：submit 协议在快照 manifest 落 sources（每件文件 ← 上游快照
node:version）；无上游的叶子 sources={}；旧快照（无 sources 字段）兼容读。
"""
from __future__ import annotations

import json

import pytest

from dag_agent.artifacts import lineage_of, verify_lineage
from dag_agent.executor_contract import CONTRACT_V1, merge_executor_resources
from dag_agent.graph import Graph
from dag_agent.repository import Repository

BITES = ("python -c \"import pathlib,sys; sys.exit(0 if any("
         "pathlib.Path('.').glob('*.txt')) else 1)\"")
BRIEF = ("【输入】无/【交付清单】impl.txt/【接口签名】文件交付/"
         "【样例】内容 ok/【自测入口】type impl.txt")


def _mk_mission(tmp_path):
    repo = Repository(tmp_path / "m" / "mission.db")
    g = Graph(repo)
    mid, root = g.create_mission("契约与血缘", root_check=BITES,
                                 root_declared_artifacts=("impl.txt",))
    return repo, g, mid, root


def _full_receipt() -> dict:
    return {"origin": "zcode-subagent", "agent_id": "agent_test-0001",
            "tokens": 123456, "duration_ms": 42000,
            "cost_currency": "local-zcode-plan",
            "executor_started_at": "2026-10-06T15:00:00+0800",
            "executor_ended_at": "2026-10-06T15:00:42+0800"}


# ---- T4 ----

def test_contract_v1_full_receipt_no_warning(tmp_path):
    repo, g, mid, root = _mk_mission(tmp_path)
    leaf = g.add_node(mid, "L", BRIEF, parent_id=root, check_cmd=BITES,
                      declared_artifacts=("impl.txt",))
    aid = g.start_attempt(mid, leaf, backend="zcode-agent")
    res = merge_executor_resources(repo, mid, leaf, aid, _full_receipt())
    assert res["ok"] and res["missing_timing"] == []
    rj = json.loads(repo.get_attempt(aid).resources_json)
    for k in CONTRACT_V1:
        assert rj.get(k), f"契约字段缺失：{k}"
    warns = [e for e in repo.events_of(mid)
             if e.type == "executor_contract_warning"]
    assert warns == []
    repo.close()


def test_contract_v1_missing_timing_warns(tmp_path):
    repo, g, mid, root = _mk_mission(tmp_path)
    leaf = g.add_node(mid, "L", BRIEF, parent_id=root, check_cmd=BITES,
                      declared_artifacts=("impl.txt",))
    aid = g.start_attempt(mid, leaf, backend="zcode-agent")
    bad = _full_receipt()
    bad.pop("executor_started_at")
    res = merge_executor_resources(repo, mid, leaf, aid, bad)
    assert res["ok"] and res["missing_timing"] == ["executor_started_at"]
    warns = [e for e in repo.events_of(mid)
             if e.type == "executor_contract_warning"]
    assert len(warns) == 1 and warns[0].node_id == leaf
    payload = json.loads(warns[0].payload_json)
    assert payload["missing"] == "executor_started_at"
    # 合并仍生效（不拒收，兼容 v0）
    rj = json.loads(repo.get_attempt(aid).resources_json)
    assert rj["agent_id"] == "agent_test-0001"
    repo.close()


# ---- T5 ----

def test_snapshot_sources_lineage_recorded(tmp_path):
    """下游快照 per-file lineage：播种件 ← 上游快照 node:version，
    自产物 source=None；机器可答任一文件来源（lineage_of）。"""
    repo, g, mid, root = _mk_mission(tmp_path)
    src = g.add_node(mid, "S", BRIEF, parent_id=root, check_cmd=BITES,
                     declared_artifacts=("lib.txt",))
    down = g.add_node(mid, "D", BRIEF, parent_id=root, deps=(src,),
                      check_cmd=BITES, declared_artifacts=("out.txt",))
    aid1 = g.start_attempt(mid, src, backend="stub")
    from pathlib import Path
    Path(g.repo.get_attempt(aid1).workdir, "lib.txt").write_text(
        "LIB", encoding="utf-8")
    r1 = g.submit_attempt(mid, src, aid1, artifact_paths=["lib.txt"])
    assert r1["verdict"] == "pass"

    aid2 = g.start_attempt(mid, down, backend="stub")
    wd = Path(g.repo.get_attempt(aid2).workdir)
    # 平台播种布局：inputs/<src>/lib.txt（宿主驱动需自播——此处按协议手播）
    from dag_agent.artifacts import ArtifactStore
    store = ArtifactStore(Path(repo.db_path).parent)
    snap1 = [s for s in repo.snapshots_of(src) if s.valid][-1]
    store.seed_from_snapshot(snap1, wd)
    (wd / "out.txt").write_text("OUT", encoding="utf-8")
    # 申报集合=发布集合：自产物 out.txt + 原样转交的播种件（inputs/S/lib.txt）
    r2 = g.submit_attempt(mid, down, aid2,
                          artifact_paths=["out.txt", f"inputs/{src}/lib.txt"])
    assert r2["verdict"] == "pass"
    snap2 = [s for s in repo.snapshots_of(down) if s.valid][-1]
    files = json.loads(snap2.files_json)
    by_rel = {f["rel_path"]: f.get("source") for f in files}
    # T5' 新语义：无上游依据=unknown（不自动断言 self；unknown 即本节点
    # 产物/派生——内容≠上游，不冒充原件也不得为 None 丢失可溯性标记）
    assert by_rel["out.txt"] == {"origin": "unknown"}
    # T5'：upstream 来源带 basis（path+sha=原样转交强证据）
    assert by_rel[f"inputs/{src}/lib.txt"] == {"node": src, "version": 1,
                                              "basis": "path+sha",
                                          "file": "lib.txt"}
    # 内容级血缘：与上游逐字节一致的合并件（放在工作目录根）推断来源
    # ——用第二个下游节点 D2（down 已 accepted 不可再 start_attempt）
    down2 = g.add_node(mid, "D2", BRIEF, parent_id=root, deps=(src,),
                       check_cmd=BITES, declared_artifacts=("out.txt",))
    aid3 = g.start_attempt(mid, down2, backend="stub")
    wd3 = Path(g.repo.get_attempt(aid3).workdir)
    store.seed_from_snapshot(snap1, wd3)
    (wd3 / "lib_copy.txt").write_text("LIB", encoding="utf-8")
    (wd3 / "out.txt").write_text("OUT-V2", encoding="utf-8")
    r3 = g.submit_attempt(mid, down2, aid3,
                          artifact_paths=["out.txt", "lib_copy.txt"])
    assert r3["verdict"] == "pass"
    snap3 = [s for s in repo.snapshots_of(down2) if s.valid][-1]
    by_rel3 = {f["rel_path"]: f.get("source") for f in
               json.loads(snap3.files_json)}
    assert by_rel3["lib_copy.txt"] == {"node": src, "version": 1,
                                       "basis": "sha",
                                       "file": "lib.txt"}
    # 机器可答任一文件来源
    from dag_agent.artifacts import lineage_of
    assert lineage_of(snap3, "lib_copy.txt")["origin"] == "upstream"
    # T5'：out.txt 内容≠上游 → unknown（无上游依据；不再自动断言 self）
    assert lineage_of(snap3, "out.txt")["origin"] == "unknown"
    # 断链校验：上游快照被作废后 lineage 校验报断链
    with repo.tx() as cur:
        cur.execute("UPDATE snapshots SET valid=0 WHERE id=?", (snap1.id,))
    broken = verify_lineage(repo, snap3)
    # T5'：verify 结果含全部未完全可溯件（unknown 条目+断链条目）；
    # 断链识别=列表中存在 lib_copy.txt 的上游缺失条目
    assert any(x["rel_path"] == "lib_copy.txt"
               and "断链" in x["reason"] for x in broken)
    repo.close()


def test_snapshot_sources_leaf_self_and_legacy_ok(tmp_path):
    """叶子（无上游）全部 source=None；旧快照（无 source 键）读取不炸。"""
    repo, g, mid, root = _mk_mission(tmp_path)
    leaf = g.add_node(mid, "L", BRIEF, parent_id=root, check_cmd=BITES,
                      declared_artifacts=("impl.txt",))
    aid = g.start_attempt(mid, leaf, backend="stub")
    from pathlib import Path
    Path(g.repo.get_attempt(aid).workdir, "impl.txt").write_text(
        "ok", encoding="utf-8")
    r = g.submit_attempt(mid, leaf, aid, artifact_paths=["impl.txt"])
    assert r["verdict"] == "pass"
    snap = [s for s in repo.snapshots_of(leaf) if s.valid][-1]
    files = json.loads(snap.files_json)
    # T5'：source 恒有值（无依据→origin=unknown），不再以 None 表示
    assert all((f.get("source") or {}).get("origin") == "unknown"
               for f in files)
    # 旧快照兼容：手工剥掉 source 键再读（=v1 之前的快照形态）
    legacy = [{k: v for k, v in f.items() if k != "source"}
              for f in files]
    from dag_agent.artifacts import lineage_of
    assert lineage_of(_FakeSnap(legacy), "impl.txt")["origin"] == "legacy"
    repo.close()


class _FakeSnap:
    """旧快照形态（files_json 无 source 键）读取兼容性探针。"""

    def __init__(self, files):
        self.files_json = json.dumps(files, ensure_ascii=False)
        self.node_id = "x"
        self.artifact_version = 1
