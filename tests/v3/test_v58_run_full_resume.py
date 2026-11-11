# -*- coding: utf-8 -*-
"""完整任务包跨进程续跑回归（v58，2026-11-08 独立复查 P2 关闭条件）。

单元级（快）：resume 各拒绝分支（缺库/缺配置/版本漂移/done 不再调用）；
全链级（slow 标记，独立 subprocess）：部分完成 kill → 新进程 --resume
→ 沿用 accepted/预算/拆分额度完成，不重复执行、不重复应用拆分。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "techstack_full_v2"
sys.path.insert(0, str(ROOT))
from techstack_full_v2.checks import IMPORTS, sha  # noqa: E402
from techstack_full_v2.run_full import resume  # noqa: E402


def mini_home(tmp_path: Path, *, done: bool = False,
              with_params: bool = True, drift_frozen: bool = False) -> Path:
    """构造符合 run_full 布局的最小任务目录。"""
    home = tmp_path / "run"
    home.mkdir(parents=True)
    frozen = home / "frozen"
    frozen.mkdir()
    for name in ("checks.py", "structure.py"):
        shutil.copyfile(PKG / name, frozen / name)
    if drift_frozen:
        p = frozen / "checks.py"
        p.write_text(p.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    inputs = PKG / "inputs"
    frozen_inputs = frozen / "inputs"
    frozen_inputs.mkdir()
    prov = frozen_inputs / "input-provenance.json"
    prov.write_text('{"origin":"test-fixture"}', encoding="utf-8")
    imports = {n: sha(inputs / n) for n in IMPORTS if (inputs / n).is_file()}
    imports["input-provenance.json"] = sha(prov)
    manifest = {"schema_version": 1, "mode": "zero-model",
                "imports": imports,
                "trusted_db": str((home / "mission.db").resolve())}
    (frozen / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    from dag_agent.repository import Repository
    from dag_agent.graph import Graph
    from dag_agent.assessment import default_checker3
    repo = Repository(home / "mission.db")
    graph = Graph(repo, checker3=default_checker3)
    mid, root = graph.create_mission("恢复回归", "简单任务",
                                     root_check="python -c pass")
    if done:
        # 直接置 done（不执行模型）
        with repo.tx() as cur:
            cur.execute("UPDATE missions SET status='done',"
                        " closure_basis='machine' WHERE id=?", (mid,))
    params = None
    if with_params:
        params = {"mid": mid, "root": root, "max_concurrency": 1,
                  "max_retries": 1, "max_calls": 128, "max_cost_usd": None,
                  "real": False, "node_cap": 51, "project": False,
                  "timeout_s": 3600.0, "auto_split": True,
                  "max_splits_per_mission": 12, "_generation": 1}
    with repo.tx() as cur:
        if params is None:
            cur.execute(
                "INSERT INTO run_control(id, stop_requested, hard_stop,"
                " generation, params_json, updated_at)"
                " VALUES(1, 0, 0, 1, '', datetime('now'))"
                " ON CONFLICT(id) DO NOTHING")
        else:
            cur.execute(
                "INSERT INTO run_control(id, stop_requested, hard_stop,"
                " generation, params_json, updated_at)"
                " VALUES(1, 0, 0, 1, ?, datetime('now'))"
                " ON CONFLICT(id) DO UPDATE SET params_json=excluded.params_json",
                (json.dumps(params, ensure_ascii=False),))
    repo.close()
    return home


def test_resume_rejects_missing_db(tmp_path):
    try:
        resume(tmp_path / "nope")
        raise AssertionError("应当拒绝")
    except ValueError as e:
        assert "任务库不存在" in str(e)


def test_resume_rejects_missing_params(tmp_path):
    home = mini_home(tmp_path, with_params=False)
    try:
        resume(home)
        raise AssertionError("应当拒绝")
    except ValueError as e:
        assert "无持久化运行配置" in str(e)


def test_resume_rejects_frozen_drift(tmp_path):
    """当前源码与冻结考卷不一致 → 拒绝（不用工作区版本替代旧冻结）。"""
    home = mini_home(tmp_path, drift_frozen=True)
    try:
        resume(home)
        raise AssertionError("应当拒绝")
    except ValueError as e:
        assert "冻结考卷" in str(e) or "不一致" in str(e)


def test_resume_rejects_generation_mismatch(tmp_path):
    home = mini_home(tmp_path)
    from dag_agent.repository import Repository
    repo = Repository(home / "mission.db")
    with repo.tx() as cur:
        cur.execute("UPDATE run_control SET generation=7 WHERE id=1")
    repo.close()
    try:
        resume(home)
        raise AssertionError("应当拒绝")
    except ValueError as e:
        assert "代际" in str(e)


def test_resume_done_returns_without_calls(tmp_path):
    """已完成任务恢复：直接返回 done，不再次调用模型。"""
    home = mini_home(tmp_path, done=True)
    r = resume(home)
    assert r.get("ok") is True
    assert r.get("resumed") is False
    assert r.get("status") == "done"


@pytest.mark.slow
def test_resume_full_chain_independent_process(tmp_path):
    """独立进程全链：8s 中断 → 新进程 --resume → 完成；已验收不重复
    执行、拆分不重复应用、预算不重置、墙钟沿用原起点。"""
    run_dir = tmp_path / "run"
    env = dict(__import__("os").environ, PYTHONUTF8="1")
    base = [sys.executable, "-m", "techstack_full_v2.run_full"]
    db = run_dir / "mission.db"
    import sqlite3
    import time as _t
    # 原墙钟 300s：本机 AV/CI 下 50 节点可能超过旧 60s；仍在首个拆分后强杀。
    proc = subprocess.Popen(base + ["--mode", "zero-model",
                                    "--output", str(run_dir),
                                    "--timeout", "300"],
                            cwd=str(ROOT), env=env,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    deadline = _t.monotonic() + 60
    while _t.monotonic() < deadline:
        if db.is_file():
            try:
                con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
                n = con.execute(
                    "SELECT COUNT(*) FROM events WHERE type='split_applied'"
                ).fetchone()[0]
                con.close()
                if n >= 1:
                    break
            except sqlite3.Error:
                pass
        _t.sleep(0.5)
    else:
        proc.terminate()
        raise AssertionError("60s 内未见首个拆分应用——环境异常")
    _t.sleep(1.0)          # 留出更多 accepted 落库
    proc.terminate()       # 外部强杀（模拟崩溃，无 stop/无留档）
    proc.wait(timeout=30)
    pre_acc = []
    con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    pre_acc = [dict(r) for r in con.execute(
        "SELECT id, node_id, status FROM attempts WHERE status='accepted'")]
    pre_budget = json.loads(con.execute(
        "SELECT budget_json FROM missions").fetchone()[0] or "{}")
    pre_applied = con.execute(
        "SELECT COUNT(*) FROM events WHERE type='split_applied'").fetchone()[0]
    con.close()
    assert not (run_dir / "run_results.json").exists(),         "强杀不应有优雅收尾留档"
    assert len(pre_acc) >= 1, "中断前应有已验收节点"
    assert pre_applied >= 1, "中断前应已有拆分应用"
    # 独立进程恢复
    r2 = subprocess.run(base + ["--resume", "--output", str(run_dir)],
                        cwd=str(ROOT), env=env, capture_output=True,
                        encoding="utf-8", timeout=900)
    resume_out = json.loads((run_dir / "resume_results.json").read_text(
        encoding="utf-8"))
    assert resume_out.get("resumed") is True
    assert resume_out.get("success") is True, \
        f"恢复后应完成：{json.dumps(resume_out, ensure_ascii=False)[:400]}"
    assert resume_out["checks"]["mission_done_machine"] is True
    assert resume_out["checks"][f"nodes_50"] is True
    # 拆分恰好 12 次应用（不重复）
    con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    post_applied = con.execute(
        "SELECT COUNT(*) FROM events WHERE type='split_applied'").fetchone()[0]
    # 已验收 attempt 不复做：恢复前的 accepted attempt 仍在且状态不变
    post_acc = {r["id"]: dict(r) for r in con.execute(
        "SELECT id, node_id, status FROM attempts WHERE status='accepted'")}
    con.close()
    assert post_applied == 12
    for a in pre_acc:
        assert post_acc.get(a["id"], {}).get("status") == "accepted", \
            f"已验收 attempt {a['id']} 状态被改动"
    # 预算不重置：恢复后累计 >= 中断前
    con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    post_budget = json.loads(con.execute(
        "SELECT budget_json FROM missions").fetchone()[0] or "{}")
    con.close()
    assert post_budget.get("calls_used", 0) >= pre_budget.get("calls_used", 1)
    # 墙钟沿用原起点：resume 返回的剩余时间 < 原 timeout
    assert 0 < resume_out.get("wall_remaining_s", 0) < 300
    # 第二次独立进程恢复（任务已完成）：幂等返回 done，零调用
    r3 = subprocess.run(base + ["--resume", "--output", str(run_dir)],
                        cwd=str(ROOT), env=env, capture_output=True,
                        encoding="utf-8", timeout=300)
    resume2 = json.loads((run_dir / "resume_results.json").read_text(
        encoding="utf-8"))
    assert resume2.get("ok") is True and resume2.get("resumed") is False
    assert resume2.get("status") == "done"
    import sqlite3 as _s
    con = _s.connect(db.as_uri() + "?mode=ro", uri=True)
    calls_after = json.loads(con.execute(
        "SELECT budget_json FROM missions").fetchone()[0] or "{}").get(
        "calls_used", 0)
    con.close()
    assert calls_after == post_budget.get("calls_used", 0),         "done 幂等恢复不得产生新调用"
