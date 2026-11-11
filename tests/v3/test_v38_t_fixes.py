"""v3.8 T 梯队修复回归（Codex t-review 三缺陷，2026-10-06）。

T6：维修换入口必须同步清单——入口不被继承清单覆盖时拒绝维修；
    带完整清单维修后新考卷漂移必拒；创建时清单漏实际入口拒绝；
    显式节点不得以空清单退回扫描模式。
来源：work/2026-10-06-t-review/exam_repair_result.json + 本仓独立复现。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dag_agent.assessment import default_checker3
from dag_agent.graph import Graph, GraphError
from dag_agent.repository import Repository

CHK = "python -c 'pass'"
BRIEF = ("【输入】无/【交付清单】x.txt/【接口签名】文件交付/【样例】GOOD/"
         "【自测入口】type x.txt")
GOOD = "import sys\nsys.exit(0 if open('x.txt').read()=='GOOD' else 1)\n"


def _env(tmp_path):
    repo = Repository(tmp_path / "m" / "mission.db")
    return repo, Graph(repo, checker3=default_checker3)


def _mk_node(g, mid, root, exam, entry):
    return g.add_node(mid, "A", BRIEF, parent_id=root,
                      check_cmd=f'python "{entry}"',
                      declared_artifacts=("x.txt",),
                      exam_files=[str(exam)])


def test_t6_repair_rejects_entry_outside_inherited_list(tmp_path):
    """用例1：维修换入口不更新清单 → 拒绝；版本与清单不变。"""
    repo, g = _env(tmp_path)
    try:
        old = tmp_path / "old_exam.py"
        new = tmp_path / "new_exam.py"
        old.write_text(GOOD, encoding="utf-8")
        new.write_text(GOOD, encoding="utf-8")
        mid, root = g.create_mission("T6-1", root_check=CHK)
        n = _mk_node(g, mid, root, old, old)
        v0 = repo.get_node(n).check_version
        with pytest.raises(GraphError, match="实际入口不在显式清单内"):
            g.repair_check(mid, n, new_check_cmd=f'python "{new}"',
                           reason="换入口不换清单")
        node = repo.get_node(n)
        assert node.check_version == v0
        assert json.loads(node.exam_files_json) == [str(old)]
    finally:
        repo.close()


def test_t6_repair_with_full_list_then_drift_rejected(tmp_path):
    """用例2：新命令+完整新清单 → 维修成功；认领后改新考卷 → 提交必拒。"""
    repo, g = _env(tmp_path)
    try:
        old = tmp_path / "old_exam.py"
        new = tmp_path / "new_exam.py"
        old.write_text(GOOD, encoding="utf-8")
        new.write_text(GOOD, encoding="utf-8")
        mid, root = g.create_mission("T6-2", root_check=CHK)
        n = _mk_node(g, mid, root, old, old)
        g.repair_check(mid, n, new_check_cmd=f'python "{new}"',
                       reason="换入口并同步清单", exam_files=[str(new)])
        assert json.loads(repo.get_node(n).exam_files_json) == [str(new)]
        aid = g.start_attempt(mid, n, backend="stub")
        wd = Path(repo.get_attempt(aid).workdir)
        (wd / "x.txt").write_text("BAD", encoding="utf-8")
        new.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        with pytest.raises(GraphError, match="考卷内容已变更"):
            g.submit_attempt(mid, n, aid, artifact_paths=["x.txt"])
    finally:
        repo.close()


def test_t6_add_node_rejects_list_missing_entry(tmp_path):
    """用例3：声明清单漏实际入口 → 创建拒绝（未冻结的判卷代码不得使用）。"""
    repo, g = _env(tmp_path)
    try:
        exam = tmp_path / "exam.py"
        decoy = tmp_path / "policy.txt"
        exam.write_text(GOOD, encoding="utf-8")
        decoy.write_text("policy", encoding="utf-8")
        mid, root = g.create_mission("T6-3", root_check=CHK)
        with pytest.raises(GraphError, match="未覆盖实际执行入口"):
            _mk_node(g, mid, root, decoy, exam)   # 清单只列 policy.txt
    finally:
        repo.close()


def test_t6_repair_empty_list_on_explicit_node_rejected(tmp_path):
    """用例4：显式节点空清单维修 → 拒绝（不得静默退回扫描模式）。"""
    repo, g = _env(tmp_path)
    try:
        exam = tmp_path / "exam.py"
        exam.write_text(GOOD, encoding="utf-8")
        mid, root = g.create_mission("T6-4", root_check=CHK)
        n = _mk_node(g, mid, root, exam, exam)
        exam.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
        with pytest.raises(GraphError, match="静默退回扫描模式"):
            g.repair_check(mid, n, new_check_cmd=f'python "{exam}"',
                           reason="空清单", exam_files=[])
    finally:
        repo.close()


def test_t6_platform_checker_exempt_from_cover(tmp_path):
    """用例5（边界）：scan/http 动作的平台 checker 豁免覆盖校验——
    出题方无须把平台组件列入业务清单（边界与解释器/标准库同理）。"""
    from dag_agent.acceptance import verify_exam_files_cover
    target = tmp_path / "data"
    target.mkdir()
    acc = [{"action": "scan", "dir": str(target), "banned": ["TODO"]}]
    ok, missing = verify_exam_files_cover(acceptance=acc, exam_files=[])
    assert ok and not missing


# ---- T5：来源三条件 + 歧义 + legacy（Codex t-review 假来源反例）----

def _t5_env(tmp_path):
    repo = Repository(tmp_path / "m" / "mission.db")
    return repo, Graph(repo, checker3=default_checker3)


def _t5_finish(g, repo, mid, nid, files):
    aid = g.start_attempt(mid, nid, backend="stub")
    wd = Path(repo.get_attempt(aid).workdir)
    for name, content in files.items():
        (wd / name).write_text(content, encoding="utf-8")
    return g.submit_attempt(mid, nid, aid,
                            artifact_paths=sorted(files))


def _snap_of(repo, nid):
    import types
    row = repo._conn.execute(
        "SELECT * FROM snapshots WHERE node_id=? ORDER BY id DESC LIMIT 1",
        (nid,)).fetchone()
    return types.SimpleNamespace(**dict(row))


def test_t5_rewritten_and_invented_files_not_marked_upstream(tmp_path):
    """改写播种件 + 凭空新增件不得冒充上游（unknown），断链列表非空。"""
    from dag_agent.artifacts import lineage_of, verify_lineage
    repo, g = _t5_env(tmp_path)
    try:
        mid, root = g.create_mission("T5-1", root_check=CHK)
        up = g.add_node(mid, "U", BRIEF, parent_id=root, check_cmd=CHK,
                        declared_artifacts=("out.txt",))
        assert _t5_finish(g, repo, mid, up,
                          {"out.txt": "LIB"})["verdict"] == "pass"
        dn = g.add_node(mid, "D", BRIEF, parent_id=root, deps=(up,),
                        check_cmd=CHK,
                        declared_artifacts=("out.txt", "never.txt"))
        # 手工构造 inputs（模拟播种）后改写+新增
        aid = g.start_attempt(mid, dn, backend="stub")
        wd = Path(repo.get_attempt(aid).workdir)
        ins = wd / "inputs" / up
        ins.mkdir(parents=True)
        (ins / "out.txt").write_text("CHANGED", encoding="utf-8")
        (ins / "never.txt").write_text("INVENTED", encoding="utf-8")
        (wd / "out.txt").write_text("CHANGED", encoding="utf-8")
        (wd / "never.txt").write_text("INVENTED", encoding="utf-8")
        res = g.submit_attempt(mid, dn, aid,
                               artifact_paths=["out.txt", "never.txt"])
        assert res["verdict"] == "pass"
        snap = _snap_of(repo, dn)
        for rel in ("out.txt", "never.txt"):
            src = lineage_of(snap, rel)
            assert src["origin"] == "unknown", (rel, src)
        findings = verify_lineage(repo, snap)
        assert findings, "unknown 来源必须在断链/未溯列表中显式出现"
        assert all("unknown" in x["reason"] for x in findings)
    finally:
        repo.close()


def test_t5_verbatim_transfer_marked_upstream_strong(tmp_path):
    """正例：逐字节转交 → upstream（basis=path+sha），verify 空。"""
    from dag_agent.artifacts import lineage_of, verify_lineage
    repo, g = _t5_env(tmp_path)
    try:
        mid, root = g.create_mission("T5-2", root_check=CHK)
        up = g.add_node(mid, "U", BRIEF, parent_id=root, check_cmd=CHK,
                        declared_artifacts=("out.txt",))
        assert _t5_finish(g, repo, mid, up,
                          {"out.txt": "LIB"})["verdict"] == "pass"
        dn = g.add_node(mid, "D", BRIEF, parent_id=root, deps=(up,),
                        check_cmd=CHK, declared_artifacts=("out.txt",))
        aid = g.start_attempt(mid, dn, backend="stub")
        wd = Path(repo.get_attempt(aid).workdir)
        ins = wd / "inputs" / up
        ins.mkdir(parents=True)
        (ins / "out.txt").write_text("LIB", encoding="utf-8")
        (wd / "out.txt").write_text("LIB", encoding="utf-8")
        assert g.submit_attempt(mid, dn, aid,
                                artifact_paths=["out.txt"])["verdict"] == "pass"
        snap = _snap_of(repo, dn)
        src = lineage_of(snap, "out.txt")
        assert src["origin"] == "upstream" and src["node"] == up, src
        assert src.get("basis") in ("path+sha", "sha"), src
        assert verify_lineage(repo, snap) == []
    finally:
        repo.close()


def test_t5_ambiguous_multi_source_same_sha(tmp_path):
    """两个上游同内容 → ambiguous（候选列表），verify 报歧义不静默取一。"""
    from dag_agent.artifacts import lineage_of, verify_lineage
    repo, g = _t5_env(tmp_path)
    try:
        mid, root = g.create_mission("T5-3", root_check=CHK)
        u1 = g.add_node(mid, "U1", BRIEF, parent_id=root, check_cmd=CHK,
                        declared_artifacts=("a.txt",))
        u2 = g.add_node(mid, "U2", BRIEF, parent_id=root, check_cmd=CHK,
                        declared_artifacts=("a.txt",))
        assert _t5_finish(g, repo, mid, u1, {"a.txt": "SAME"})["verdict"] == "pass"
        assert _t5_finish(g, repo, mid, u2, {"a.txt": "SAME"})["verdict"] == "pass"
        dn = g.add_node(mid, "D", BRIEF, parent_id=root, deps=(u1, u2),
                        check_cmd=CHK, declared_artifacts=("a.txt",))
        assert _t5_finish(g, repo, mid, dn,
                          {"a.txt": "SAME"})["verdict"] == "pass"
        snap = _snap_of(repo, dn)
        src = lineage_of(snap, "a.txt")
        assert src["origin"] == "ambiguous" and len(src["candidates"]) == 2, src
        findings = verify_lineage(repo, snap)
        assert any("ambiguous" in x["reason"] for x in findings)
    finally:
        repo.close()


def test_t5_legacy_snapshot_without_source(tmp_path):
    """旧协议快照（无 source 键）→ legacy（不追溯性证明自产）。"""
    from dag_agent.artifacts import lineage_of
    repo, g = _t5_env(tmp_path)
    try:
        mid, root = g.create_mission("T5-4", root_check=CHK)
        n = g.add_node(mid, "A", BRIEF, parent_id=root, check_cmd=CHK,
                       declared_artifacts=("x.txt",))
        assert _t5_finish(g, repo, mid, n,
                          {"x.txt": "V"})["verdict"] == "pass"
        # 手工抹掉 source 键模拟旧协议快照
        with repo.tx() as cur:
            row = cur.execute(
                "SELECT id, files_json FROM snapshots WHERE node_id=?",
                (n,)).fetchone()
            import json as _j
            files = _j.loads(row["files_json"])
            for f in files:
                f.pop("source", None)
            cur.execute("UPDATE snapshots SET files_json=? WHERE id=?",
                        (_j.dumps(files), row["id"]))
        snap = _snap_of(repo, n)
        assert lineage_of(snap, "x.txt")["origin"] == "legacy"
    finally:
        repo.close()


# ---- T4：身份约束的回执合并（Codex t-review：负值/非法时间/错归属静默入库）----

def test_t4_contract_validations(tmp_path):
    """正例：正确归属 + 合法回执（0 是合法计量值）→ contract_status=valid。"""
    from dag_agent.executor_contract import merge_executor_resources
    repo, g = _env(tmp_path)
    try:
        mid, root = g.create_mission("T4", root_check=CHK)
        n = g.add_node(mid, "A", BRIEF, parent_id=root, check_cmd=CHK,
                       declared_artifacts=("x.txt",))
        aid = g.start_attempt(mid, n, backend="stub")
        r = merge_executor_resources(repo, mid, n, aid, {
            "origin": "zcode-subagent", "agent_id": "agent_x",
            "tokens": 0, "duration_ms": 0, "cost_currency": "local",
            "executor_started_at": "2026-10-06T12:00:00+08:00",
            "executor_ended_at": "2026-10-06T12:01:00+08:00"})
        assert r["contract_status"] == "valid"
        assert "tokens" in r["contract_fields"]
    finally:
        repo.close()


def test_t4_rejects_bad_values_and_wrong_owner(tmp_path):
    from dag_agent.executor_contract import (ContractError,
                                             merge_executor_resources)
    repo, g = _env(tmp_path)
    try:
        mid, root = g.create_mission("T4b", root_check=CHK)
        other_mid, other_root = g.create_mission("别的", root_check=CHK)
        n = g.add_node(mid, "A", BRIEF, parent_id=root, check_cmd=CHK,
                       declared_artifacts=("x.txt",))
        n_other = g.add_node(other_mid, "B", BRIEF, parent_id=other_root,
                             check_cmd=CHK, declared_artifacts=("x.txt",))
        aid = g.start_attempt(mid, n, backend="stub")
        base = {"origin": "zcode-subagent", "agent_id": "agent_x"}
        # 负值
        with pytest.raises(ContractError, match="非负"):
            merge_executor_resources(repo, mid, n, aid,
                                     {**base, "tokens": -1,
                                      "duration_ms": -20})
        # 非法时间
        with pytest.raises(ContractError, match="不可解析"):
            merge_executor_resources(repo, mid, n, aid,
                                     {**base,
                                      "executor_started_at": "not-a-time",
                                      "executor_ended_at": "also-bad"})
        # 逆序时间
        with pytest.raises(ContractError, match="逆序"):
            merge_executor_resources(repo, mid, n, aid,
                                     {**base,
                                      "executor_started_at":
                                          "2026-10-06T12:01:00+08:00",
                                      "executor_ended_at":
                                          "2026-10-06T12:00:00+08:00"})
        # 错归属（用另一任务的 mid/nid 改本 attempt）
        with pytest.raises(ContractError, match="归属错配"):
            merge_executor_resources(repo, other_mid, n_other, aid, base)
        # 平台控制字段覆盖
        with pytest.raises(ContractError, match="平台控制字段"):
            merge_executor_resources(repo, mid, n, aid,
                                     {**base, "goal_version": 99})
        # 全部失败后数据未变：goal_version 等平台字段原样
        rj = json.loads(repo.get_attempt(aid).resources_json or "{}")
        assert "goal_version" not in rj or isinstance(rj["goal_version"], int)
        assert "agent_x" not in rj.get("agent_id", "")
        # 缺时序兼容：legacy_incomplete + 告警事件
        r = merge_executor_resources(repo, mid, n, aid, base)
        assert r["contract_status"] == "legacy_incomplete"
        with repo.tx() as cur:
            ev = cur.execute(
                "SELECT COUNT(*) AS c FROM events WHERE type=? AND node_id=?",
                ("executor_contract_warning", n)).fetchone()["c"]
        assert ev >= 1
    finally:
        repo.close()


# ---- T3：staging 包含检查确定性（形态翻转根因）----

def test_t3_within_is_deterministic_across_mkdir(tmp_path):
    """staging 目录 mkdir 前后，_within(staging/x, staging) 必须一致为 True。

    归因口径（Codex t-fix-review F5 后降级）：resolve 形态翻转是候选原因
    （分析性归因，未构造确定性复现）；本测试锁定双道行为契约——词法道
    （确定性组件比较）+ realpath 道（链接边界）。稳定性继续观察。"""
    import os
    from dag_agent.artifacts import _within
    staging = tmp_path / "accepted" / "nX" / ".staging" / "a-1-abc"
    dest = staging / "impl.txt"
    assert not staging.exists()
    assert _within(dest, staging), "目录不存在时不得误判越界"
    staging.mkdir(parents=True)
    (staging / "first.txt").write_text("1", encoding="utf-8")
    assert _within(dest, staging), "目录存在后不得因形态翻转误判越界"
    assert not _within(tmp_path / "sibling", staging), "兄弟目录须拒"
    # .. 段：abspath 的 normpath 会折叠 staging/.. ——结果在 staging 之外
    assert not _within(staging / ".." / "esc", staging), ".. 逃逸须拒"


def test_t3_within_blocks_junction_escape(tmp_path):
    """F2 回归（Codex t-fix-review）：目录联接词法在内、实指在外——
    第二道 realpath 展开后必须拒绝；播种不得写出到目标根。"""
    import subprocess
    from dag_agent.artifacts import ArtifactStore, _within
    base = tmp_path / "jf"
    (base / "dest" / "inputs").mkdir(parents=True)
    (base / "outside").mkdir()
    (base / "outside" / "sentinel.txt").write_text("KEEP", encoding="utf-8")
    r = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(base / "dest" / "inputs" / "n-src"),
         str(base / "outside")], capture_output=True)
    if r.returncode != 0:
        pytest.skip("本环境无法创建 junction")
    link = base / "dest" / "inputs" / "n-src"
    # 词法在内、实指在外 → 双道必须拒
    assert not _within(link / "out.txt", base / "dest"), \
        "junction 逃逸必须被实际解析道拦截"
    # 真实播种不得写出 outside（sentinel 不得被动）
    import hashlib
    import json as _json
    import types
    up_v = tmp_path / "accepted" / "n-src" / "v1"
    up_v.mkdir(parents=True)
    (up_v / "out.txt").write_text("PAYLOAD", encoding="utf-8")
    fake = types.SimpleNamespace(
        node_id="n-src", artifact_version=1,
        files_json=_json.dumps([{"rel_path": "out.txt",
                                 "sha256": hashlib.sha256(
                                     b"PAYLOAD").hexdigest(), "size": 7}]))
    store = ArtifactStore(tmp_path)
    with pytest.raises(ValueError):
        store.seed_from_snapshot(fake, base / "dest")
    assert not (base / "outside" / "out.txt").is_file(), "不得写出到目标根"
    assert (base / "outside" / "sentinel.txt").read_text(
        encoding="utf-8") == "KEEP"


def test_t3_staging_multi_file_after_first_mkdir(tmp_path):
    """多件申报：第二件文件的越界检查发生在 staging 目录已被首件 mkdir
    之后——Codex 偶发失败的确定性复现位（修复后必须稳定通过）。"""
    from dag_agent.artifacts import ArtifactStore
    store = ArtifactStore(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    for name in ("a.txt", "impl.txt", "z.txt"):
        (work / name).write_text("x", encoding="utf-8")
    staging, rows = store.stage_prepared(
        "m", "nX", "a-1", work, ["a.txt", "impl.txt", "z.txt"])
    assert len(rows) == 3 and staging.is_dir()


# ---- F1/F3/F4（Codex t-fix-review 第二轮反例转回归）----

def test_f1_pyw_entry_must_be_covered(tmp_path):
    """F1：维修换 .pyw 入口（Python 可执行的非 .py 后缀）——命令引用的
    本地文件 token 必须被清单覆盖，覆盖不了拒绝维修。"""
    old = tmp_path / "old.py"
    new = tmp_path / "new.pyw"
    old.write_text(GOOD, encoding="utf-8")
    new.write_text(GOOD, encoding="utf-8")
    repo, g = _env(tmp_path)
    try:
        mid, root = g.create_mission("F1", root_check=CHK)
        n = g.add_node(mid, "A", BRIEF, parent_id=root,
                       check_cmd=f'python "{old}"',
                       declared_artifacts=("x.txt",), exam_files=[str(old)])
        with pytest.raises(GraphError, match="实际入口不在显式清单内"):
            g.repair_check(mid, n, new_check_cmd=f'python "{new}"',
                           reason="F1 pyw 不带清单")
        # 同步清单后成功，且篡改 new.pyw 必拒
        g.repair_check(mid, n, new_check_cmd=f'python "{new}"',
                       reason="F1 pyw 带清单", exam_files=[str(new)])
        aid = g.start_attempt(mid, n, backend="stub")
        wd = Path(repo.get_attempt(aid).workdir)
        (wd / "x.txt").write_text("BAD", encoding="utf-8")
        new.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        with pytest.raises(GraphError, match="考卷内容已变更"):
            g.submit_attempt(mid, n, aid, artifact_paths=["x.txt"])
    finally:
        repo.close()


def test_f3_merged_state_validations(tmp_path):
    """F3：分次更新不得存入逆序时间（校验合并后最终值）；NaN/Inf 拒绝；
    仅时间字段无核心字段=incomplete。"""
    from dag_agent.executor_contract import (ContractError,
                                             merge_executor_resources)
    repo, g = _env(tmp_path)
    try:
        mid, root = g.create_mission("F3", root_check=CHK)
        n = g.add_node(mid, "A", BRIEF, parent_id=root, check_cmd=CHK,
                       declared_artifacts=("x.txt",))
        aid = g.start_attempt(mid, n, backend="stub")
        full = {"origin": "zcode-subagent", "agent_id": "a1",
                "tokens": 100, "duration_ms": 1000,
                "cost_currency": "local",
                "executor_started_at": "2026-10-06T12:00:00+08:00",
                "executor_ended_at": "2026-10-06T12:01:00+08:00"}
        assert merge_executor_resources(
            repo, mid, n, aid, full)["contract_status"] == "valid"
        # 分次把 end 改到 start 前 → 拒绝且库不变
        with pytest.raises(ContractError, match="逆序"):
            merge_executor_resources(
                repo, mid, n, aid,
                {"executor_ended_at": "2026-10-06T11:00:00+08:00"})
        rj = json.loads(repo.get_attempt(aid).resources_json or "{}")
        assert rj["executor_ended_at"].startswith("12:01".rjust(8, "0")) or \
            "12:01" in rj["executor_ended_at"], "拒绝后库中值不得被改"
        # NaN / Infinity
        n2 = g.add_node(mid, "B", BRIEF, parent_id=root, check_cmd=CHK,
                        declared_artifacts=("y.txt",))
        aid2 = g.start_attempt(mid, n2, backend="stub")
        with pytest.raises(ContractError, match="有限数值"):
            merge_executor_resources(
                repo, mid, n2, aid2,
                {"tokens": float("nan"), "duration_ms": float("inf")})
        # 仅时间字段、缺核心字段 → incomplete 而非 valid
        r = merge_executor_resources(
            repo, mid, n2, aid2,
            {"executor_started_at": "2026-10-06T12:00:00+08:00",
             "executor_ended_at": "2026-10-06T12:01:00+08:00"})
        assert r["contract_status"] == "legacy_incomplete"
        assert r.get("missing_core")
        # 多次补齐后 valid
        r = merge_executor_resources(
            repo, mid, n2, aid2,
            {"origin": "zcode-subagent", "agent_id": "a2",
             "tokens": 10, "duration_ms": 5, "cost_currency": "local"})
        assert r["contract_status"] == "valid"
    finally:
        repo.close()


def test_f4_manifest_hash_versioned_compat(tmp_path):
    """F4：四类 source 摘要——无 source/旧三段/新四段/歧义（含 file 绑定）。"""
    import hashlib
    from dag_agent.artifacts import manifest_hash_of
    no_src = [{"rel_path": "a.txt", "sha256": "h1", "size": 1}]
    assert manifest_hash_of(no_src) == hashlib.sha256(
        b"a.txt:h1:1").hexdigest(), "无 source 口径不得漂移"
    legacy = [{"rel_path": "a.txt", "sha256": "h1", "size": 1,
               "source": {"node": "n2", "version": 1, "file": "a.txt"}}]
    assert manifest_hash_of(legacy) == hashlib.sha256(
        b"a.txt:h1:1:n2@1:a.txt").hexdigest(), "旧三段格式重算必须稳定"
    modern = [{"rel_path": "a.txt", "sha256": "h1", "size": 1,
               "source": {"node": "n2", "version": 1, "file": "a.txt",
                          "basis": "path+sha"}}]
    assert manifest_hash_of(modern) == hashlib.sha256(
        b"a.txt:h1:1:n2@1:a.txt:path+sha").hexdigest()
    amb = [{"rel_path": "z.txt", "sha256": "h9", "size": 9,
            "source": {"origin": "ambiguous", "candidates": [
                {"node": "nA", "version": 1, "file": "a.txt"},
                {"node": "nB", "version": 2, "file": "b.txt"}]}}]
    amb2 = json.loads(json.dumps(amb))
    amb2[0]["source"]["candidates"][0]["file"] = "NEVER_EXISTED.txt"
    assert manifest_hash_of(amb) != manifest_hash_of(amb2), \
        "候选文件名变化必须改变摘要"


# ---- 成本上限默认不设（小孟 2026-10-06 决定）----

def test_budget_default_has_no_cost_cap():
    """默认构造的 Budget 不设成本上限：费用增长不受限（次数上限与
    成本上限是两道闸——按决定默认只保留次数保险丝）。显式传入时
    成本熔断仍生效（机制保留）。"""
    from dag_agent.scheduler import Budget
    b = Budget(max_calls=5)                 # 未传 max_cost_usd
    b.cost_used = 9999.0                    # 巨额已用
    ok, why = b.allow()
    assert ok, f"默认不得因费用熔断：{why}"
    b2 = Budget(max_calls=5, max_cost_usd=1.0)   # 显式传入仍生效
    b2.cost_used = 2.0
    ok2, why2 = b2.allow()
    assert not ok2 and "费用熔断" in why2


def test_budget_default_has_no_call_cap():
    """次数上限默认同样不设（小孟 2026-10-06 决定延伸：费马级 511 节点
    大图下固定默认 32 会中途误杀）；显式传入时次数熔断仍生效。"""
    from dag_agent.scheduler import Budget
    b = Budget()                          # 全默认：次数/成本均不限
    b.calls_used = 10_000
    ok, why = b.allow()
    assert ok, why
    b2 = Budget(max_calls=2)              # 显式传入仍熔断
    b2.calls_used = 2
    ok2, why2 = b2.allow()
    assert not ok2 and "调用次数熔断" in why2
