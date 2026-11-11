# -*- coding: utf-8 -*-
"""R3/R4 续跑监控与恢复语义回归（v61，第二轮）。

- done 幂等最前置（墙钟过期/工作区升级仍零调用返回）；
- max_calls=0 预算停派快速返回（budget_halted，不误报 timed_out）；
- max_retries=0 等合法零值保留；
- backend_options 全字段漂移拒绝；未显式提供 backend 沿用已保存值；
- 连续两次恢复 wall_deadline 不漂移（不重复扣减）；
- 状态查询失联 → 停止并留档。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys = __import__("sys")
sys.path.insert(0, str(ROOT))
from dag_agent.daemon import RunManager  # noqa: E402
from techstack_full_v2.run_full import resume, resume_blocking  # noqa: E402
from tests.v3.test_v58_run_full_resume import mini_home  # noqa: E402


def test_done_idempotent_first_before_expiry_or_drift(tmp_path):
    """已 done：墙钟过期 + frozen 漂移 → 仍幂等返回 done，零调用。"""
    home = mini_home(tmp_path, done=True, drift_frozen=True)
    # 把墙钟配置成早已过期
    repo = RunManager(home)
    db = home / "mission.db"
    import sqlite3 as _s
    con = _s.connect(str(db))
    con.execute("UPDATE run_control SET params_json=? WHERE id=1",
                (json.dumps({"_generation": 1, "real": False,
                             "timeout_s": 1.0, "root": ""}),))
    con.execute("UPDATE run_control SET generation=1 WHERE id=1")
    con.commit()
    con.close()
    r = resume(home)
    assert r.get("ok") is True and r.get("status") == "done"
    assert r.get("resumed") is False


def test_zero_values_preserved(tmp_path):
    """max_retries=0 等合法零值恢复后不被 or 默认改写。"""
    home = mini_home(tmp_path)
    import sqlite3 as _s
    con = _s.connect(str(home / "mission.db"))
    params = {"mid": "x", "root": "", "max_concurrency": 1,
              "max_retries": 0, "max_calls": 0, "max_cost_usd": None,
              "real": False, "node_cap": 51, "project": False,
              "timeout_s": 3600.0, "auto_split": True,
              "max_splits_per_mission": 1, "_generation": 1}
    con.execute("UPDATE run_control SET params_json=? WHERE id=1",
                (json.dumps(params),))
    con.commit()
    con.close()
    info = resume(home)
    assert info.get("ok") is True and info.get("resumed") is True
    manager = info["_manager"]
    mid = info["mission_id"]
    try:
        manager.stop(mid, mode="kill")
        # start 持久化的 params 应保留 0 值
        con = _s.connect(str(home / "mission.db"))
        saved = json.loads(con.execute(
            "SELECT params_json FROM run_control WHERE id=1").fetchone()[0])
        con.close()
        assert saved.get("max_retries") == 0      # 0 不被 or 默认改写
        assert saved.get("max_calls") == 0
        assert saved.get("max_concurrency") == 1
        assert saved.get("max_splits_per_mission") == 1
        # R4.1：不可变截止时间已固化
        assert saved.get("wall_deadline_utc")
    finally:
        old = info.pop("_old_registry", None)


def test_budget_halted_returns_promptly(tmp_path):
    """max_calls=0：底层立即停派 → resume_blocking 快速返回
    budget_halted（不再等待墙钟误报 timed_out），并已执行停止确认。"""
    home = mini_home(tmp_path)
    import sqlite3 as _s
    con = _s.connect(str(home / "mission.db"))
    params = json.loads(con.execute(
        "SELECT params_json FROM run_control WHERE id=1").fetchone()[0])
    params["max_calls"] = 0
    params["timeout_s"] = 60.0
    con.execute("UPDATE run_control SET params_json=? WHERE id=1",
                (json.dumps(params),))
    con.commit()
    con.close()
    t0 = time.monotonic()
    out = resume_blocking(home)
    elapsed = time.monotonic() - t0
    assert out.get("ended_reason") == "budget_halted", out
    assert out.get("timed_out") is not True
    assert elapsed < 30, f"应快速返回（实测 {elapsed:.1f}s）"
    assert out.get("stop_called") is True   # 收尾停止已确认


def test_backend_options_drift_rejected(tmp_path, monkeypatch):
    """real 模式 backend_options 全字段核对：env_file 等任一字段漂移
    → 拒绝（不只比较 timeout）。"""
    home = mini_home(tmp_path)
    import sqlite3 as _s
    con = _s.connect(str(home / "mission.db"))
    params = {"mid": "x", "root": "", "max_concurrency": 1,
              "max_retries": 1, "max_calls": 50, "max_cost_usd": None,
              "real": True, "node_cap": 51, "project": False,
              "timeout_s": 3600.0, "auto_split": True,
              "max_splits_per_mission": 12, "_generation": 1,
              "backend_name": "pi-cli",
              "backend_options": {"timeout": 900, "env_file": "orig.env",
                                  "model": "GLM-5.3-Flash"}}
    con.execute("UPDATE run_control SET params_json=? WHERE id=1",
                (json.dumps(params),))
    con.commit()
    con.close()
    with pytest.raises(ValueError, match="backend_options 与原运行不一致"):
        resume(home, backend_name="pi-cli",
               backend_options={"timeout": 900, "env_file": "orig.env",
                                "model": "GLM-5.3-Flash-X"})   # 模型漂移
    # 全字段一致 → 进入后端预检。monkeypatch pi 预检失败以避免真实启动
    # （错误信息含 pi-cli 证明沿用 saved 后端而非默认 claude-cli）
    from dag_agent import pi_backend as _pb
    orig_avail = _pb.PiBackend.available
    monkeypatch.setattr(_pb.PiBackend, "available",
                        lambda self: (False, "pi-cli unavailable-test"))
    try:
        with pytest.raises(ValueError) as ei:
            resume(home)   # 不显式提供 backend → 沿用 pi-cli
        assert "pi-cli" in str(ei.value)
    finally:
        monkeypatch.setattr(_pb.PiBackend, "available", orig_avail)


def test_double_resume_no_deadline_drift(tmp_path):
    """连续两次独立恢复：wall_deadline_utc 不变（不重复扣减）；第二次
    remaining ≈ deadline-now（而非再次减去全部历史耗时）。"""
    home = mini_home(tmp_path)
    import sqlite3 as _s
    con = _s.connect(str(home / "mission.db"))
    params = json.loads(con.execute(
        "SELECT params_json FROM run_control WHERE id=1").fetchone()[0])
    params["timeout_s"] = 1000.0
    con.execute("UPDATE run_control SET params_json=? WHERE id=1",
                (json.dumps(params),))
    con.commit()
    con.close()
    r1 = resume(home)
    assert r1.get("ok") is True
    dl1 = r1["wall_deadline_utc"]
    rem1 = r1["wall_remaining_s"]
    manager1: RunManager = r1["_manager"]
    manager1.stop(r1["mission_id"], mode="kill")
    time.sleep(1.2)
    r2 = resume(home)   # 第二次独立恢复（同一进程内代表新恢复调用）
    assert r2.get("ok") is True
    dl2 = r2["wall_deadline_utc"]
    rem2 = r2["wall_remaining_s"]
    manager2: RunManager = r2["_manager"]
    manager2.stop(r2["mission_id"], mode="kill")
    assert dl1 == dl2, f"截止时间漂移：{dl1} -> {dl2}"
    # 第二次只应比第一次少约 1.2s 间隔（而非重扣 1000-已过全程）
    assert 0 < rem1 - rem2 < 10, f"rem1={rem1} rem2={rem2}"
    for r in (r1, r2):
        old = r.pop("_old_registry", None)


def test_status_query_failure_stops_and_records(tmp_path, monkeypatch):
    """状态查询持续异常 → 停止本次运行并留档 status_query_failed
    （直接驱动统一监控路径 _monitor_run）。"""
    home = mini_home(tmp_path)
    info = resume(home)
    assert info.get("ok") is True
    manager: RunManager = info["_manager"]
    mid = info["mission_id"]

    def boom(self, m):
        raise RuntimeError("simulated status outage")

    monkeypatch.setattr(RunManager, "status", boom)
    try:
        from techstack_full_v2.run_full import _monitor_run
        completed, ended_reason, stop_result = _monitor_run(
            manager, mid, wall_remaining_s=60, poll=0.05)
    finally:
        monkeypatch.undo()
    assert completed is False
    assert ended_reason == "status_query_failed"
    assert stop_result is not None   # 失联路径已调用停止
    old = info.pop("_old_registry", None)
