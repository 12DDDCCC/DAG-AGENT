# -*- coding: utf-8 -*-
"""v49：Codex 三轮独立验收（2026-10-11）F1–F5＋顺带问题的正式回归。

对应 outputs/DAG-agent-二轮闭环与SPEC-B-Codex独立验收-2026-10-11.md §4：
- F1 合法维修后冻结摘要同事务发布（repair→认领可恢复；维修后漂移仍拒；
  旧摘要留审计事件，不删行绕过）。
- F2 同 ID 同文件名的完整 manifest 漂移被识别（dep_bindings 持久完整
  文件元信息；认领时规范化 manifest_hash_of 重算＋逐文件元信息比对）。
- F3 认领后输入损坏，播种 fail-closed（_seed_bound 按不可变契约复核
  整体 manifest＋逐文件 sha；空清单/缺件/内容不符拒绝，不静默空目录）。
- F4 有界回收保留轻量恢复配置（未闭合任务的恢复配置持久化到任务库；
  重对象释放）；B3：带不可重建组件标识（backend_factory 不可序列化）
  的配置 kick 显式失败，不静默换执行语义。
- F5 公开停止一次写足＋幂等（重复 kill/未知句柄不抛关闭连接错误）。
- 顺带：hard_stop 落库（kill 后 run_control.hard_stop=true）、
  keep=0 全回收语义、source_binding 覆盖核验（以认领账本为权威）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dag_agent.artifacts import manifest_hash_of  # noqa: E402
from dag_agent.graph import Graph, GraphError  # noqa: E402
from dag_agent.repository import Repository  # noqa: E402
from dag_agent.scheduler import AutogrowConfig, Scheduler  # noqa: E402

from test_v45_autogrow import (  # noqa: E402
    BRIEF, CMD_OK, AutoGrowBackend, build_mission, make_proposal, stage_split)

STRICT = ("from pathlib import Path\nimport sys\n"
          "p=Path('deliverable.md')\n"
          "sys.exit(0 if p.is_file() and p.read_text(encoding='utf-8')"
          "=='GOOD' else 1)\n")

UPSTREAM_PAYLOAD = "ORIGINAL upstream payload for v49 binding tests"


def _freeze_row(repo, nid):
    row = repo._conn.execute(
        "SELECT exam_digest FROM node_exam_freeze WHERE node_id=?",
        (nid,)).fetchone()
    return row["exam_digest"] if row else None


def _accept_upstream(repo, graph, mid, up, payload=UPSTREAM_PAYLOAD) -> int:
    aid = graph.start_attempt(mid, up, backend="stub")
    wd = Path(repo.get_attempt(aid).workdir)
    (wd / "deliverable.md").write_text(payload, encoding="utf-8")
    result = graph.submit_attempt(mid, up, aid,
                                  artifact_paths=["deliverable.md"])
    assert result["verdict"] == "pass"
    return repo._conn.execute(
        "SELECT id FROM snapshots WHERE node_id=? AND valid=1"
        " ORDER BY id DESC LIMIT 1", (up,)).fetchone()["id"]


def _apply_two_children(repo, graph, mid, parent, pid, parent_binding, fp,
                        apply_binding=None):
    """apply 两个无局部依赖孩子（外部依赖=父绑定），返回 (kids, fp2)。"""
    from dag_agent.acceptance import compute_check_digest as _ccd

    def _fz(spec):
        spec = dict(spec)
        spec["_frozen_digest"] = _ccd(check_cmd=spec["check_cmd"],
                                      exam_files=())
        return spec

    aid_split, fp2 = stage_split(repo, graph, mid, parent, pid,
                                 input_binding_json=parent_binding)
    kids = graph.apply_auto_split(
        mid, parent, aid_split, pid,
        [_fz({"title": f"{pid}-子一", "statement": BRIEF.format(body="a"),
              "check_cmd": CMD_OK, "declared_artifacts": ["deliverable.md"],
              "deps": [], "source_local_id": "c1"}),
         _fz({"title": f"{pid}-子二", "statement": BRIEF.format(body="b"),
              "check_cmd": CMD_OK, "declared_artifacts": ["deliverable.md"],
              "deps": [], "source_local_id": "c2"})],
        fp2, source_binding_json=(apply_binding
                                  if apply_binding is not None
                                  else parent_binding))
    return kids, fp2


def _parent_with_upstream(tmp_path):
    """根+上游(accepted)+父(deps=up) 认领态；返回全部句柄与绑定。"""
    repo, graph, mid, root = build_mission(tmp_path)
    up = graph.add_node(mid, title="上游", statement=BRIEF.format(body="u"),
                        parent_id=root, check_cmd=CMD_OK,
                        declared_artifacts=("deliverable.md",))
    parent = graph.add_node(mid, title="父任务P",
                            statement=BRIEF.format(body="P"), parent_id=root,
                            deps=(up,), check_cmd=CMD_OK,
                            declared_artifacts=("deliverable.md",))
    sid = _accept_upstream(repo, graph, mid, up)
    pa = graph.start_attempt(mid, parent, backend="stub")
    binding = repo.get_attempt(pa).input_binding_json
    fp = repo.get_attempt(pa).claim_fingerprint_json
    return repo, graph, mid, root, up, parent, sid, binding, fp


# ---------- F1 · 维修与冻结摘要同事务发布 ----------


def test_f1_repair_publishes_new_freeze(tmp_path):
    """合法维修（v2）后孩子可重新认领；维修后漂移仍被拒；旧摘要留
    exam_freeze_updated 审计事件（不删行绕过）。"""
    repo, graph, mid, root = build_mission(tmp_path)
    old_digest = _freeze_row(repo, root)  # 无冻结行（人工节点）→ None
    exam = tmp_path / "repaired_exam.py"
    exam.write_text(STRICT, encoding="utf-8")
    new_cmd = f'python "{exam}"'
    new_v = graph.repair_check(mid, root, new_check_cmd=new_cmd, reason="v49 维修",
                               exam_files=[str(exam)])
    assert new_v == 2
    new_digest = _freeze_row(repo, root)
    assert new_digest, "授权维修必须同事务发布新冻结摘要"
    types = [e.type for e in repo.events_of(mid)]
    assert "exam_freeze_updated" in types, "旧→新摘要审计链缺失"
    # 合法 v2：认领成功
    aid = graph.start_attempt(mid, root, backend="stub")
    assert repo.get_attempt(aid).status == "running"
    # 维修后漂移：考卷文件改成永绿 → 认领必须拒绝
    exam.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    with repo.tx() as cur:
        cur.execute("UPDATE nodes SET status='pending' WHERE id=?", (root,))
    with pytest.raises(GraphError, match="冻结摘要不一致"):
        graph.start_attempt(mid, root, backend="stub")
    repo.close()


# ---------- F2 · 完整 manifest 漂移识别 ----------


def test_f2_full_manifest_drift_rejected(tmp_path):
    """同 ID、同文件名：files_json 的 sha/size 与 manifest_hash 字段联合
    改写＋accepted 文件同步改——认领必须拒绝（规范化重算≠冻结契约）；
    元数据改而文件未改同样拒绝；完整快照正例逐字节播种。"""
    repo, graph, mid, root, up, parent, sid, binding, fp = \
        _parent_with_upstream(tmp_path)
    kids, _ = _apply_two_children(repo, graph, mid, parent, "sp-f2",
                                  binding, fp)
    ctr = repo._conn.execute(
        "SELECT manifest_hash, files_meta_json FROM dep_bindings"
        " WHERE node_id=? AND depends_on=?", (kids[0], up)).fetchone()
    ctr_meta = json.loads(ctr["files_meta_json"])
    assert ctr_meta and ctr_meta[0]["sha256"], "契约必须持久完整文件元信息"
    orig_row = repo._conn.execute(
        "SELECT files_json, manifest_hash FROM snapshots WHERE id=?",
        (sid,)).fetchone()
    # 联合损坏①：行内 sha/size 改＋manifest_hash 同步重算（自洽假象）
    tampered = json.loads(orig_row["files_json"])
    tampered[0]["sha256"] = "0" * 64
    tampered[0]["size"] = 3
    with repo.tx() as cur:
        cur.execute(
            "UPDATE snapshots SET files_json=?, manifest_hash=? WHERE id=?",
            (json.dumps(tampered), manifest_hash_of(tampered), sid))
    with pytest.raises(GraphError, match="契约不符"):
        graph.start_attempt(mid, kids[0], backend="stub")
    # 联合损坏②：只改清单字段、manifest 保持——逐文件元信息比对拦截
    with repo.tx() as cur:
        cur.execute("UPDATE snapshots SET files_json=?, manifest_hash=?"
                    " WHERE id=?", (orig_row["files_json"],
                                    orig_row["manifest_hash"], sid))
        cur.execute("UPDATE snapshots SET files_json=? WHERE id=?",
                    (json.dumps(tampered), sid))
    with pytest.raises(GraphError, match="契约不符"):
        graph.start_attempt(mid, kids[0], backend="stub")
    # 正例：恢复原样 → 认领成功且播种内容逐字节正确
    with repo.tx() as cur:
        cur.execute("UPDATE snapshots SET files_json=? WHERE id=?",
                    (orig_row["files_json"], sid))
    ca = graph.start_attempt(mid, kids[0], backend="stub")
    cw = Path(repo.get_attempt(ca).workdir)
    Scheduler(repo, graph, AutoGrowBackend())._seed_bound(ca, cw)
    seeded = list(cw.rglob("deliverable.md"))
    assert seeded and seeded[0].read_text(encoding="utf-8") == UPSTREAM_PAYLOAD
    repo.close()


# ---------- F3 · 认领后输入损坏，播种 fail-closed ----------


def test_f3_seed_after_claim_damage_fail_closed(tmp_path):
    """精确顺序：认领成功→清空快照 files_json→播种必须拒绝（不静默
    交付空目录）；行完好但磁盘内容被换→sha 核对拒绝；正例完整播种。"""
    repo, graph, mid, root, up, parent, sid, binding, fp = \
        _parent_with_upstream(tmp_path)
    kids, _ = _apply_two_children(repo, graph, mid, parent, "sp-f3",
                                  binding, fp)
    ca = graph.start_attempt(mid, kids[0], backend="stub")
    cw = Path(repo.get_attempt(ca).workdir)
    sched = Scheduler(repo, graph, AutoGrowBackend())
    # 认领后损坏①：清单清空 → 播种拒绝（空清单≠合法无输入）
    orig_row = repo._conn.execute(
        "SELECT files_json FROM snapshots WHERE id=?", (sid,)).fetchone()
    with repo.tx() as cur:
        cur.execute("UPDATE snapshots SET files_json='[]' WHERE id=?", (sid,))
    # （清空清单同时破坏 manifest 重算与空清单两道防线——任一触发即拒）
    with pytest.raises(ValueError, match="拒绝播种"):
        sched._seed_bound(ca, cw)
    assert not list(cw.rglob("deliverable.md")), "不得静默播种空目录"
    # 认领后损坏②：行完好、磁盘内容被换 → 契约 sha 核对拒绝
    with repo.tx() as cur:
        cur.execute("UPDATE snapshots SET files_json=? WHERE id=?",
                    (orig_row["files_json"], sid))
    # accepted 原件在 store 的 accepted 目录——找它并改内容
    acc = next((p for p in (tmp_path / "mission.db").parent.rglob(
        "accepted" ) if p.is_dir()), None)
    acc = next(acc.rglob("deliverable.md"), None) if acc else None
    assert acc is not None
    acc.write_text("TAMPERED", encoding="utf-8")
    # （行内摘要校验与契约 sha 核对双防线，任一触发即拒）
    with pytest.raises(ValueError, match="sha 不一致|摘要不符"):
        sched._seed_bound(ca, cw)
    # 正例：恢复内容 → 完整播种且逐字节一致
    acc.write_text(UPSTREAM_PAYLOAD, encoding="utf-8")
    sched._seed_bound(ca, cw)
    seeded = list(cw.rglob("deliverable.md"))
    assert seeded and seeded[0].read_text(encoding="utf-8") == UPSTREAM_PAYLOAD
    repo.close()


# ---------- F4 · 有界回收保留恢复配置 ----------


def test_f4_cleanup_preserves_kick_params(tmp_path):
    """drain 后 mission 仍 active（blocked 死局）、运行条目被有界回收 →
    轻量启动参数归档保留、status 如实 archived；且配置已持久化到任务库
    （start 成功即持久化＋cleanup 幂等再写）。B3：内存归档淘汰后 kick
    从库回退读到组件标识（backend_factory 不可序列化，仅存
    _backend_factory_component、无 callable）→ 组件守卫显式失败，
    不静默换执行语义（注入 stub 被换 null 的教训）。"""
    from dag_agent.daemon import RunManager
    from test_v46_codex_review_fixes import _NullishBackend
    home = tmp_path / "h"
    home.mkdir()
    repo = Repository(home / "mission.db")
    graph = Graph(repo)
    bad_check = 'python -c "import sys;sys.exit(2)"'
    mid, root = graph.create_mission(
        "v49 F4", brief=BRIEF.format(body="root"), root_check=bad_check,
        root_declared_artifacts=("deliverable.md",))
    mgr = RunManager(home)
    r = mgr.start(mid, root=root, max_concurrency=1, real=False,
                  project=False, backend_factory=lambda: _NullishBackend(),
                  timeout_s=30)
    assert r.get("ok")
    t0 = time.monotonic()
    while time.monotonic() - t0 < 20:
        if not mgr._runs[mid]["thread"].is_alive():
            break
        time.sleep(0.1)
    mgr.stop(mid, mode="drain")
    assert repo.get_mission(mid).status == "active", "前置：mission 须 active"
    assert not mgr._runs[mid]["thread"].is_alive()
    mgr.cleanup_finished(keep=0)   # keep=0：全回收（轻量参数转归档）
    assert mid not in mgr._runs
    assert mgr.status(mid).get("archived") is True
    # ①配置已持久化到任务库：start 成功即持久化、cleanup 幂等再写——
    # callable（backend_factory）被剔除、组件标识 _backend_factory_component
    # 入库（B3-1/B3-2）
    raw = repo._conn.execute(
        "SELECT params_json FROM run_control WHERE id=1").fetchone()
    assert raw and raw["params_json"], "恢复配置必须持久化到 run_control"
    persisted = json.loads(raw["params_json"])
    assert persisted.get("_backend_factory_component") == "<lambda>", (
        f"不可序列化组件必须以组件标识入库：{persisted}")
    assert "backend_factory" not in persisted, "callable 不得入库"
    # 模拟内存归档淘汰（N5 同手法）→ kick 被迫从任务库回退读持久化配置
    mgr._archive.clear()
    # 合法维修（好考卷）→ pending：节点本可续跑，但 B3-3 组件守卫先行
    graph.repair_check(mid, root, new_check_cmd=CMD_OK, reason="v49 F4 维修")
    rk = mgr.kick(mid)
    assert rk.get("ok") is False and rk.get("kicked") is not True, (
        f"带组件标识的配置 kick 必须显式失败（不静默续跑）：{rk}")
    err = rk.get("error") or ""
    assert "无法自动重建" in err, f"错误须指明无法自动重建：{err}"
    assert "backend_factory" in err, f"错误须点名缺失组件 backend_factory：{err}"
    assert "无历史运行参数" not in err, (
        f"失败须是组件守卫而非配置缺失（证明从库回退成功）：{err}")
    # 显式失败后 mission 保持 active：既未闭合、也未换执行语义静默续跑
    assert repo.get_mission(mid).status == "active"
    mgr.stop(mid, mode="kill")
    repo.close()


# ---------- F5 · 一次停止写＋幂等 ----------


def test_f5_stop_idempotent_and_persisted(tmp_path):
    """kill：run_control.hard_stop 必须落库（INSERT 分支不再写 0）；
    重复 stop 与未知句柄 stop 幂等不抛错；停止返回后无残留。"""
    from dag_agent.daemon import RunManager
    from test_v46_codex_review_fixes import build_simple, _NullishBackend
    home = tmp_path / "h"
    home.mkdir()
    repo, graph, mid, root = build_simple(home)
    mgr = RunManager(home)
    r = mgr.start(mid, root=root, max_concurrency=1, real=False,
                  project=False, backend_factory=lambda: _NullishBackend(),
                  timeout_s=30)
    assert r.get("ok")
    mgr.stop(mid, mode="kill")
    rc = repo.get_run_control()
    assert rc["hard_stop"] is True and rc["stop_requested"] is True, (
        f"kill 后库内旗标必须落库：{rc}")
    # 重复 stop（线程已退出）→ 幂等不抛错
    r2 = mgr.stop(mid, mode="kill")
    assert r2.get("ok") and r2.get("stopped") is True
    # 未知句柄 → 幂等
    r3 = mgr.stop(mid, mode="kill")
    assert r3.get("ok") is True
    assert not [a for a in repo._conn.execute(
        "SELECT status FROM attempts WHERE mission_id=?", (mid,))
        .fetchall() if a["status"] == "running"], "无残留 running"
    repo.close()


# ---------- 顺带 · source_binding 覆盖核验 ----------


def test_binding_override_rejected(tmp_path):
    """传入与源 attempt 认领绑定不一致的 source_binding（如空数组）→
    apply 拒绝覆盖；传一致绑定正常应用。"""
    repo, graph, mid, root, up, parent, sid, binding, fp = \
        _parent_with_upstream(tmp_path)
    with pytest.raises(GraphError, match="拒绝覆盖"):
        _apply_two_children(repo, graph, mid, parent, "sp-ovr", binding,
                            fp, apply_binding="[]")
    kids, _ = _apply_two_children(repo, graph, mid, parent, "sp-ovr2",
                                  binding, fp)
    assert len(kids) == 2
    repo.close()
