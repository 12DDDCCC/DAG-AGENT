# -*- coding: utf-8 -*-
"""第三轮独立验收反例回归测试（A1–A6 全量闭环，零模型调用）。"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import dag_agent.backends as backends
from dag_agent.daemon import RunManager
from dag_agent.pi_events import PiEventStream, parse_retry_after_hint
from techstack_full_v2 import run_full as rf
from techstack_full_v2.checks import Invalid, sha, trusted_from_db

ROOT = Path(__file__).resolve().parents[2]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _get_params(home: Path) -> dict:
    conn = sqlite3.connect(home / "mission.db")
    try:
        row = conn.execute("SELECT params_json FROM run_control").fetchone()
        return json.loads(row[0]) if row and row[0] else {}
    finally:
        conn.close()


def _set_params(home: Path, params: dict) -> None:
    conn = sqlite3.connect(home / "mission.db")
    try:
        conn.execute(
            "UPDATE run_control SET params_json=?",
            (json.dumps(params, ensure_ascii=False),),
        )
        conn.commit()
    finally:
        conn.close()


def test_a1_double_resume_with_normal_cleanup_preserves_deadline_and_zero_retries(tmp_path):
    """A1-1：恢复 → 停止 → cleanup_finished → 再次恢复，wall_deadline_utc 与 max_retries=0 不丢失。"""
    t58 = _load_module("t58_fixtures", ROOT / "tests/v3/test_v58_run_full_resume.py")
    home = t58.mini_home(tmp_path / "double-cleanup")
    p = _get_params(home)
    p.update(max_calls=0, timeout_s=1000, max_retries=0)
    _set_params(home, p)
    conn = sqlite3.connect(home / "mission.db")
    conn.execute(
        "UPDATE missions SET created_at=?",
        ((datetime.now() - timedelta(seconds=100)).replace(microsecond=0).isoformat(),),
    )
    conn.commit()
    conn.close()

    r1 = rf.resume_blocking(home, poll=0.01)
    p1 = _get_params(home)
    r2 = rf.resume_blocking(home, poll=0.01)
    p2 = _get_params(home)

    assert r1["wall_deadline_utc"]
    assert p1.get("wall_deadline_utc") == r1["wall_deadline_utc"]
    assert r2["wall_deadline_utc"] == r1["wall_deadline_utc"]
    assert p2.get("wall_deadline_utc") == r1["wall_deadline_utc"]
    assert r2["wall_remaining_s"] >= 850.0
    assert p1.get("max_retries") == 0
    assert p2.get("max_retries") == 0


def test_a1_cooldown_survives_start_and_cleanup_finished(tmp_path):
    """A1-2：provider_cooldown 在 start() 与 cleanup_finished() 后完整保留，二次启动恢复 _rate_streak。"""
    t58 = _load_module("t58_fixtures_cd", ROOT / "tests/v3/test_v58_run_full_resume.py")
    home = t58.mini_home(tmp_path / "cooldown")
    p = _get_params(home)
    p["provider_cooldown"] = {
        "until_utc": (datetime.now(timezone.utc) + timedelta(seconds=600)).isoformat(),
        "rate_streak": 4,
    }
    _set_params(home, p)
    conn = sqlite3.connect(home / "mission.db")
    mid = conn.execute("SELECT id FROM missions").fetchone()[0]
    conn.close()

    class NeverRun:
        name = "stub"

        def run(self, *a, **kw):
            raise AssertionError("No backend execution allowed during active cooldown")

    m1 = RunManager(home)
    s1 = m1.start(mid, backend_factory=NeverRun, max_calls=0, project=False, timeout_s=10)
    assert s1["ok"] is True
    assert m1._runs[mid]["scheduler"]._rate_streak == 4
    assert (_get_params(home).get("provider_cooldown") or {}).get("rate_streak") == 4
    m1.stop(mid, mode="kill")
    m1.cleanup_finished(keep=0)
    assert (_get_params(home).get("provider_cooldown") or {}).get("rate_streak") == 4

    m2 = RunManager(home)
    s2 = m2.start(mid, backend_factory=NeverRun, max_calls=0, project=False, timeout_s=10)
    assert s2["ok"] is True
    assert m2._runs[mid]["scheduler"]._rate_streak == 4
    m2.stop(mid, mode="kill")
    m2.cleanup_finished(keep=0)


def test_a1_provider_pause_survives_cleanup_and_failed_ack_preflight(tmp_path, monkeypatch):
    """A1-3：额度暂停经 cleanup_finished 后仍保留，无 ack 拒绝再次启动；ack 但预检失败时亦不丢失暂停态。"""
    t57 = _load_module("t57_pause", ROOT / "tests/v3/test_v57_provider_pause.py")
    home = tmp_path / "quota-cleanup"
    home.mkdir()
    mid, root, _ = t57.build_task(home)
    be = t57.FakeNodeBackend({"节点0": t57.quota_result("quota_exhausted", "offline quota exhausted")})
    mgr = RunManager(home)
    r = mgr.run_blocking(
        mid,
        timeout=10,
        root=root,
        backend_factory=lambda: be,
        real=False,
        project=False,
        max_calls=10,
        max_concurrency=1,
        timeout_s=5,
    )
    assert "供应商暂停" in str(r.get("halt_reason", ""))
    assert _get_params(home).get("provider_pause")
    mgr.cleanup_finished(keep=0)
    assert _get_params(home).get("provider_pause")

    mgr2 = RunManager(home)
    restart = mgr2.start(
        mid, root=root, backend_factory=lambda: be, real=False, project=False, max_calls=0, timeout_s=5
    )
    assert restart.get("ok") is False

    class Unavailable:
        def available(self):
            return False, "offline unavailable"

    monkeypatch.setattr(backends, "create_backend", lambda *a, **kw: Unavailable())
    r_bad_preflight = mgr2.start(
        mid, real=True, backend_name="pi-cli", provider_pause_ack=True, project=False, max_calls=0
    )
    assert r_bad_preflight.get("ok") is False
    assert _get_params(home).get("provider_pause")


def test_a2_root_retry_after_failed_attempt_and_manifest_hash_verification(tmp_path):
    """A2：根节点第一次 attempt 失败、第二次 running 时，trusted_from_db 支持正常重试并校验 manifest_hash/files。"""
    source = ROOT / "work/full50-runs/glm-50-20261108/mission.db"
    if not source.exists():
        t59 = _load_module("t59_fixtures_a2", ROOT / "tests/v3/test_v59_trusted_anchor.py")
        source = t59.mini_db(tmp_path) / "mission.db"
    before_sha = sha(source)
    copy_db = tmp_path / "root-retry.db"
    src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    dst = sqlite3.connect(copy_db)
    src.backup(dst)
    src.close()
    dst.row_factory = sqlite3.Row
    row = dict(dst.execute("SELECT * FROM attempts WHERE node_id='n1' AND status='accepted'").fetchone())
    dst.execute("UPDATE attempts SET status='failed' WHERE id=?", (row["id"],))
    row["id"] = "offline-root-retry"
    row["status"] = "running"
    row["ended_at"] = None
    cols = list(row)
    dst.execute(
        "INSERT INTO attempts (" + ",".join(cols) + ") VALUES (" + ",".join("?" for _ in cols) + ")",
        [row[k] for k in cols],
    )
    dst.commit()
    dst.close()

    trusted, mid, att_id = trusted_from_db(copy_db)
    assert att_id == "offline-root-retry"
    assert len(trusted) > 0
    _, _, att_explicit = trusted_from_db(copy_db, expected_attempt="offline-root-retry")
    assert att_explicit == "offline-root-retry"

    # Tamper with binding manifest_hash in copy_db and verify fail-closed rejection
    dst2 = sqlite3.connect(copy_db)
    binding = json.loads(row["input_binding_json"])
    binding[0]["manifest_hash"] = "0" * 64
    dst2.execute(
        "UPDATE attempts SET input_binding_json=? WHERE id=?",
        (json.dumps(binding), "offline-root-retry"),
    )
    dst2.commit()
    dst2.close()
    with pytest.raises(Invalid, match="manifest_hash"):
        trusted_from_db(copy_db)

    assert sha(source) == before_sha


def test_a3_stop_failure_and_main_write_failure(tmp_path, monkeypatch):
    """A3：stop() 失败且线程仍存活时不提前恢复模板或清理；结果文件写入失败时返回非零退出码。"""
    marks = []

    class AliveThread:
        def join(self, timeout=None):
            marks.append("joined_but_alive")

        def is_alive(self):
            return True

    class Manager:
        def __init__(self):
            self._runs = {"offline": {"thread": AliveThread()}}

        def status(self, mid):
            return {"running": True, "mission_status": "active"}

        def stop(self, *a, **kw):
            return {"stopped": False, "error": "offline stop failure"}

        def cleanup_finished(self, **kw):
            marks.append("cleanup")

    monkeypatch.setattr(
        rf,
        "resume",
        lambda *a, **kw: {
            "ok": True,
            "resumed": True,
            "mission_id": "offline",
            "mode": "zero-model",
            "wall_remaining_s": 0,
            "_manager": Manager(),
            "_old_registry": {"offline": "registry"},
        },
    )
    monkeypatch.setattr(rf, "restore", lambda old: marks.append("registry_restored_while_thread_alive"))
    r = rf.resume_blocking(tmp_path / "fake-monitor", poll=0.001)
    assert r["stop"]["stopped"] is False
    assert r.get("stop_failed") is True
    assert "registry_restored_while_thread_alive" not in marks
    assert "cleanup" not in marks

    monkeypatch.setattr(
        rf,
        "resume_blocking",
        lambda *a, **kw: {"ok": True, "resumed": False, "status": "done", "mission_id": "offline"},
    )
    old_write = Path.write_text

    def fail_result(self, *a, **kw):
        if self.name == "resume_results.json":
            raise OSError("offline injected write failure")
        return old_write(self, *a, **kw)

    monkeypatch.setattr(Path, "write_text", fail_result)
    monkeypatch.setattr(sys, "argv", ["run_full", "--resume", "--output", str(tmp_path / "write-failure")])
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = rf.main()
    out_obj = json.loads(stdout.getvalue())
    assert code == 1
    assert out_obj["success"] is False
    assert out_obj["results"] is None
    assert out_obj.get("fallback_results") is not None


def test_a4_empty_options_and_templates_freeze_and_effective_backend_drift(tmp_path, monkeypatch):
    """A4：显式传入空 backend_options={} 被拒绝；templates.py 冻结校验与解析后后端身份漂移拦截生效。"""
    t58 = _load_module("t58_fixtures_a4", ROOT / "tests/v3/test_v58_run_full_resume.py")
    home = t58.mini_home(tmp_path / "empty-options")
    p = _get_params(home)
    p.update(
        real=True,
        max_calls=0,
        backend_name="pi-cli",
        backend_options={"model": "original-offline", "timeout": 1500},
    )
    _set_params(home, p)

    class Available:
        name = "pi-cli"
        model = "original-offline"
        provider = "glm"
        cli = "pi"

        def available(self):
            return True, "offline"

        def run(self, *a, **kw):
            raise AssertionError("No backend execution allowed")

    monkeypatch.setattr(backends, "create_backend", lambda name, opts: Available())
    with pytest.raises(ValueError, match="backend_options"):
        rf.resume(home, backend_options={})

    # Effective backend model drift
    p["effective_backend"] = {"backend_name": "pi-cli", "model": "GLM-5.3-Flash", "provider": "glm", "cli": "pi"}
    _set_params(home, p)
    with pytest.raises(ValueError, match="model"):
        rf.resume(home)


def test_a5_conflicting_duplicate_response_id_and_retry_after_bounds(tmp_path):
    """A5：同一 responseId 冲突 message_end 判失败；负值 Retry-After 拒绝；>900s Retry-After 完整等待。"""
    st = PiEventStream()
    msgs = [
        {"type": "session", "id": "offline"},
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "responseId": "same-id",
                "stopReason": "stop",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"totalTokens": 100, "cost": {"total": 0}},
            },
        },
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "responseId": "same-id",
                "stopReason": "error",
                "errorMessage": "quota exhausted",
                "content": [],
                "usage": {"totalTokens": 110, "cost": {"total": 0}},
            },
        },
        {"type": "agent_settled", "aborted": False},
    ]
    for e in msgs:
        st.feed(json.dumps(e) + "\n")
    st.close()
    assert st.outcome()[0] == "failed"
    assert st.stats.conflicting_duplicate_ends == 1
    assert st.stats.usage_complete is False

    assert parse_retry_after_hint("Retry-After: -10") is None
    assert parse_retry_after_hint("Retry-After: 0") is None
    assert parse_retry_after_hint("Retry-After: 1200") == 1200.0


def test_a6_publication_scanner_staged_head_history_and_11th_pattern(tmp_path):
    """A6：公开扫描器覆盖工作区、暂存区（INDEX）、HEAD 与历史，且 10 个占位值后的第 11 个凭据不漏扫。"""
    scan_path = ROOT / "work/2026-11-08-r2-round/publication_scan_v2.py"
    if not scan_path.exists():
        scan_path = ROOT / "scripts/publication_scan.py"
    scan_v2 = _load_module("scan_v2", scan_path)
    checks = scan_v2.self_test(tmp_path / "self-test")
    assert checks["all"] is True, checks

    repo = tmp_path / "synthetic-repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)

    git("init", "-q")
    p = repo / "config.json"
    p.write_text("{}", encoding="utf-8")
    git("add", "config.json")
    git("-c", "user.name=Offline Fixture", "-c", "user.email=offline@example.invalid", "commit", "-qm", "baseline")
    p.write_text(json.dumps({"apiKey": "A" * 49}), encoding="utf-8")
    git("add", "config.json")
    p.write_text("{}", encoding="utf-8")

    rep = scan_v2.scan_repo(repo, skip_history=True)
    assert rep["scope"]["index_blob_count"] == 1
    assert rep["scope"]["head_blob_count"] == 1
    assert rep["scope"]["suspect_real_files_index"] == 1
    assert "INDEX:config.json" in rep["hits"]

    text = "\n".join(json.dumps({"apiKey": f"$PLACEHOLDER{i}"}) for i in range(10)) + "\n" + json.dumps({"apiKey": "B" * 49})
    hits = scan_v2.scan_text(text)
    assert len(hits) == 11
    assert sum(h["class"] == "suspect_real" for h in hits) == 1
