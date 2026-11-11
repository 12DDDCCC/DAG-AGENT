# -*- coding: utf-8 -*-
"""跨进程锁文件（.run.lock）与干净发布子集导出回归测试（v65，零模型调用）。"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dag_agent.assessment import IGNORED_FILES
from dag_agent.daemon import RunManager
from dag_agent.repository import Repository, is_active_ledger_file

ROOT = Path(__file__).resolve().parents[2]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mini_home(tmp_path: Path) -> tuple[Path, str]:
    t58 = _load_module("t58_lock_fx", ROOT / "tests/v3/test_v58_run_full_resume.py")
    home = t58.mini_home(tmp_path / "lock-home")
    conn = sqlite3.connect(home / "mission.db")
    try:
        mid = conn.execute("SELECT id FROM missions").fetchone()[0]
    finally:
        conn.close()
    return home, mid


def test_lock_file_ignored_by_assessment_and_ledger_filter(tmp_path):
    """验证 .run.lock 已纳入快照忽略集与活跃账本伴生文件白名单，不会污染工件快照。"""
    assert ".run.lock" in IGNORED_FILES
    repo = Repository(tmp_path / "mission.db")
    try:
        assert is_active_ledger_file(tmp_path / ".run.lock") is True
    finally:
        repo.close()


def test_same_process_second_manager_blocked_and_lock_released_on_stop(tmp_path):
    """同进程内第二个 RunManager 实例尝试操作同一 mission.db 时被 .run.lock 拒绝；stop 后锁释放。"""
    home, mid = _mini_home(tmp_path)
    lock_file = home / ".run.lock"

    # 设置冷却期使 m1 调度线程持续处于活跃等待状态（零模型调用）
    conn = sqlite3.connect(home / "mission.db")
    p = json.loads(conn.execute("SELECT params_json FROM run_control").fetchone()[0] or "{}")
    p["provider_cooldown"] = {
        "until_utc": (datetime.now(timezone.utc) + timedelta(seconds=600)).isoformat(),
        "rate_streak": 2,
    }
    conn.execute("UPDATE run_control SET params_json=?", (json.dumps(p),))
    conn.commit()
    conn.close()

    m1 = RunManager(home)
    r1 = m1.start(mid, max_calls=0, project=False, timeout_s=30)
    assert r1["ok"] is True
    assert lock_file.exists()

    # 第二个 RunManager 实例尝试启动同一任务库 → 被拒绝
    m2 = RunManager(home)
    r2 = m2.start(mid, max_calls=0, project=False, timeout_s=30)
    assert r2["ok"] is False
    assert ".run.lock" in r2["error"]
    assert r2.get("lock_holder", {}).get("mission_id") == mid

    # m1 正常停止后，OS 锁释放（锁文件稳定保留防 inode 竞态），m2 可以正常启动并获锁
    m1.stop(mid, mode="kill")
    m1.cleanup_finished(keep=0)
    assert lock_file.exists()  # 稳定保留，不 unlink

    r3 = m2.start(mid, max_calls=0, project=False, timeout_s=30)
    assert r3["ok"] is True
    m2.stop(mid, mode="kill")
    m2.cleanup_finished(keep=0)
    assert lock_file.exists()


def test_cross_process_lock_blocks_competing_start_without_reaping_and_releases_on_kill(tmp_path):
    """跨独立进程（subprocess.Popen）持有 .run.lock 时：
    1) 父进程 RunManager.start() 在触碰 recover_mission 之前即被拦截，绝不误收割子进程 running attempt；
    2) 子进程被强杀（proc.kill()）后，OS 内核自动释放文件锁，父进程立即获锁接管。
    """
    home, mid = _mini_home(tmp_path)

    # 在库内插入一条模拟由子进程正在执行的 running attempt
    conn = sqlite3.connect(home / "mission.db")
    conn.execute(
        "INSERT INTO attempts(id,node_id,mission_id,backend,status,workdir,pid,heartbeat_at,"
        "resources_json,result_summary,input_binding_json,claim_fingerprint_json,category,started_at,ended_at) "
        "VALUES('a-child-live','n1',?,'stub','running','w',999999,datetime('now'),'{}','','[]','[]','',"
        "datetime('now'),NULL)",
        (mid,),
    )
    conn.commit()
    conn.close()

    child_code = (
        "import sys, time\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from pathlib import Path\n"
        "from dag_agent.daemon import _RunFileLock\n"
        f"lk, err, holder = _RunFileLock.acquire(Path({str(home / 'mission.db')!r}), {mid!r})\n"
        "assert lk is not None, err\n"
        "print('CHILD_LOCKED', flush=True)\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        line = proc.stdout.readline().strip()
        assert line == "CHILD_LOCKED"

        mgr = RunManager(home)
        blocked = mgr.start(mid, max_calls=0, project=False, timeout_s=10)
        assert blocked["ok"] is False
        assert ".run.lock" in blocked["error"]
        assert blocked.get("lock_holder", {}).get("pid") == proc.pid

        # 关键断言：因锁在打开 Repository / recover_mission 之前拦截，a-child-live 仍为 running，未被误收割
        conn2 = sqlite3.connect(home / "mission.db")
        st_while_locked = conn2.execute("SELECT status FROM attempts WHERE id='a-child-live'").fetchone()[0]
        conn2.close()
        assert st_while_locked == "running"

        # 强杀子进程（模拟进程崩溃/SIGKILL/taskkill），验证 OS 自动释放锁
        proc.kill()
        proc.wait(timeout=5)

        resumed = mgr.start(mid, max_calls=0, project=False, timeout_s=10)
        assert resumed["ok"] is True
        mgr.stop(mid, mode="kill")
        mgr.cleanup_finished(keep=0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_pyproject_toml_and_clean_release_subset_export(tmp_path):
    """验证 pyproject.toml 规范完整，且 export_release_subset 导出的干净发布子集零凭据、无历史大目录。"""
    pyproject_path = ROOT / "pyproject.toml"
    assert pyproject_path.is_file()
    raw_toml = pyproject_path.read_text(encoding="utf-8")
    assert 'name = "dag-agent"' in raw_toml
    assert 'version = "3.2.1"' in raw_toml
    assert 'dag-agent = "dag_agent.cli:main"' in raw_toml
    assert 'dag-agent-mcp = "mcp_server_v3:main"' in raw_toml

    exporter = _load_module("export_release_subset", ROOT / "scripts/export_release_subset.py")
    out_dir = tmp_path / "dag-agent-release"
    manifest = exporter.export_release_subset(ROOT, out_dir)

    assert manifest["suspect_real_hits_count"] == 0
    assert manifest["file_count"] >= 50
    assert (out_dir / "RELEASE_MANIFEST.json").is_file()
    assert (out_dir / "pyproject.toml").is_file()
    assert (out_dir / "dag_agent" / "daemon.py").is_file()
    assert (out_dir / "tests" / "v3" / "test_v64_r3_closure.py").is_file()
    assert (out_dir / "scripts" / "publication_scan.py").is_file()

    # 确认 work/、missions/、*.db、.env* 等均未混入发布子集
    assert not (out_dir / "work").exists()
    assert not (out_dir / "missions").exists()
    assert not list(out_dir.rglob("*.db"))
    assert not list(out_dir.rglob(".env*"))
    assert not list(out_dir.rglob(".run.lock"))

    # 抽检 RELEASE_MANIFEST.json 中记录的 sha256 与落盘文件完全一致
    for rel_key in ("pyproject.toml", "dag_agent/daemon.py", "scripts/publication_scan.py"):
        recorded = manifest["files"][rel_key]["sha256"]
        actual = hashlib.sha256((out_dir / rel_key).read_bytes()).hexdigest()
        assert recorded == actual


def test_export_protection_and_safe_overwrite_rollback(tmp_path, monkeypatch):
    """验证发布子集导出的强清单安全校验、受保护路径拒绝及覆盖失败事务回滚。"""
    exporter = _load_module("export_release_subset_t65", ROOT / "scripts/export_release_subset.py")

    # 1. 验证源码根、祖先及受保护源码目录被拒绝
    for protected in [ROOT, ROOT.parent, ROOT / "dag_agent", ROOT / "tests"]:
        with pytest.raises(ValueError):
            exporter._validate_dest_path(ROOT, protected)

    # 2. 验证非空目录默认拒绝覆盖
    target = tmp_path / "target-release"
    target.mkdir()
    (target / "random.txt").write_text("user data", encoding="utf-8")
    with pytest.raises(ValueError, match="默认拒绝导出"):
        exporter._validate_dest_path(ROOT, target, overwrite=False)

    # 3. 验证残缺清单（如 name-only、缺 version、缺 files、files 数不一致、缺少 sha256 结构）均被拒绝
    (target / "RELEASE_MANIFEST.json").write_text(json.dumps({"name": "dag-agent"}), encoding="utf-8")
    with pytest.raises(ValueError, match="缺少 version 字段"):
        exporter._validate_dest_path(ROOT, target, overwrite=True)

    (target / "RELEASE_MANIFEST.json").write_text(json.dumps({
        "name": "dag-agent", "version": "3.2.0"
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="缺少或空的 files 字段"):
        exporter._validate_dest_path(ROOT, target, overwrite=True)

    (target / "RELEASE_MANIFEST.json").write_text(json.dumps({
        "name": "dag-agent", "version": "3.2.0", "files": {"a.txt": {}}, "file_count": 2
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="file_count != len\\(files\\)"):
        exporter._validate_dest_path(ROOT, target, overwrite=True)


    # 完整导出一次，后续针对真实导出目录做全量强校验反例
    import copy
    import shutil
    shutil.rmtree(target)
    exporter.export_release_subset(ROOT, target)
    manifest_path = target / "RELEASE_MANIFEST.json"
    good_text = manifest_path.read_text(encoding="utf-8")
    good = json.loads(good_text)
    exporter._validate_dest_path(ROOT, target, overwrite=True)  # 干净导出目录应通过

    def _expect_reject(mutate, match):
        mf = copy.deepcopy(good)
        mutate(mf)
        manifest_path.write_text(json.dumps(mf), encoding="utf-8")
        try:
            with pytest.raises(ValueError, match=match):
                exporter._validate_dest_path(ROOT, target, overwrite=True)
        finally:
            manifest_path.write_text(good_text, encoding="utf-8")

    keys = list(good["files"])
    # 第 11 条（及之后）元数据为空字典
    _expect_reject(lambda m: m["files"].__setitem__(keys[10], {}), "sha256 非法")
    # 越界路径 / sha256=None / bytes=-1
    def _escape(m):
        m["files"]["../outside"] = {"sha256": None, "bytes": -1}
        m["file_count"] = len(m["files"])
    _expect_reject(_escape, "越界|非法")
    # 清单声称存在但磁盘缺失
    def _ghost(m):
        m["files"]["ghost.txt"] = {"sha256": "0" * 64, "bytes": 1}
        m["file_count"] = len(m["files"])
    _expect_reject(_ghost, "缺失")
    # 清单哈希与实际不符
    _expect_reject(lambda m: m["files"][keys[20]].__setitem__("sha256", "f" * 64), "SHA256")
    # 清单大小与实际不符
    _expect_reject(lambda m: m["files"][keys[21]].__setitem__("bytes", good["files"][keys[21]]["bytes"] + 1), "大小")

    # 实际文件被篡改（清单未变）
    victim = target / keys[30]
    orig_bytes = victim.read_bytes()
    victim.write_bytes(orig_bytes + b"\n# tampered")
    try:
        with pytest.raises(ValueError, match="大小|SHA256"):
            exporter._validate_dest_path(ROOT, target, overwrite=True)
    finally:
        victim.write_bytes(orig_bytes)

    # 额外用户文件
    extra = target / "user-notes.txt"
    extra.write_text("mine", encoding="utf-8")
    try:
        with pytest.raises(ValueError, match="清单外文件"):
            exporter._validate_dest_path(ROOT, target, overwrite=True)
    finally:
        extra.unlink()
    exporter._validate_dest_path(ROOT, target, overwrite=True)

    # 4. 覆盖替换事务性回滚：rename/move 失败时旧目录全部文件与原清单字节保留
    snapshot = {p.relative_to(target).as_posix(): p.read_bytes()
                for p in target.rglob("*") if p.is_file()}

    orig_rename = Path.rename
    orig_move = shutil.move

    def fake_rename(self, dst):
        if self.name.startswith(".tmp-export-"):
            raise OSError("injected rename failure")
        return orig_rename(self, dst)

    def fake_move(src, dst):
        if Path(src).name.startswith(".tmp-export-"):
            raise OSError("injected move failure")
        return orig_move(src, dst)

    monkeypatch.setattr(Path, "rename", fake_rename)
    monkeypatch.setattr(shutil, "move", fake_move)

    with pytest.raises(OSError, match="injected"):
        exporter.export_release_subset(ROOT, target, overwrite=True)

    after = {p.relative_to(target).as_posix(): p.read_bytes()
             for p in target.rglob("*") if p.is_file()}
    assert after == snapshot


def test_frozen_fixture_bytes_are_lf_and_manifest_matches():
    """冻结夹具必须是 LF 字节且与 freeze_manifest 哈希一致，并由 .gitattributes 禁止换行转换。"""
    base = ROOT / "tests" / "fixtures" / "acceptance_checks" / "fixtures"
    mf = json.loads((base / "freeze_manifest.json").read_text(encoding="utf-8"))
    assert mf["files_sha256"]
    for rel, digest in mf["files_sha256"].items():
        raw = (base / rel).read_bytes()
        assert b"\r" not in raw, rel
        assert hashlib.sha256(raw).hexdigest() == digest, rel
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "tests/fixtures/acceptance_checks/fixtures/** -text" in attrs
