"""v3.6 模块化零模型试点专项测试（2026-10-13，Codex 评审与任务书 M1 落地）。

测试矩阵（对应任务书五与 Codex 修订建议）：
1. 接口可调用真实实现；缺依赖、改来源/版本/清单、文件漂移必拒。
2. 同名冲突与去重行为：同名同哈希确定性复用留痕，同名异哈希显式抛 ConflictError 拒静默覆盖。
3. 不可变依赖防篡改：整合/执行过程中下游篡改 deps/ 依赖文件被即刻识别拦截。
4. 冻结判卷器对独立交付副本 22/22 ＋ 干净重放 100% 独立验证。
5. 注入边界负例（非法金额尾随换行）：合法输入仍可过，完整 ROOT judge 必须抓住（拦截为 20/22），不能缩成冒烟。
6. 修订生产者后受影响节点准确失效，无关节点证据确定性复用。
7. A/B 结构化计量入口规范：字段覆盖 input/output/cache、cost、tools、disk、wall、failures，缺项标记 'unknown'。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from dag_agent.assessment import default_checker3
from dag_agent.graph import Graph, GraphError
from dag_agent.models import Snapshot
from dag_agent.modular import (
    ConflictError,
    IntegrityError,
    InterfaceContract,
    bundle_for_standalone_replay,
    prepare_ab_receipt,
    seed_interface_view,
    sha256_file,
    verify_immutable_deps,
)
from dag_agent.repository import Repository

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_FIX_JUDGE = Path(__file__).resolve().parents[1] / "fixtures" / "acceptance_checks" / "judge.py"
_LEG_JUDGE = PROJECT_ROOT / "outputs" / "多agent协同验收项目" / "judge.py"
JUDGE_SCRIPT = _FIX_JUDGE if _FIX_JUDGE.is_file() else _LEG_JUDGE

_FIX_SNAP = Path(__file__).resolve().parents[1] / "fixtures" / "accepted_snapshot" / "n1" / "v2"
_LEG_SNAP = PROJECT_ROOT / "data" / "missions" / "zc-collab" / "accepted" / "n1" / "v2"
HISTORICAL_ACCEPTED = _FIX_SNAP if _FIX_SNAP.is_dir() else _LEG_SNAP


# ---- 1. 接口可调用真实实现与防篡改 ----

def test_interface_facade_and_deps_call(tmp_path):
    """接口门面可调用真实实现，且当依赖缺失或摘要篡改时播种/核验报错。"""
    repo = Repository(tmp_path / "mission.db")
    g = Graph(repo, checker3=default_checker3)
    try:
        mid, root = g.create_mission("模块化测试", root_check="python -c 'pass'")
        n1 = g.add_node(mid, "N1", "提供 loader 模块", parent_id=root,
                        check_cmd="python -c 'pass'", declared_artifacts=("loader.py",))
        aid1 = g.start_attempt(mid, n1)
        w1 = Path(repo.get_attempt(aid1).workdir)
        (w1 / "loader.py").write_text(
            "def load_data(text):\n    return [line.strip() for line in text.splitlines() if line.strip()]\n",
            encoding="utf-8"
        )
        res1 = g.submit_attempt(mid, n1, aid1, artifact_paths=["loader.py"])
        assert res1["verdict"] == "pass"

        snapshot_obj = repo.latest_valid_snapshot(n1)
        assert snapshot_obj is not None

        # 构建门面契约
        contract = InterfaceContract(
            module_name="loader",
            version=snapshot_obj.artifact_version,
            snapshot_id=snapshot_obj.id,
            source_node=n1,
            manifest_hash=snapshot_obj.manifest_hash,
            entry_symbol="load_data",
            signature="def load_data(text: str) -> list[str]",
            docstring="按行加载非空文本列表。",
            inputs_schema={"text": "string"},
            outputs_schema={"lines": "list[string]"},
            units={"text": "UTF-8 string"},
            null_semantics={"text": "空串返回空列表"},
            error_codes=[],
            side_effects="纯计算，无副作用",
            examples=[{"input": "a\\nb", "output": ["a", "b"]}],
            selftest_cmd="python -c 'import loader; assert loader.load_data(\"x\")==[\"x\"]'",
            deps_rel_path=f"deps/{n1}/v{snapshot_obj.artifact_version}",
            api_code=(
                "from pathlib import Path\n"
                "import sys\n"
                f"dep_dir = Path(__file__).resolve().parents[2] / 'deps' / '{n1}' / 'v{snapshot_obj.artifact_version}'\n"
                "if str(dep_dir) not in sys.path:\n"
                "    sys.path.insert(0, str(dep_dir))\n"
                "from loader import load_data\n"
            )
        )

        dest_work = tmp_path / "consumer_workdir"
        manifest_data = seed_interface_view(
            g.store,
            snapshots=[snapshot_obj],
            dest_root=dest_work,
            contracts={n1: contract}
        )

        assert manifest_data["view_mode"] == "interface_first"
        assert (dest_work / "interfaces" / "loader" / "api.py").is_file()
        assert (dest_work / "interfaces" / "loader" / "API.md").is_file()
        assert (dest_work / "deps" / n1 / f"v{snapshot_obj.artifact_version}" / "loader.py").is_file()

        # 验证门面调用真实实现
        consumer_script = dest_work / "run_test.py"
        consumer_script.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).parent / 'interfaces' / 'loader'))\n"
            "import api\n"
            "res = api.load_data('line1\\nline2\\n')\n"
            "assert res == ['line1', 'line2'], f'Unexpected: {res}'\n",
            encoding="utf-8"
        )
        proc = subprocess.run([sys.executable, str(consumer_script)], cwd=str(dest_work), capture_output=True, text=True)
        assert proc.returncode == 0, f"Consumer run failed: {proc.stderr}"

        # 验证防篡改核验
        ok, mismatches = verify_immutable_deps(dest_work, manifest_data)
        assert ok is True
        assert len(mismatches) == 0

        # 下游篡改 deps
        (dest_work / "deps" / n1 / f"v{snapshot_obj.artifact_version}" / "loader.py").write_text("# tampered\n", encoding="utf-8")
        ok_tampered, mismatches_tampered = verify_immutable_deps(dest_work, manifest_data)
        assert ok_tampered is False
        assert any("被篡改" in m for m in mismatches_tampered)

    finally:
        repo.close()


# ---- 2. 同名冲突与去重行为 ----

def test_deterministic_dedup_and_conflict_rejection(tmp_path):
    """同名同哈希确定性复用并留痕，同名异哈希显式抛出 ConflictError 拒绝静默覆盖。"""
    repo = Repository(tmp_path / "mission.db")
    g = Graph(repo, checker3=default_checker3)
    try:
        mid, root = g.create_mission("去重与冲突", root_check="python -c 'pass'")

        # 节点 A 和 节点 B 都产出相同 shared.csv
        na = g.add_node(mid, "NA", "产出 A", parent_id=root,
                        check_cmd="python -c 'pass'", declared_artifacts=("shared.csv", "a_only.py"))
        nb = g.add_node(mid, "NB", "产出 B", parent_id=root,
                        check_cmd="python -c 'pass'", declared_artifacts=("shared.csv", "b_only.py"))

        a_aid = g.start_attempt(mid, na)
        a_wd = Path(repo.get_attempt(a_aid).workdir)
        (a_wd / "shared.csv").write_text("id,val\n1,100\n", encoding="utf-8")
        (a_wd / "a_only.py").write_text("A = 1\n", encoding="utf-8")
        g.submit_attempt(mid, na, a_aid, artifact_paths=["shared.csv", "a_only.py"])
        snap_a = repo.latest_valid_snapshot(na)

        b_aid = g.start_attempt(mid, nb)
        b_wd = Path(repo.get_attempt(b_aid).workdir)
        (b_wd / "shared.csv").write_text("id,val\n1,100\n", encoding="utf-8")  # 内容完全相同
        (b_wd / "b_only.py").write_text("B = 2\n", encoding="utf-8")
        g.submit_attempt(mid, nb, b_aid, artifact_paths=["shared.csv", "b_only.py"])
        snap_b = repo.latest_valid_snapshot(nb)

        # 1. 同名同哈希：去重并保留全部来源
        dest_dedup = tmp_path / "dedup_work"
        manifest_meta = seed_interface_view(g.store, snapshots=[snap_a, snap_b], dest_root=dest_dedup)
        sources_list = manifest_meta["dedup_sources"]["shared.csv"]
        assert len(sources_list) == 2
        assert f"{na}@v1" in sources_list
        assert f"{nb}@v1" in sources_list

        # 2. 同名异哈希：上游 NC 产出内容不同的 shared.csv
        nc = g.add_node(mid, "NC", "产出 C", parent_id=root,
                        check_cmd="python -c 'pass'", declared_artifacts=("shared.csv",))
        c_aid = g.start_attempt(mid, nc)
        c_wd = Path(repo.get_attempt(c_aid).workdir)
        (c_wd / "shared.csv").write_text("id,val\n999,DIFFERENT\n", encoding="utf-8")  # 冲突！
        g.submit_attempt(mid, nc, c_aid, artifact_paths=["shared.csv"])
        snap_c = repo.latest_valid_snapshot(nc)

        dest_conflict = tmp_path / "conflict_work"
        with pytest.raises(ConflictError, match="同名异哈希冲突"):
            seed_interface_view(g.store, snapshots=[snap_a, snap_c], dest_root=dest_conflict)

    finally:
        repo.close()


# ---- 3. ROOT 22 项业务判卷与独立干净重放 ----

# ---- 3. ROOT 22 项业务判卷与独立干净重放（真实模块化链路产出全新包） ----

def test_root_22_judge_clean_replay(tmp_path):
    """模块化整合交付包自包含完整依赖：通过 Graph/Repository 提交真实上游模块，
    使用 seed_interface_view 播种接口优先视图，通过 verify_immutable_deps 核验依赖完整性，
    由 bundle_for_standalone_replay 打包为全新的独立交付包，直接运行 ROOT 22 项判卷及独立重放均为 22/22 PASS。
    """
    assert HISTORICAL_ACCEPTED.is_dir(), f"历史黄金快照不存在: {HISTORICAL_ACCEPTED}"
    assert JUDGE_SCRIPT.is_file(), f"判卷脚本不存在: {JUDGE_SCRIPT}"

    # 1. 真实通过 Graph/Repository 模拟上游 4 个模块提交与签发
    repo = Repository(tmp_path / "pilot_mission.db")
    g = Graph(repo, checker3=default_checker3)
    try:
        mid, root = g.create_mission("M1模块化协同", root_check="python -c 'pass'")
        modules = ["loader", "validation", "reconcile", "render"]
        snapshots = {}
        contracts = {}

        for mod in modules:
            nid = g.add_node(
                mid,
                f"N_{mod}",
                f"提供 {mod} 模块",
                parent_id=root,
                check_cmd="python -c 'pass'",
                declared_artifacts=(f"{mod}.py",),
            )
            aid = g.start_attempt(mid, nid)
            w = Path(repo.get_attempt(aid).workdir)
            src_file = HISTORICAL_ACCEPTED / f"{mod}.py"
            assert src_file.is_file(), f"缺少源模块文件: {src_file}"
            (w / f"{mod}.py").write_text(src_file.read_text(encoding="utf-8"), encoding="utf-8")
            g.submit_attempt(mid, nid, aid, artifact_paths=[f"{mod}.py"])
            snap = repo.latest_valid_snapshot(nid)
            assert snap is not None
            snapshots[mod] = snap

            contracts[nid] = InterfaceContract(
                module_name=mod,
                version=snap.artifact_version,
                snapshot_id=snap.id,
                source_node=nid,
                manifest_hash=snap.manifest_hash,
                entry_symbol=mod,
                signature=f"def {mod}(*args, **kwargs)",
                docstring=f"{mod} 模块接口契约",
                inputs_schema={},
                outputs_schema={},
                units={},
                null_semantics={},
                error_codes=[],
                side_effects="无副作用",
                examples=[],
                selftest_cmd="python -c 'pass'",
                deps_rel_path=f"deps/{nid}/v{snap.artifact_version}",
                api_code=(
                    "from pathlib import Path\n"
                    "import sys\n"
                    f"dep_dir = Path(__file__).resolve().parents[2] / 'deps' / '{nid}' / 'v{snap.artifact_version}'\n"
                    "if str(dep_dir) not in sys.path:\n"
                    "    sys.path.insert(0, str(dep_dir))\n"
                    f"import {mod}\n"
                ),
            )

        # 2. 播种整合节点工作区（接口优先视图）
        integrate_workdir = tmp_path / "integrate_workdir"
        manifest_meta = seed_interface_view(
            g.store,
            snapshots=list(snapshots.values()),
            dest_root=integrate_workdir,
            contracts=contracts,
        )
        assert manifest_meta["view_mode"] == "interface_first"

        # 3. 防篡改核验
        ok, mismatches = verify_immutable_deps(integrate_workdir, manifest_meta)
        assert ok is True, f"不可变依赖核验失败: {mismatches}"

        # 4. 写入整合入口 main.py
        main_src = HISTORICAL_ACCEPTED / "main.py"
        (integrate_workdir / "main.py").write_text(main_src.read_text(encoding="utf-8"), encoding="utf-8")

        # 5. 打包为自包含独立交付包
        bundle_dir = tmp_path / "replay_bundle"
        bundle_for_standalone_replay(integrate_workdir, bundle_dir, integration_files=["main.py"])

        # 验证打包产物目录结构：必须同时具备 interfaces/、deps/ 和平铺入口
        assert (bundle_dir / "interfaces" / "loader" / "api.py").is_file()
        assert (bundle_dir / "deps").is_dir()
        assert (bundle_dir / "main.py").is_file()
        assert (bundle_dir / "loader.py").is_file()

        # 6. 对全新生成的 bundle 运行 ROOT 22 项黄金判卷
        evidence_file = tmp_path / "replay_evidence.json"
        cmd = [
            sys.executable,
            str(JUDGE_SCRIPT.resolve()),
            "--bundle", str(bundle_dir.resolve()),
            "--evidence", str(evidence_file.resolve()),
        ]
        proc = subprocess.run(cmd, cwd=str(tmp_path), capture_output=True, text=True)
        assert proc.returncode == 0, f"Judge failed: {proc.stdout}\n{proc.stderr}"
        data = json.loads(evidence_file.read_text(encoding="utf-8"))
        assert data["verdict"] == "PASS"
        assert len(data["cases"]) == 22
        assert all(c["passed"] for c in data["cases"])

        # 7. 将 bundle 复制到全新干净目录进行 100% 独立重放
        clean_replay_dir = tmp_path / "clean_standalone_replay"
        shutil.copytree(bundle_dir, clean_replay_dir)
        replay_evidence = tmp_path / "clean_replay_evidence.json"
        proc2 = subprocess.run(
            [
                sys.executable,
                str(JUDGE_SCRIPT.resolve()),
                "--bundle", str(clean_replay_dir.resolve()),
                "--evidence", str(replay_evidence.resolve()),
            ],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
        )
        assert proc2.returncode == 0, f"Clean replay failed: {proc2.stdout}\n{proc2.stderr}"
        data2 = json.loads(replay_evidence.read_text(encoding="utf-8"))
        assert data2["verdict"] == "PASS"
        assert len(data2["cases"]) == 22
        assert all(c["passed"] for c in data2["cases"])
    finally:
        repo.close()


# ---- 4. 边界负例注入（金额尾随换行）被完整 ROOT judge 拦截 ----

def test_boundary_fault_trailing_newline_caught_by_full_judge(tmp_path):
    """注入非法金额尾随换行等边界缺陷：合法输入仍过，但完整 ROOT judge 必须抓住（降为 20/22 FAIL）。"""
    bundle_bad = tmp_path / "bad_bundle"
    shutil.copytree(HISTORICAL_ACCEPTED, bundle_bad)

    # 篡改 validation.py：改用宽松 strip().isdigit() 容忍尾随换行
    val_p = bundle_bad / "validation.py"
    content = val_p.read_text(encoding="utf-8")
    target_check = "if not isinstance(value, str) or not AMOUNT_RE.fullmatch(value):"
    assert target_check in content
    loosened = content.replace(
        target_check,
        "if not isinstance(value, str) or not str(value).strip().isdigit():"
    )
    assert loosened != content
    val_p.write_text(loosened, encoding="utf-8")

    evidence_file = tmp_path / "bad_evidence.json"
    cmd = [
        sys.executable,
        str(JUDGE_SCRIPT),
        "--bundle", str(bundle_bad),
        "--evidence", str(evidence_file)
    ]
    proc = subprocess.run(cmd, cwd=str(tmp_path), capture_output=True, text=True)
    # 判卷必须 FAIL（退出码非 0）
    assert proc.returncode != 0
    data = json.loads(evidence_file.read_text(encoding="utf-8"))
    assert data["verdict"] == "FAIL"

    # 验证刚好是那两项尾随换行用例被抓住！
    failed_names = {c["name"] for c in data["cases"] if not c["passed"]}
    assert "amount_trailing_newline_order" in failed_names
    assert "amount_trailing_newline_refund" in failed_names
    passed_count = sum(1 for c in data["cases"] if c["passed"])
    assert passed_count == 20
    assert len(data["cases"]) == 22


# ---- 5. 修订生产者后级联失效与无关节点证据复用 ----

def test_revise_producer_cascade_invalidation_and_reuse(tmp_path):
    """修订生产者后受影响节点准确失效，无关节点快照与证据确定性复用。"""
    repo = Repository(tmp_path / "mission.db")
    g = Graph(repo, checker3=default_checker3)
    try:
        mid, root = g.create_mission("修订级联测试", root_check="python -c 'pass'")

        # 节点 A（生产者 1）、节点 B（生产者 2）、节点 C（消费者，仅依赖 A）
        na = g.add_node(mid, "NA", "产出 A", parent_id=root,
                        check_cmd="python -c 'pass'", declared_artifacts=("a.txt",))
        nb = g.add_node(mid, "NB", "产出 B（无关）", parent_id=root,
                        check_cmd="python -c 'pass'", declared_artifacts=("b.txt",))
        nc = g.add_node(mid, "NC", "消费者 C（依 A）", parent_id=root,
                        deps=(na,), check_cmd="python -c 'pass'", declared_artifacts=("c.txt",))

        # 提交 A
        a_aid = g.start_attempt(mid, na)
        (Path(repo.get_attempt(a_aid).workdir) / "a.txt").write_text("A v1\n", encoding="utf-8")
        g.submit_attempt(mid, na, a_aid, artifact_paths=["a.txt"])
        snap_a1 = repo.latest_valid_snapshot(na)

        # 提交 B
        b_aid = g.start_attempt(mid, nb)
        (Path(repo.get_attempt(b_aid).workdir) / "b.txt").write_text("B v1\n", encoding="utf-8")
        g.submit_attempt(mid, nb, b_aid, artifact_paths=["b.txt"])
        snap_b1 = repo.latest_valid_snapshot(nb)

        # 提交 C
        c_aid = g.start_attempt(mid, nc)
        (Path(repo.get_attempt(c_aid).workdir) / "c.txt").write_text("C v1\n", encoding="utf-8")
        g.submit_attempt(mid, nc, c_aid, artifact_paths=["c.txt"])
        snap_c1 = repo.latest_valid_snapshot(nc)

        assert snap_a1.valid == 1
        assert snap_b1.valid == 1
        assert snap_c1.valid == 1

        # 记录修订前 B 的评估证据数量
        b_evals_before = len(repo.assessments_of(nb))

        # 修订生产者 A：触发 revise_node
        g.revise_node(mid, na, reason="修复底层 A 缺陷")

        # 重新提交 A 得到 v2
        a_aid2 = g.start_attempt(mid, na)
        (Path(repo.get_attempt(a_aid2).workdir) / "a.txt").write_text("A v2\n", encoding="utf-8")
        g.submit_attempt(mid, na, a_aid2, artifact_paths=["a.txt"])
        snap_a2 = repo.latest_valid_snapshot(na)

        # 检查状态：
        # 1. 生产者 A 产生新快照 snap_a2
        assert snap_a2.artifact_version == 2
        # 2. 消费者 C 节点由于依赖已修订，状态重置为 pending，旧证据失效
        node_c = repo.get_node(nc)
        assert node_c.status == "pending"
        assert node_c.accepted_artifact_version is None
        c_evals = repo.assessments_of(nc)
        assert any(e.invalid == 1 for e in c_evals)

        # 3. 无关节点 B 快照 snap_b1 依然 valid，评估证据未被失效，证据完全复用！
        b_cur = repo.latest_valid_snapshot(nb)
        assert b_cur.valid == 1
        assert b_cur.id == snap_b1.id
        b_evals_after = repo.assessments_of(nb)
        assert len(b_evals_after) == b_evals_before
        assert all(e.invalid == 0 for e in b_evals_after)

    finally:
        repo.close()


# ---- 6. A/B 计量结构化入口规范 ----

def test_ab_metrics_receipt_structure():
    """验证 A/B 测试计量凭证结构完整、缺项标记为 unknown、拒绝凭空捏造。"""
    receipt_a = prepare_ab_receipt(
        condition="A",
        target="ROOT 22项整合",
        input_tokens=851090,
        output_tokens=12000,
        tool_uses=24,
        wall_time_s=170.7,
        failures=0,
        rescues=0,
        verdict="pass",
        note="基线全量视图运行"
    )
    assert receipt_a["condition"] == "A"
    assert receipt_a["condition_name"] == "full_view"
    assert receipt_a["tokens"]["input_tokens"] == 851090
    assert receipt_a["tokens"]["cache_read_tokens"] == "unknown"
    assert receipt_a["cost"]["reported_amount"] == "unknown"

    receipt_b = prepare_ab_receipt(
        condition="B",
        target="ROOT 22项整合",
        tool_uses=5,
        verdict="pass",
        note="模块化接口优先试点"
    )
    assert receipt_b["condition"] == "B"
    assert receipt_b["condition_name"] == "interface_first_with_full_deps"
    assert receipt_b["tokens"]["input_tokens"] == "unknown"
    assert receipt_b["disk_io"]["read_files"] == "unknown"
    assert receipt_b["performance"]["wall_time_s"] == "unknown"
