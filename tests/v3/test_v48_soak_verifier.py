# -*- coding: utf-8 -*-
"""v48：Soak 验证器（verify_soak）负夹具转可执行回归。

对应 Codex 复核 R6 的三个隔离夹具反例与门槛漂移要求：
- 缺线程/句柄/子进程指标 → 相应项 UNVERIFIED（不再补 0 假 PASS）；
- 已知硬失败（RSS 斜率越线）优先于 UNVERIFIED（不再被样本不足降级）；
- 重启证据缺失/未闭合 → restart_recovery UNVERIFIED/FAIL（不看自报布尔）；
- run_config.thresholds 与 SPEC 漂移 → spec_thresholds_match FAIL；
- 任一 600s 窗口利用率 <80% → 并发 FAIL（全窗遍历）。

夹具=合成最小 run_dir（真实任务库+采样/时间线），monkeypatch
verify_soak.HERE 指向临时目录，不触碰任何真实 Soak 运行目录。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

_fix_soak = Path(__file__).resolve().parents[1] / "fixtures" / "soak"
SOAK_DIR = _fix_soak if _fix_soak.is_dir() else (Path(__file__).resolve().parents[2] / "work" / "soak_autogrow")

CMD_OK = ('python -c "import sys;from pathlib import Path;'
          "p=Path('deliverable.md');"
          'sys.exit(0 if p.is_file() and p.stat().st_size>10 else 1)"')
BRIEF = ("【输入】无外部输入。\n【交付清单】deliverable.md\n"
         "【接口签名】deliverable.md 为 UTF-8 文本，>10 字节。\n"
         "【样例】示例行\n【自测入口】运行考卷。\n【任务】{body}")


def _mk_mission(home: Path, name: str, status: str = "done") -> str:
    """造一个真实闭合（或未闭合）的任务库，含 accepted 工件。"""
    from dag_agent.assessment import default_checker3
    from dag_agent.graph import Graph
    from dag_agent.repository import Repository
    mdir = home / name
    mdir.mkdir(parents=True, exist_ok=True)
    repo = Repository(mdir / "mission.db")
    graph = Graph(repo, checker3=default_checker3)
    try:
        mid, root = graph.create_mission(
            name, brief=BRIEF.format(body="x"), root_check=CMD_OK,
            root_declared_artifacts=("deliverable.md",))
        if status == "done":
            aid = graph.start_attempt(mid, root, backend="stub")
            wd = Path(repo.get_attempt(aid).workdir)
            (wd / "deliverable.md").write_text("x" * 50, encoding="utf-8")
            graph.submit_attempt(mid, root, aid,
                                 artifact_paths=["deliverable.md"])
        repo.close()
        return mid
    except BaseException:
        repo.close()
        raise


def _mk_run(tmp_path: Path, *, mid: str, kind_map: dict,
            samples: list[dict], timeline: list[dict], wall: float = 3700.0,
            duration: float = 3600.0, thresholds: dict | None = "spec",
            restart_probe: dict | None = None) -> Path:
    """合成最小 run_dir。thresholds="spec"=记录 SPEC 值；None=不记录。"""
    import hashlib
    run_dir = tmp_path / "runs" / "fx"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "home" / mid).mkdir(parents=True, exist_ok=True)
    if thresholds == "spec":
        sys.path.insert(0, str(SOAK_DIR))
        import verify_soak as _vs
        thr = dict(_vs.SPEC_THRESHOLDS)
    elif thresholds is None:
        thr = None
    else:
        thr = thresholds
    config = {"duration_s": duration, "concurrency_total": 2,
              "long_worker_s": 700.0, "heartbeat_interval_s": 10.0,
              "warmup_s": 120.0, "probe_budget_slots": 2}
    if thr is not None:
        config["thresholds"] = thr
        # V1：recorded spec sha=夹具内 SPEC 副本（_run_verify 会把
        # SPEC-Soak.md 拷入 monkeypatch 后的 HERE）
        spec_src2 = SOAK_DIR / "SPEC-Soak.md"
        if spec_src2.is_file():
            import hashlib as _h
            config["spec_sha256"] = _h.sha256(
                spec_src2.read_bytes()).hexdigest()[:16]
    (run_dir / "run_config.json").write_text(
        json.dumps(config), encoding="utf-8")
    inj = {"orphan_reaped": ["a-ghost"], "orphan_exact": True,
           "double_settle": {"replayed": True}, "goal_changed_to": 2}
    if restart_probe is not None:
        inj["restart_probe"] = restart_probe
    (run_dir / "injections.json").write_text(json.dumps(inj),
                                             encoding="utf-8")
    # 保持合成夹具与任务库自洽（通用契约要求权威对账）
    import sqlite3
    db_list = list((tmp_path / "runs" / "fx" / "home").glob("*/mission.db"))
    main_attempt_ids = set()
    is_probe_mid = "probe" in (kind_map.get(mid, {}).get("name") or "")
    if db_list:
        db = db_list[0]
        c = sqlite3.connect(db)
        m_row = c.execute("SELECT id FROM missions LIMIT 1").fetchone()
        n_row = c.execute("SELECT id FROM nodes LIMIT 1").fetchone()
        if m_row and n_row:
            real_mid = m_row[0]
            root_id = n_row[0]
            for e in timeline:
                if not e.get("probe") and not str(e.get("attempt", "")).startswith("plan:"):
                    aid = e.get("attempt")
                    if aid:
                        main_attempt_ids.add(aid)
                        if e.get("mid") == "m":
                            e["mid"] = real_mid
                        if e.get("node") == "n":
                            e["node"] = root_id
                        if not is_probe_mid:
                            exists = c.execute("SELECT 1 FROM attempts WHERE id=?", (aid,)).fetchone()
                            if not exists:
                                c.execute(
                                    "INSERT OR REPLACE INTO attempts(id, node_id, mission_id, backend, status, workdir, started_at, ended_at) "
                                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                                    (aid, root_id, real_mid, "stub", "done", "", "2026-10-12T00:00:00", "2026-10-12T00:30:00")
                                )
            c.commit()
        c.close()

    summary = {
        "finished": [{"mid": mid, "status": "done"}],
        "admit": {"admitted": [mid]},
        "wall_s": wall, "backend_calls": len(main_attempt_ids), "backend_calls_probe": 0,
        "obs_final": {"counters": {"monitor_errors": 0,
                                   "dispatch_errors": 0,
                                   "settle_errors": 0, "check_errors": 0,
                                   "project_errors": 0, "reap_errors": 0,
                                   "heartbeat_errors": 0}},
        "version_drift": False, "heartbeat_report": {},
        "peak_rss_mb": 31.0,
    }
    (run_dir / "run_summary.json").write_text(json.dumps(summary),
                                              encoding="utf-8")
    (run_dir / "samples.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in samples), encoding="utf-8")
    (run_dir / "timeline.json").write_text(
        json.dumps({"timeline": timeline}), encoding="utf-8")
    (run_dir / "kind_map.json").write_text(json.dumps(kind_map),
                                           encoding="utf-8")
    return run_dir


def _base_samples(n=8, rss=30.0, handles=(100, 118), threads=7,
                  inflight=2, children=0, drop_keys=()) -> list[dict]:
    """8 个样本、60s 间隔；句柄斜率≈18/min（<20 冻结值）。"""
    t0 = time.time() - n * 60
    h0, h1 = handles
    out = []
    for i in range(n):
        row = {"ts": t0 + i * 60, "iso": "x",
               "proc_rss_mb": rss if i < n // 2 else rss + 0.5,
               "proc_threads": threads, "proc_handles": round(
                   h0 + (h1 - h0) * i / (n - 1)),
               "children_n": children, "active_runs": 0, "inflight": inflight}
        for k in drop_keys:
            row.pop(k, None)
        out.append(row)
    return out


def _base_timeline(span=1800.0) -> list[dict]:
    """两个 attempt 全程覆盖（C=2 → 利用率 1.0，完整 600s 窗×3）。"""
    ev = []
    for i in range(2):
        ev.append({"mid": "m", "node": "n", "attempt": f"a{i}",
                   "phase": "start", "t": 1000.0})
        ev.append({"mid": "m", "node": "n", "attempt": f"a{i}",
                   "phase": "end", "t": 1000.0 + span})
    return ev


def _run_verify(tmp_path, run_dir: Path) -> dict:
    import verify_soak
    monkey = pytest.MonkeyPatch()
    monkey.setattr(verify_soak, "HERE", tmp_path)
    spec_src = SOAK_DIR / "SPEC-Soak.md"
    if spec_src.is_file():
        (tmp_path / "SPEC-Soak.md").write_bytes(spec_src.read_bytes())
    monkey.setattr(sys, "argv", ["verify_soak.py", "--run-id", "fx"])
    try:
        verify_soak.main()
    finally:
        monkey.undo()
    return json.loads((run_dir / "consistency_report.json")
                      .read_text(encoding="utf-8"))


@pytest.fixture()
def fx_env(tmp_path):
    """基础夹具：1 个 done/machine 任务 + 正常采样/时间线。"""
    sys.path.insert(0, str(SOAK_DIR))
    home = tmp_path / "runs" / "fx" / "home"
    home.mkdir(parents=True)
    mid = _mk_mission(home, "fx-tp-00")
    kind_map = {mid: {"kind": "tp", "name": "soak-tp-00-fx", "root": "n1"}}
    return tmp_path, mid, kind_map


def test_v48_base_fixture_sane(fx_env):
    """正对照：合规夹具 → 资源/并发/重启(未计划UNVERIFIED)/SPEC 一致。"""
    tmp_path, mid, kind_map = fx_env
    run_dir = _mk_run(tmp_path, mid=mid, kind_map=kind_map,
                      samples=_base_samples(), timeline=_base_timeline())
    report = _run_verify(tmp_path, run_dir)
    v = report["verdicts"]
    assert v["resource_thresholds"]["result"] in ("PASS", "UNVERIFIED")
    if v["resource_thresholds"]["result"] == "UNVERIFIED":
        assert "per_mission" in v["resource_thresholds"]["detail"]  # 1done样本不足
    assert v["concurrency_utilization>=80%"]["result"] == "PASS"
    assert v["spec_thresholds_match"]["result"] == "PASS"
    assert v["restart_recovery"]["result"] == "UNVERIFIED"  # 未计划重启探针
    assert v["normal_missions_closed"]["result"] == "PASS"
    assert v["calls_accounting"]["result"] in ("PASS", "UNVERIFIED")


def test_v48_missing_metrics_not_pass(fx_env):
    """反例A（Codex：删线程/句柄/子进程指标仍 PASS）——现在必须
    UNVERIFIED，句柄不得补 0/min。"""
    tmp_path, mid, kind_map = fx_env
    samples = _base_samples(drop_keys=("proc_handles", "proc_threads",
                                       "children_n"))
    run_dir = _mk_run(tmp_path, mid=mid, kind_map=kind_map,
                      samples=samples, timeline=_base_timeline())
    report = _run_verify(tmp_path, run_dir)
    v = report["verdicts"]
    assert v["resource_thresholds"]["result"] == "UNVERIFIED"
    d = v["resource_thresholds"]["detail"]
    assert "handle_slope" in d and "thread_fluct" in d \
        and "children_reclaim" in d


def test_v48_known_fail_beats_unverified(fx_env):
    """反例B（Codex：RSS 23999 MiB/h + done 少被降级 UNVERIFIED）——
    已知硬失败必须 FAIL（FAIL 优先于 UNVERIFIED）。"""
    tmp_path, mid, kind_map = fx_env
    samples = _base_samples()
    for r in samples[len(samples) // 2:]:
        r["proc_rss_mb"] = 24000.0
    run_dir = _mk_run(tmp_path, mid=mid, kind_map=kind_map,
                      samples=samples, timeline=_base_timeline())
    report = _run_verify(tmp_path, run_dir)
    v = report["verdicts"]
    assert v["resource_thresholds"]["result"] == "FAIL"
    assert "RSS斜率" in v["resource_thresholds"]["detail"]


def test_v48_restart_evidence_required(fx_env):
    """反例C（Codex：删重启证据仍 PASS）——计划了重启探针但库内无
    闭合/取消事件 → FAIL；自报布尔不能替代库内证据。"""
    tmp_path, mid, kind_map = fx_env
    kind_map = dict(kind_map)
    kind_map[mid]["name"] = "soak-restart-probe-fx"   # 计划含重启探针
    run_dir = _mk_run(tmp_path, mid=mid, kind_map=kind_map,
                      samples=_base_samples(), timeline=_base_timeline(),
                      restart_probe={"killed": True, "restarted": True,
                                     "final_status": "active"})
    report = _run_verify(tmp_path, run_dir)
    v = report["verdicts"]
    # mission 已闭合（done）但库内无 attempt_cancelled（kill 事件）→ FAIL
    assert v["restart_recovery"]["result"] == "FAIL"
    assert "cancel_event=False" in v["restart_recovery"]["detail"]


def test_v48_threshold_drift_fails(fx_env):
    """run_config.thresholds 与 SPEC 漂移（句柄 25 冒充 20、波动带冒改
    25）→ spec_thresholds_match FAIL，不得冒称按本 SPEC 通过。"""
    tmp_path, mid, kind_map = fx_env
    run_dir = _mk_run(tmp_path, mid=mid, kind_map=kind_map,
                      samples=_base_samples(), timeline=_base_timeline(),
                      thresholds={"rss_slope_mib_h": 60,
                                  "per_mission_mib": 0.75,
                                  "handle_slope_per_min": 25,
                                  "thread_fluct_base": 25,
                                  "thread_fluct_per_c": 3,
                                  "thread_quiescent": 5,
                                  "util_min": 0.80})
    report = _run_verify(tmp_path, run_dir)
    v = report["verdicts"]
    assert v["spec_thresholds_match"]["result"] == "FAIL"
    d = v["spec_thresholds_match"]["detail"]
    assert "handle_slope_per_min" in d and "thread_fluct" in d


def test_v48_window_below_threshold_fails(fx_env):
    """任一完整 600s 窗口 <80% → 并发 FAIL（全窗遍历，不再只看整体）。"""
    tmp_path, mid, kind_map = fx_env
    tl = _base_timeline(span=1800.0)
    # 第二个 attempt 只覆盖第一个窗口 → 后续窗口利用率 50% <80%
    tl = [e for e in tl if e["attempt"] != "a1"]
    tl += [{"mid": "m", "node": "n", "attempt": "a1", "phase": "start",
            "t": 1000.0},
           {"mid": "m", "node": "n", "attempt": "a1", "phase": "end",
            "t": 1000.0 + 600.0}]
    run_dir = _mk_run(tmp_path, mid=mid, kind_map=kind_map,
                      samples=_base_samples(), timeline=tl)
    report = _run_verify(tmp_path, run_dir)
    v = report["verdicts"]
    assert v["concurrency_utilization>=80%"]["result"] == "FAIL"
    assert "窗口未达标" in v["concurrency_utilization>=80%"]["detail"]


def test_v48_short_wall_fails_duration(fx_env):
    """实际 wall 不足计划时长（−30s 容差）→ duration_wall FAIL
    （config 写 3600 不等于实际满时长）。"""
    tmp_path, mid, kind_map = fx_env
    run_dir = _mk_run(tmp_path, mid=mid, kind_map=kind_map,
                      samples=_base_samples(), timeline=_base_timeline(),
                      wall=3550.0, duration=3600.0)
    report = _run_verify(tmp_path, run_dir)
    v = report["verdicts"]
    assert v["duration_wall"]["result"] == "FAIL"
    assert "3550" in v["duration_wall"]["detail"]


def test_v48_ordinary_cancel_not_restart(fx_env):
    """V2（Codex 三轮）：无重启、仅普通 attempt_cancelled、自报
    killed/restarted=false → restart_recovery FAIL（不再能靠库内
    done+cancel 通过）。"""
    import sqlite3
    tmp_path, mid, kind_map = fx_env
    kind_map = dict(kind_map)
    kind_map[mid]["name"] = "soak-restart-probe-fx"
    run_dir = _mk_run(tmp_path, mid=mid, kind_map=kind_map,
                      samples=_base_samples(), timeline=_base_timeline(),
                      restart_probe={"killed": False, "restarted": False,
                                     "final_status": "done"})
    c = sqlite3.connect(next((run_dir / "home").glob("*/mission.db")))
    c.execute("INSERT INTO events(mission_id,at,type,node_id,payload_json)"
              " VALUES(?,?,?,?,?)",
              (mid, "2026-10-11T00:00:00", "attempt_cancelled", "n1",
               '{"reason":"ordinary_cancel_not_restart"}'))
    c.commit()
    c.close()
    report = _run_verify(tmp_path, run_dir)
    v = report["verdicts"]
    assert v["restart_recovery"]["result"] == "FAIL"
    assert "self_killed=False" in v["restart_recovery"]["detail"]


def test_v48_duplicate_finished_not_denominator(fx_env):
    """V3（Codex 三轮）：summary 同一任务重复 40 次——done 分母按去重
    计，资源项不得由 UNVERIFIED 转 PASS。"""
    tmp_path, mid, kind_map = fx_env
    run_dir = _mk_run(tmp_path, mid=mid, kind_map=kind_map,
                      samples=_base_samples(), timeline=_base_timeline())
    summ = json.loads((run_dir / "run_summary.json").read_text())
    summ["finished"] = [{"mid": mid, "status": "done"} for _ in range(40)]
    (run_dir / "run_summary.json").write_text(json.dumps(summ))
    report = _run_verify(tmp_path, run_dir)
    v = report["verdicts"]
    assert v["resource_thresholds"]["result"] == "UNVERIFIED"
    assert "per_mission" in v["resource_thresholds"]["detail"]
    m = report["checks"]["resource_metrics"]
    assert m["done_dedup_total"] == 1 and m["dup_finished_entries"] == 39


def test_v48_quiescent_missing_metric_unverified(fx_env):
    """V4（Codex 三轮）：末静止点行缺 children_n → 资源项 UNVERIFIED
    （不得借其他样本曾有指标放行）。"""
    tmp_path, mid, kind_map = fx_env
    samples = _base_samples()
    samples[-1].pop("children_n")
    run_dir = _mk_run(tmp_path, mid=mid, kind_map=kind_map,
                      samples=samples, timeline=_base_timeline())
    report = _run_verify(tmp_path, run_dir)
    v = report["verdicts"]
    assert v["resource_thresholds"]["result"] == "UNVERIFIED"
    assert "quiescent_metrics" in v["resource_thresholds"]["detail"]


def test_v48_forged_spec_sha_fails(fx_env):
    """V1（Codex 三轮）：spec_sha256=伪造值 → FAIL（不属于任何已知
    SPEC 版本）。"""
    tmp_path, mid, kind_map = fx_env
    run_dir = _mk_run(tmp_path, mid=mid, kind_map=kind_map,
                      samples=_base_samples(), timeline=_base_timeline())
    cfg = json.loads((run_dir / "run_config.json").read_text())
    cfg["spec_sha256"] = "invalid-does-not-match"
    (run_dir / "run_config.json").write_text(json.dumps(cfg))
    report = _run_verify(tmp_path, run_dir)
    v = report["verdicts"]
    assert v["spec_thresholds_match"]["result"] == "FAIL"
    assert "不属于任何已知 SPEC" in v["spec_thresholds_match"]["detail"]


def test_v48_quiescent_null_metric_rejected(fx_env):
    """四轮变体①：末静止点 children_n=null——键存在但值无效 →
    资源 UNVERIFIED（null 不得代表 0）。"""
    tmp_path, mid, kind_map = fx_env
    samples = _base_samples()
    samples[-1]["children_n"] = None
    run_dir = _mk_run(tmp_path, mid=mid, kind_map=kind_map,
                      samples=samples, timeline=_base_timeline())
    report = _run_verify(tmp_path, run_dir)
    v = report["verdicts"]
    assert v["resource_thresholds"]["result"] == "UNVERIFIED"
    assert "quiescent_metrics" in v["resource_thresholds"]["detail"]


def test_v48_probe_timeline_deleted_rejected(fx_env):
    """四轮变体②：声明探针预算但删除全部 probe 时间线 →
    total_concurrency_cap FAIL（计量缺失不得静默降回主峰）。"""
    tmp_path, mid, kind_map = fx_env
    run_dir = _mk_run(tmp_path, mid=mid, kind_map=kind_map,
                      samples=_base_samples(), timeline=_base_timeline())
    tl = json.loads((run_dir / "timeline.json").read_text())
    # 只留主负载事件（无任何 probe 事件）
    (run_dir / "timeline.json").write_text(json.dumps(tl))
    report = _run_verify(tmp_path, run_dir)
    v = report["verdicts"]
    assert v["total_concurrency_cap"]["result"] == "FAIL"
    assert "无探针事件" in v["total_concurrency_cap"]["detail"]


def test_v48_all_done_before_warmup_unverified(fx_env):
    """四轮变体③：所有 done 的完成时点被改到预热之前 → done_in_warm=0
    → per_mission UNVERIFIED（不得回退扩大分母）。"""
    import datetime as _dt
    tmp_path, mid, kind_map = fx_env
    run_dir = _mk_run(tmp_path, mid=mid, kind_map=kind_map,
                      samples=_base_samples(), timeline=_base_timeline())
    # 库内 attempt ended_at 改到很早（库侧权威时点）
    import sqlite3
    db = next((run_dir / "home").glob("*/mission.db"))
    c = sqlite3.connect(db)
    c.execute("UPDATE attempts SET ended_at='2020-01-01T00:00:00'"
              " WHERE ended_at IS NOT NULL")
    c.commit()
    c.close()
    report = _run_verify(tmp_path, run_dir)
    v = report["verdicts"]
    assert v["resource_thresholds"]["result"] == "UNVERIFIED"
    assert "per_mission" in v["resource_thresholds"]["detail"]


def test_v48_fake_done_mids_fail(fx_env):
    """四轮变体④：summary 换成 200 个库外 mid → 资源 FAIL（伪造 done
    计数），分母仍按库。"""
    tmp_path, mid, kind_map = fx_env
    run_dir = _mk_run(tmp_path, mid=mid, kind_map=kind_map,
                      samples=_base_samples(), timeline=_base_timeline())
    summ = json.loads((run_dir / "run_summary.json").read_text())
    summ["finished"] = [{"mid": f"fake-{i}", "status": "done"}
                        for i in range(200)]
    (run_dir / "run_summary.json").write_text(json.dumps(summ))
    report = _run_verify(tmp_path, run_dir)
    v = report["verdicts"]
    assert v["resource_thresholds"]["result"] == "FAIL"
    assert "库外 mid" in v["resource_thresholds"]["detail"]


def test_v48_restart_needs_kill_reason_and_order(fx_env):
    """四轮重启夹具①：普通取消事件＋generation=2＋自报 true→ FAIL
    （取消原因无 kill 语义/无有序性）；②无代际表→ UNVERIFIED。"""
    import sqlite3
    tmp_path, mid, kind_map = fx_env
    # ①：普通取消 + gen=2 + 自报 true
    kind_map1 = dict(kind_map)
    kind_map1[mid]["name"] = "soak-restart-probe-fx"
    run_dir = _mk_run(tmp_path, mid=mid, kind_map=kind_map1,
                      samples=_base_samples(), timeline=_base_timeline(),
                      restart_probe={"killed": True, "restarted": True,
                                     "final_status": "done",
                                     "probe_calls": 5})
    db = next((run_dir / "home").glob("*/mission.db"))
    c = sqlite3.connect(db)
    c.execute("INSERT INTO events(mission_id,at,type,node_id,payload_json)"
              " VALUES(?,?,?,?,?)",
              (mid, "2026-10-12T00:00:00", "attempt_cancelled", "n1",
               '{"reason":"ordinary_cancel_not_restart"}'))
    c.execute("UPDATE run_control SET generation=2 WHERE id=1")
    c.commit()
    c.close()
    report = _run_verify(tmp_path, run_dir)
    v = report["verdicts"]
    assert v["restart_recovery"]["result"] == "FAIL"
    assert "kill_reason=False" in v["restart_recovery"]["detail"]
    # ②：无代际表 → UNVERIFIED
    tmp_path2, mid2, kind_map2 = fx_env
    kind_map3 = dict(kind_map2)
    kind_map3[mid2]["name"] = "soak-restart-probe-fx2"
    run_dir2 = _mk_run(tmp_path2, mid=mid2, kind_map=kind_map3,
                       samples=_base_samples(), timeline=_base_timeline(),
                       restart_probe={"killed": True, "restarted": True,
                                      "final_status": "done",
                                      "probe_calls": 5})
    db2 = next((run_dir2 / "home").glob("*/mission.db"))
    c2 = sqlite3.connect(db2)
    c2.execute("DROP TABLE run_control")
    c2.commit()
    c2.close()
    report2 = _run_verify(tmp_path2, run_dir2)
    v2 = report2["verdicts"]
    assert v2["restart_recovery"]["result"] == "UNVERIFIED"


def test_v48_nan_rss_rejected(fx_env):
    """五轮 V1：稳定期一条 RSS=NaN——剔除并不得 PASS（1/8=12.5%>10%
    → UNVERIFIED，detail 含 NaN/Inf；占比≤10% 时为 FAIL——NaN 混入
    拟合会让整条回归线变 NaN 而"全部门槛内"）。"""
    import math
    tmp_path, mid, kind_map = fx_env
    samples = _base_samples()
    samples[3]["proc_rss_mb"] = float("nan")
    run_dir = _mk_run(tmp_path, mid=mid, kind_map=kind_map,
                      samples=samples, timeline=_base_timeline())
    report = _run_verify(tmp_path, run_dir)
    v = report["verdicts"]
    assert v["resource_thresholds"]["result"] in ("FAIL", "UNVERIFIED")
    assert "NaN/Inf" in v["resource_thresholds"]["detail"]


def test_v48_partial_probe_timeline_rejected(fx_env):
    """五轮 V2：删部分探针区间（保留 1/2）→ total_concurrency_cap FAIL
    （执行集对账：probe attempt 数 < 库内探针库 attempts）。"""
    import sqlite3
    tmp_path, mid, kind_map = fx_env
    # 造探针库：库内 2 条 attempts（对应 2 个 probe attempt 区间）
    kind_map = dict(kind_map)
    kind_map[mid]["name"] = "soak-restart-probe-fx"
    run_dir = _mk_run(tmp_path, mid=mid, kind_map=kind_map,
                      samples=_base_samples(), timeline=_base_timeline(),
                      restart_probe={"killed": True, "restarted": True,
                                     "final_status": "done",
                                     "probe_calls": 2})
    db = next((run_dir / "home").glob("*/mission.db"))
    c = sqlite3.connect(db)
    # 库内补第 2 条 attempts（探针第 2 次执行被 kill → cancelled）：
    # _mk_mission 只落 1 条 done attempt，对账要求"时间线 probe
    # attempt 数 == 库内 attempts 数"，须先补齐成 2↔2 严格一致
    # 造独立主任务库供主负载利用率对账，避免探针库混入主负载
    mid_main = _mk_mission(tmp_path / "runs" / "fx" / "home", "fx-tp-main")
    kind_map[mid_main] = {"kind": "tp", "name": "soak-tp-main", "root": "n1"}
    db_main = tmp_path / "runs" / "fx" / "home" / "fx-tp-main" / "mission.db"
    c_m = sqlite3.connect(db_main)
    for i in range(2):
        c_m.execute(
            "INSERT OR REPLACE INTO attempts(id,node_id,mission_id,backend,status,"
            "workdir,started_at,ended_at) VALUES(?,?,?,?,?,?,?,?)",
            (f"a{i}", "n1", mid_main, "stub", "done", "",
             "2026-10-12T00:00:00", "2026-10-12T00:30:00"))
    c_m.commit()
    c_m.close()

    _node = c.execute("SELECT node_id FROM attempts LIMIT 1").fetchone()[0]
    c.execute(
        "INSERT INTO attempts(id,node_id,mission_id,backend,status,"
        "workdir,started_at,ended_at) VALUES(?,?,?,?,?,?,?,?)",
        ("probe-kill-2", _node, mid, "stub", "cancelled", "",
         "2026-10-12T00:01:00", "2026-10-12T00:02:00"))
    c.execute("UPDATE run_control SET generation=2 WHERE id=1")
    c.execute(
        "INSERT INTO events(mission_id,at,type,node_id,payload_json)"
        " VALUES(?,?,?,?,?)",
        (mid, "2026-10-12T00:00:00", "attempt_cancelled", "n1",
         json.dumps({"reason": "RunManager.stop(kill)"})))
    c.commit()
    probe_attempt_ids = [r[0] for r in c.execute("SELECT id FROM attempts ORDER BY id").fetchall()]
    c.close()
    # 更新 summary 保持对账平
    summ = json.loads((run_dir / "run_summary.json").read_text(encoding="utf-8"))
    summ["finished"] = [{"mid": mid, "status": "done"}, {"mid": mid_main, "status": "done"}]
    summ["admit"] = {"admitted": [mid, mid_main]}
    summ["backend_calls"] = 2
    summ["backend_calls_probe"] = len(probe_attempt_ids)
    (run_dir / "run_summary.json").write_text(json.dumps(summ), encoding="utf-8")
    # 时间线：2 个主负载区间 + 2 个 probe 区间（与库内 attempts 真实 ID 严格对齐）
    tl = {"timeline": []}
    for i in range(2):
        tl["timeline"] += [
            {"mid": mid_main, "node": "n1", "attempt": f"a{i}", "phase": "start",
             "t": 1000.0},
            {"mid": mid_main, "node": "n1", "attempt": f"a{i}", "phase": "end",
             "t": 1000.0 + 600.0}]
    for aid in probe_attempt_ids:
        tl["timeline"] += [
            {"mid": mid, "node": _node, "attempt": aid, "phase": "start",
             "t": 1000.0, "probe": True},
            {"mid": mid, "node": _node, "attempt": aid, "phase": "end",
             "t": 1000.0 + 3.0, "probe": True}]
    (run_dir / "timeline.json").write_text(json.dumps(tl))
    report = _run_verify(tmp_path, run_dir)
    v = report["verdicts"]
    assert v["total_concurrency_cap"]["result"] == "PASS"  # 前置：全量对齐
    # 删掉 1 个 probe 区间（保留 1/2）→ 对账 FAIL
    tl2 = {"timeline": [e for e in tl["timeline"]
                        if not (e.get("probe") and e["attempt"] == probe_attempt_ids[-1])]}
    (run_dir / "timeline.json").write_text(json.dumps(tl2))
    report = _run_verify(tmp_path, run_dir)
    v = report["verdicts"]
    assert v["total_concurrency_cap"]["result"] == "FAIL"
    assert "执行集不完整" in v["total_concurrency_cap"]["detail"]


def test_v48_negation_reason_not_kill(fx_env):
    """五轮 V3：取消原因含 "not_kill" 子串 → 不得算 kill 证据
    （结构化 reason 前缀核对）。"""
    import sqlite3
    tmp_path, mid, kind_map = fx_env
    kind_map = dict(kind_map)
    kind_map[mid]["name"] = "soak-restart-probe-fx"
    run_dir = _mk_run(tmp_path, mid=mid, kind_map=kind_map,
                      samples=_base_samples(), timeline=_base_timeline(),
                      restart_probe={"killed": True, "restarted": True,
                                     "final_status": "done",
                                     "probe_calls": 5})
    db = next((run_dir / "home").glob("*/mission.db"))
    c = sqlite3.connect(db)
    c.execute("UPDATE run_control SET generation=2 WHERE id=1")
    c.execute(
        "INSERT INTO events(mission_id,at,type,node_id,payload_json)"
        " VALUES(?,?,?,?,?)",
        (mid, "2026-10-12T00:00:00", "attempt_cancelled", "n1",
         json.dumps({"reason": "ordinary_cancel_not_kill"})))
    c.commit()
    c.close()
    report = _run_verify(tmp_path, run_dir)
    v = report["verdicts"]
    assert v["restart_recovery"]["result"] == "FAIL"
    assert "kill_reason=False" in v["restart_recovery"]["detail"]
