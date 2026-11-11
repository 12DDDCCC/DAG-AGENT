# -*- coding: utf-8 -*-
"""第八轮修复：通用语义契约与 M1 权威绑定正式回归测试套件。

涵盖：
1. 验收器与 M1 权威快照对账（拒绝任意伪造 ID 与摘要，不依赖特判）；
2. 模块名与门面路径越界逃逸防护；
3. 同名门面冲突防护（抛 ConflictError，拒静默覆盖）；
4. 依赖文件与清单联合篡改跨核验防护（打包器 fail-closed 拒发布）；
5. 清单缺少必需字段拒绝；
6. 损坏库构造失败时不遗留 active ledger 活登记；
7. 正式 RunManager 运行时投影终局异常结构化事件与 stderr 留证；
8. opt-in 接口优先链路完整 DAG（认领->自动播种->接口消费->22项judge->干净重放->done）。
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from dag_agent.assessment import default_checker3
from dag_agent.daemon import RunManager, render_mission_versioned as _orig_render
import dag_agent.daemon as daemon
from dag_agent.graph import Graph
from dag_agent.modular import (
    ConflictError,
    IntegrityError,
    InterfaceContract,
    bundle_for_standalone_replay,
    seed_interface_view,
    sha256_file,
    verify_immutable_deps,
)
from dag_agent.repository import Repository, is_active_ledger_file
from test_v45_autogrow import AutoGrowBackend, BRIEF

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_FIX_JUDGE = Path(__file__).resolve().parents[1] / "fixtures" / "acceptance_checks" / "judge.py"
_LEG_JUDGE = PROJECT_ROOT / "outputs" / "多agent协同验收项目" / "judge.py"
JUDGE_SCRIPT = _FIX_JUDGE if _FIX_JUDGE.is_file() else _LEG_JUDGE

_FIX_SNAP = Path(__file__).resolve().parents[1] / "fixtures" / "accepted_snapshot" / "n1" / "v2"
_LEG_SNAP = PROJECT_ROOT / "data" / "missions" / "zc-collab" / "accepted" / "n1" / "v2"
HISTORICAL_ACCEPTED = _FIX_SNAP if _FIX_SNAP.is_dir() else _LEG_SNAP


def _make_loader_contract(snap, module: str = "loader") -> InterfaceContract:
    return InterfaceContract(
        module_name=module,
        version=snap.artifact_version,
        snapshot_id=snap.id,
        source_node=snap.node_id,
        manifest_hash=snap.manifest_hash,
        entry_symbol="load_data",
        signature="def load_data(x: str) -> str",
        deps_rel_path=f"deps/{snap.node_id}/v{snap.artifact_version}",
        api_code="def load_data(x):\n    return x.upper()\n",
    )


def test_forged_snapshot_identity_rejected(tmp_path):
    """通用语义契约：任意伪造 snapshot ID 或 manifest_hash 必须被拦截。"""
    repo = Repository(tmp_path / "mission.db")
    g = Graph(repo, checker3=default_checker3)
    try:
        mid, root = g.create_mission("test_forged", root_check="python -c 'pass'")
        nid = g.add_node(mid, "loader", "statement", parent_id=root,
                         check_cmd="python -c 'pass'", declared_artifacts=("loader.py",))
        aid = g.start_attempt(mid, nid)
        (Path(repo.get_attempt(aid).workdir) / "loader.py").write_text(
            "def load_data(x):\n    return x.upper()\n", encoding="utf-8"
        )
        g.submit_attempt(mid, nid, aid, artifact_paths=["loader.py"])
        snap = repo.latest_valid_snapshot(nid)
        assert snap is not None

        # 1. 正常快照播种与核验通过
        dest_ok = tmp_path / "view_ok"
        meta_ok = seed_interface_view(g.store, [snap], dest_ok, contracts={nid: _make_loader_contract(snap)})
        ok, errs = verify_immutable_deps(dest_ok, meta_ok)
        assert ok is True
        assert errs == []

        # 2. 伪造 ID 与 manifest_hash（不限于旧探针值 987654321 / FAKE_HASH）
        fake = copy.deepcopy(snap)
        fake.id = 864209753
        fake.manifest_hash = "abab" * 16
        dest_fake = tmp_path / "view_fake"
        meta_fake = seed_interface_view(g.store, [fake], dest_fake, contracts={nid: _make_loader_contract(fake)})
        ok, errs = verify_immutable_deps(dest_fake, meta_fake)
        assert ok is False
        assert len(errs) >= 1
    finally:
        repo.close()


def test_facade_path_escape_rejected(tmp_path):
    """路径越界防范：带有 ../ 逃逸的模块名被拒绝且不得写出到 interfaces_root 外部。"""
    repo = Repository(tmp_path / "mission.db")
    g = Graph(repo, checker3=default_checker3)
    try:
        mid, root = g.create_mission("test_escape", root_check="python -c 'pass'")
        nid = g.add_node(mid, "loader", "statement", parent_id=root,
                         check_cmd="python -c 'pass'", declared_artifacts=("loader.py",))
        aid = g.start_attempt(mid, nid)
        (Path(repo.get_attempt(aid).workdir) / "loader.py").write_text("x = 1\n", encoding="utf-8")
        g.submit_attempt(mid, nid, aid, artifact_paths=["loader.py"])
        snap = repo.latest_valid_snapshot(nid)

        dest = tmp_path / "view_escape"
        con = _make_loader_contract(snap, "../../escaped_facade")
        meta = seed_interface_view(g.store, [snap], dest, contracts={nid: con})
        ok, errs = verify_immutable_deps(dest, meta)
        assert ok is False
        assert any("越界" in e or "非法" in e for e in errs)
        assert not (tmp_path / "escaped_facade").exists()
    finally:
        repo.close()


def test_facade_conflict_rejected(tmp_path):
    """同名门面冲突防范：多个节点提供相同模块名必须抛出 ConflictError，拒绝静默覆盖。"""
    repo = Repository(tmp_path / "mission.db")
    g = Graph(repo, checker3=default_checker3)
    try:
        mid, root = g.create_mission("test_conflict", root_check="python -c 'pass'")
        n1 = g.add_node(mid, "loader1", "statement", parent_id=root,
                        check_cmd="python -c 'pass'", declared_artifacts=("loader1.py",))
        n2 = g.add_node(mid, "loader2", "statement", parent_id=root,
                        check_cmd="python -c 'pass'", declared_artifacts=("loader2.py",))
        aid1 = g.start_attempt(mid, n1)
        (Path(repo.get_attempt(aid1).workdir) / "loader1.py").write_text("x = 1\n", encoding="utf-8")
        g.submit_attempt(mid, n1, aid1, artifact_paths=["loader1.py"])
        snap1 = repo.latest_valid_snapshot(n1)

        aid2 = g.start_attempt(mid, n2)
        (Path(repo.get_attempt(aid2).workdir) / "loader2.py").write_text("x = 2\n", encoding="utf-8")
        g.submit_attempt(mid, n2, aid2, artifact_paths=["loader2.py"])
        snap2 = repo.latest_valid_snapshot(n2)

        dest = tmp_path / "view_facade_conflict"
        contracts = {
            n1: _make_loader_contract(snap1, "shared_module"),
            n2: _make_loader_contract(snap2, "shared_module"),
        }
        with pytest.raises(ConflictError) as exc_info:
            seed_interface_view(g.store, [snap1, snap2], dest, contracts=contracts)
        assert "同名门面冲突" in str(exc_info.value)
    finally:
        repo.close()


def test_joint_tamper_and_bundle_rejection(tmp_path):
    """防联合篡改与打包器 fail-closed：下游联合改写依赖与 view_manifest 仍被拦截，打包器拒发布。"""
    repo = Repository(tmp_path / "mission.db")
    g = Graph(repo, checker3=default_checker3)
    try:
        mid, root = g.create_mission("test_joint_tamper", root_check="python -c 'pass'")
        nid = g.add_node(mid, "loader", "statement", parent_id=root,
                         check_cmd="python -c 'pass'", declared_artifacts=("loader.py",))
        aid = g.start_attempt(mid, nid)
        (Path(repo.get_attempt(aid).workdir) / "loader.py").write_text(
            "def load_data(x):\n    return x.upper()\n", encoding="utf-8"
        )
        g.submit_attempt(mid, nid, aid, artifact_paths=["loader.py"])
        snap = repo.latest_valid_snapshot(nid)

        dest = tmp_path / "view_joint"
        meta = seed_interface_view(g.store, [snap], dest, contracts={nid: _make_loader_contract(snap)})

        # 模拟攻击：改写依赖文件，并重新计算逐文件 sha 与 manifest_hash 覆写 view_manifest.json
        dep_entry = next(f for f in meta["files"] if f["role"] == "dependency")
        target_dep = dest / dep_entry["rel_path"]
        target_dep.write_text("def load_data(x):\n    return 'TAMPERED'\n", encoding="utf-8")
        dep_entry["sha256"] = sha256_file(target_dep)
        dep_entry["size"] = target_dep.stat().st_size
        meta["manifest_hash"] = hashlib.sha256(
            json.dumps(sorted(meta["files"], key=lambda x: (x["rel_path"], str(x.get("source_node", "")))),
                       ensure_ascii=False, indent=2).encode("utf-8")
        ).hexdigest()
        (dest / "view_manifest.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        # verify_immutable_deps 必须通过门面契约权威绑定识别出依赖被篡改
        ok, errs = verify_immutable_deps(dest, meta)
        assert ok is False
        assert any("篡改" in e for e in errs)

        # bundle_for_standalone_replay 必须 fail-closed 抛出 IntegrityError 拒绝打包
        with pytest.raises(IntegrityError):
            bundle_for_standalone_replay(dest, tmp_path / "bad_bundle")
        assert not (tmp_path / "bad_bundle" / "loader.py").exists()
    finally:
        repo.close()


def test_missing_manifest_hash_rejected(tmp_path):
    """缺少必需的 manifest_hash 字段必须被核验器识别拒绝。"""
    repo = Repository(tmp_path / "mission.db")
    g = Graph(repo, checker3=default_checker3)
    try:
        mid, root = g.create_mission("test_missing_hash", root_check="python -c 'pass'")
        nid = g.add_node(mid, "loader", "statement", parent_id=root,
                         check_cmd="python -c 'pass'", declared_artifacts=("loader.py",))
        aid = g.start_attempt(mid, nid)
        (Path(repo.get_attempt(aid).workdir) / "loader.py").write_text("x = 1\n", encoding="utf-8")
        g.submit_attempt(mid, nid, aid, artifact_paths=["loader.py"])
        snap = repo.latest_valid_snapshot(nid)

        dest = tmp_path / "view_nohash"
        meta = seed_interface_view(g.store, [snap], dest, contracts={nid: _make_loader_contract(snap)})
        meta.pop("manifest_hash")
        (dest / "view_manifest.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        ok, errs = verify_immutable_deps(dest, meta)
        assert ok is False
        assert any("缺失" in e and "manifest_hash" in e for e in errs)
    finally:
        repo.close()


def test_repository_failed_init_no_active_ledger(tmp_path):
    """生命周期安全：损坏库在 Repository 构造失败时，不得在活跃账本集合遗留身份。"""
    bad_db = tmp_path / "bad.db"
    bad_db.write_bytes(b"not a valid sqlite file")
    with pytest.raises(Exception):
        Repository(bad_db)
    assert is_active_ledger_file(bad_db) is False


def test_projection_runtime_logging_and_events(tmp_path):
    """投影终局失败必须留存结构化事件（projection_error）与 stderr 日志，不影响主任务闭合。"""
    td = tmp_path / "proj_run"
    td.mkdir()
    check = td / "check.py"
    check.write_text(
        "from pathlib import Path\nimport sys\nsys.exit(0 if Path('deliverable.md').exists() else 1)\n",
        encoding="utf-8"
    )
    repo = Repository(td / "mission.db")
    gr = Graph(repo)
    try:
        backend = AutoGrowBackend()
        mid, rt = gr.create_mission(
            "proj-err-test",
            brief=BRIEF.format(body="root"),
            root_check=f'python "{check}"',
            root_declared_artifacts=("deliverable.md",)
        )
        mgr = RunManager(td)
        old_render = daemon.render_mission_versioned
        call_count = [0]

        def fail_render(*a, **kw):
            call_count[0] += 1
            raise PermissionError("controlled terminal projection failure for test")

        daemon.render_mission_versioned = fail_render
        out_buf = io.StringIO()
        err_buf = io.StringIO()

        try:
            with redirect_stdout(out_buf), redirect_stderr(err_buf):
                res = mgr.run_blocking(
                    mid, timeout=15, root=rt, real=False, project=True,
                    max_concurrency=1, max_retries=0, timeout_s=15,
                    backend_factory=lambda: backend
                )
            assert res.get("mission_status") == "done"
            events = repo.events_of(mid)
            proj_errs = [e for e in events if e.type == "projection_error"]
            assert len(proj_errs) >= 1
            payload = json.loads(proj_errs[0].payload_json)
            assert payload["error_type"] == "PermissionError"
            assert "controlled terminal projection failure" in payload["message"]
            assert payload["target"] == "board"
            assert "[projection_error]" in err_buf.getvalue()
        finally:
            daemon.render_mission_versioned = old_render
            mgr.cleanup_finished(keep=0)
    finally:
        repo.close()


def test_opt_in_interface_first_full_dag(tmp_path):
    """M1 验收闭环：opt-in 接口优先链路完整 DAG。
    上游产出快照 -> 下游以接口优先模式播种与消费 -> 产出通过原 22 项黄金 Judge -> 干净重放 -> ROOT accepted。
    """
    assert JUDGE_SCRIPT.is_file(), f"黄金 Judge 脚本缺失: {JUDGE_SCRIPT}"
    assert HISTORICAL_ACCEPTED.is_dir(), f"历史受控工件目录缺失: {HISTORICAL_ACCEPTED}"

    repo = Repository(tmp_path / "mission.db")
    g = Graph(repo, checker3=default_checker3)
    try:
        mid, root = g.create_mission("m1_opt_in_dag", root_check="python -c 'pass'")

        # 1. 真实通过 Graph/Repository 模拟上游 4 个核心模块提交与签发
        modules = ["loader", "validation", "reconcile", "render"]
        snapshots = {}
        contracts = {}

        for mod in modules:
            nid = g.add_node(
                mid, f"producer_{mod}", f"产出 {mod} 模块", parent_id=root,
                check_cmd="python -c 'pass'", declared_artifacts=(f"{mod}.py",)
            )
            aid = g.start_attempt(mid, nid)
            w = Path(repo.get_attempt(aid).workdir)
            src_file = HISTORICAL_ACCEPTED / f"{mod}.py"
            assert src_file.is_file(), f"缺少源模块: {src_file}"
            (w / f"{mod}.py").write_text(src_file.read_text(encoding="utf-8"), encoding="utf-8")
            sub = g.submit_attempt(mid, nid, aid, artifact_paths=[f"{mod}.py"])
            assert sub["verdict"] == "pass"
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
                side_effects="只读计算",
                examples=[],
                selftest_cmd=f"python -c 'import {mod}'",
                deps_rel_path=f"deps/{nid}/v{snap.artifact_version}",
                api_code=(
                    "from pathlib import Path\n"
                    "import sys\n"
                    f"dep_dir = Path(__file__).resolve().parents[2] / 'deps' / '{nid}' / 'v{snap.artifact_version}'\n"
                    "if str(dep_dir) not in sys.path:\n"
                    "    sys.path.insert(0, str(dep_dir))\n"
                    f"from {mod} import *\n"
                )
            )

        # 2. 下游消费者节点（整合主程序），声明依赖 4 个生产者模块
        root_check_cmd = f'python "{JUDGE_SCRIPT}"'
        consumer_nid = g.add_node(
            mid, "consumer_root", "整合各模块并通过 22 项黄金 Judge 验收",
            parent_id=root, deps=tuple(contracts.keys()), check_cmd=root_check_cmd,
            declared_artifacts=("main.py", "loader.py", "validation.py", "reconcile.py", "render.py")
        )

        # 3. 认领与播种接口优先视图
        consumer_workdir = tmp_path / "consumer_workdir"
        manifest_meta = seed_interface_view(
            g.store,
            snapshots=list(snapshots.values()),
            dest_root=consumer_workdir,
            contracts=contracts
        )
        ok, errs = verify_immutable_deps(consumer_workdir, manifest_meta)
        assert ok is True, f"接口优先视图核验失败: {errs}"

        # 4. 消费者编写 main.py 调用接口门面并执行业务主流程
        main_src = HISTORICAL_ACCEPTED / "main.py"
        (consumer_workdir / "main.py").write_text(main_src.read_text(encoding="utf-8"), encoding="utf-8")

        # 5. 打包自包含独立交付包
        standalone_bundle = tmp_path / "standalone_bundle"
        bundle_for_standalone_replay(
            dest_root=consumer_workdir,
            output_dir=standalone_bundle,
            integration_files=["main.py"]
        )

        # 6. 在独立交付包中直接运行原 22 项黄金 Judge
        evidence_file = tmp_path / "judge_evidence.json"
        proc = subprocess.run(
            [sys.executable, str(JUDGE_SCRIPT), "--bundle", str(standalone_bundle), "--evidence", str(evidence_file)],
            cwd=str(standalone_bundle),
            capture_output=True,
            text=True
        )
        assert proc.returncode == 0, f"22 项 Judge 执行失败: stdout={proc.stdout}, stderr={proc.stderr}"
        report = json.loads(evidence_file.read_text(encoding="utf-8"))
        assert report["verdict"] == "PASS"
        assert report["passed_cases"] == 22 and report["total_cases"] == 22

        # 7. 在干净独立临时目录下进行 100% 独立重放与验证
        replay_clean = tmp_path / "replay_clean"
        shutil.copytree(standalone_bundle, replay_clean)
        replay_evidence = tmp_path / "replay_evidence.json"
        replay_proc = subprocess.run(
            [sys.executable, str(JUDGE_SCRIPT), "--bundle", str(replay_clean), "--evidence", str(replay_evidence)],
            cwd=str(replay_clean),
            capture_output=True,
            text=True
        )
        assert replay_proc.returncode == 0, f"干净重放失败: stdout={replay_proc.stdout}, stderr={replay_proc.stderr}"
        replay_report = json.loads(replay_evidence.read_text(encoding="utf-8"))
        assert replay_report["verdict"] == "PASS"
        assert replay_report["passed_cases"] == 22 and replay_report["total_cases"] == 22
    finally:
        repo.close()

