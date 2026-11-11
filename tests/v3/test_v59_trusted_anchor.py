# -*- coding: utf-8 -*-
"""R1 可信锚点回归（v59，第二轮独立验收关闭条件）。

mini DB 构造（Repository 真实 schema）：精确绑定 accepted attempt、
snapshot 身份/版本/有效性核对、导出 JSON+digest 锚、多来源冲突拒绝、
generated_by 不再构成认证、无锚拒绝。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from dag_agent.artifacts import manifest_hash_of  # noqa: E402
from dag_agent.repository import Repository  # noqa: E402
from techstack_full_v2.checks import (Invalid, export_trusted,  # noqa: E402
                                      load_trusted, trusted_from_db)


def mini_db(tmp_path: Path):
    """构造最小任务库：mission m-t1 + root n1 + 导入节点 n2（accepted，
    绑定 snapshot 1 携带 2 个文件）。"""
    home = tmp_path / "run"
    home.mkdir()
    repo = Repository(str(home / "mission.db"))
    con = repo._conn
    con.execute(
        "INSERT INTO missions(id,name,brief,status,goal_version,"
        "closure_basis,anchor,budget_json,created_at,updated_at)"
        " VALUES('m-t1','t','b','active',1,'','','',"
        "datetime('now'),datetime('now'))")
    con.execute(
        "INSERT INTO nodes(id,mission_id,title,statement,parent_id,kind,"
        "status,check_cmd,check_version,acceptance_json,priority,"
        "strength_actual,accepted_artifact_version,declared_artifacts_json,"
        "revision,group_id,group_order,switch_after,exam_files_json,"
        "created_at,updated_at)"
        " VALUES('n1','m-t1','root','s',NULL,'task','pending',NULL,1,"
        "'NULL',0,0,NULL,'[]',1,NULL,0,1,'[]',datetime('now'),"
        "datetime('now'))")
    con.execute(
        "INSERT INTO nodes(id,mission_id,title,statement,parent_id,kind,"
        "status,check_cmd,check_version,acceptance_json,priority,"
        "strength_actual,accepted_artifact_version,declared_artifacts_json,"
        "revision,group_id,group_order,switch_after,exam_files_json,"
        "created_at,updated_at)"
        " VALUES('n2','m-t1','import','s','n1','task','accepted',NULL,1,"
        "'NULL',0,0,1,'[]',1,NULL,0,1,'[]',datetime('now'),"
        "datetime('now'))")
    files_list = [
        {"rel_path": "alpha.md", "sha256": "a" * 64, "size": 1},
        {"rel_path": "beta.json", "sha256": "b" * 64, "size": 2},
    ]
    files = json.dumps(files_list)
    mh = manifest_hash_of(files_list)
    con.execute(
        "INSERT INTO snapshots(id,mission_id,node_id,attempt_id,"
        "artifact_version,node_revision,goal_version,manifest_hash,"
        "files_json,valid,created_at)"
        " VALUES(1,'m-t1','n2','a-n2',1,1,1,?,?,1,datetime('now'))",
        (mh, files))
    binding_n2 = json.dumps([
        {"source": "n2", "snapshot_id": 1, "artifact_version": 1,
         "files": ["alpha.md", "beta.json"], "manifest_hash": mh}])
    con.execute(
        "INSERT INTO attempts(id,node_id,mission_id,backend,status,workdir,"
        "pid,heartbeat_at,resources_json,result_summary,input_binding_json,"
        "claim_fingerprint_json,category,started_at,ended_at)"
        " VALUES('a-n2','n2','m-t1','stub','accepted','w',NULL,NULL,'{}',"
        "'',?,'[]','',datetime('now'),datetime('now'))", (binding_n2,))
    # root(n1) 的 accepted attempt：绑定=其直接输入（n2 快照）——
    # 可信锚查询按 root attempt 的绑定构建
    binding_root = json.dumps([
        {"source": "n2", "snapshot_id": 1, "artifact_version": 1,
         "files": ["alpha.md", "beta.json"], "manifest_hash": mh}])
    con.execute(
        "INSERT INTO attempts(id,node_id,mission_id,backend,status,workdir,"
        "pid,heartbeat_at,resources_json,result_summary,input_binding_json,"
        "claim_fingerprint_json,category,started_at,ended_at)"
        " VALUES('a-n1','n1','m-t1','stub','accepted','w',NULL,NULL,'{}',"
        "'',?,'[]','',datetime('now'),datetime('now'))", (binding_root,))
    con.commit()
    repo.close()
    return home


def test_db_anchor_basic_and_precision(tmp_path):
    home = mini_db(tmp_path)
    trusted, mid, att = trusted_from_db(home / "mission.db")
    assert mid == "m-t1" and att == "a-n1"  # 锚=root accepted attempt
    assert trusted == {"alpha.md": "a" * 64, "beta.json": "b" * 64}
    # 精确 mission 绑定：错 mission 拒绝
    with pytest.raises(Invalid):
        trusted_from_db(home / "mission.db", expected_mission="m-other")


def test_db_anchor_rejects_snapshot_drift(tmp_path):
    home = mini_db(tmp_path)
    repo = Repository(str(home / "mission.db"))
    repo._conn.execute(
        "UPDATE snapshots SET artifact_version=2 WHERE id=1")
    repo._conn.commit()
    repo.close()
    with pytest.raises(Invalid):
        trusted_from_db(home / "mission.db")   # 版本漂移拒绝


def test_db_anchor_rejects_invalid_snapshot(tmp_path):
    home = mini_db(tmp_path)
    repo = Repository(str(home / "mission.db"))
    repo._conn.execute("UPDATE snapshots SET valid=0 WHERE id=1")
    repo._conn.commit()
    repo.close()
    with pytest.raises(Invalid):
        trusted_from_db(home / "mission.db")   # 失效快照拒绝


def test_db_anchor_rejects_identity_swap(tmp_path):
    home = mini_db(tmp_path)
    repo = Repository(str(home / "mission.db"))
    repo._conn.execute("UPDATE snapshots SET node_id='n9' WHERE id=1")
    repo._conn.commit()
    repo.close()
    with pytest.raises(Invalid):
        trusted_from_db(home / "mission.db")   # 来源身份被改拒绝


def test_db_anchor_ambiguous_attempts(tmp_path):
    """真正歧义（accepted 与 running 并存且未指定 expected_attempt，或 2 个
    running 并存）→ 歧义拒绝；而 1 failed + 1 running 属于正常重试，允许通过。"""
    home = mini_db(tmp_path)
    repo = Repository(str(home / "mission.db"))
    mh = repo._conn.execute("SELECT manifest_hash FROM snapshots WHERE id=1").fetchone()[0]
    binding = json.dumps([{"source": "n2", "snapshot_id": 1,
                           "artifact_version": 1,
                           "files": ["alpha.md", "beta.json"],
                           "manifest_hash": mh}])
    # 1) a-n1 仍为 accepted + 新增 a-late 为 running，未指定 expected_attempt → 歧义拒绝
    repo._conn.execute(
        "INSERT INTO attempts(id,node_id,mission_id,backend,status,workdir,"
        "pid,heartbeat_at,resources_json,result_summary,input_binding_json,"
        "claim_fingerprint_json,category,started_at,ended_at)"
        " VALUES('a-late','n1','m-t1','stub','running','w2',NULL,NULL,'{}',"
        "'',?,'[]','',datetime('now'),NULL)", (binding,))
    repo._conn.commit()
    repo.close()
    with pytest.raises(Invalid):
        trusted_from_db(home / "mission.db")
    # 显式指定 expected_attempt='a-late'（如重开后新版本判卷）→ 精确通过
    trusted, mid, att = trusted_from_db(home / "mission.db", expected_attempt="a-late")
    assert att == "a-late" and trusted["alpha.md"] == "a" * 64
    # 2) 原 attempt 失败（failed）+ 新重试 attempt running → 唯一活跃 attempt 允许通过
    repo2 = Repository(str(home / "mission.db"))
    repo2._conn.execute("UPDATE attempts SET status='failed' WHERE id='a-n1'")
    repo2._conn.commit()
    repo2.close()
    trusted2, mid2, att2 = trusted_from_db(home / "mission.db")
    assert att2 == "a-late"
    # 3) 两个 running 并存 → 歧义拒绝
    repo3 = Repository(str(home / "mission.db"))
    repo3._conn.execute("UPDATE attempts SET status='running' WHERE id='a-n1'")
    repo3._conn.commit()
    repo3.close()
    with pytest.raises(Invalid):
        trusted_from_db(home / "mission.db")


def test_export_and_digest_anchor(tmp_path):
    home = mini_db(tmp_path)
    info = export_trusted(home / "mission.db", home / "t.json",
                          home / "t.digest.json")
    raw = (home / "t.json").read_bytes()
    rec = json.loads((home / "t.digest.json").read_text(encoding="utf-8"))
    assert rec["trusted_json_sha256"] == hashlib.sha256(raw).hexdigest()
    trusted = load_trusted(trusted_json_path=home / "t.json",
                           trusted_digest_path=home / "t.digest.json")
    assert trusted["alpha.md"] == "a" * 64
    # digest 不匹配（JSON 被改写）→ 拒绝
    payload = json.loads(raw.decode("utf-8"))
    payload["deliverables"]["alpha.md"] = "f" * 64
    (home / "forged.json").write_bytes(json.dumps(
        payload, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8"))
    with pytest.raises(Invalid):
        load_trusted(trusted_json_path=home / "forged.json",
                     trusted_digest_path=home / "t.digest.json")


def test_generated_by_string_is_not_auth(tmp_path):
    """generated_by='platform-export' 的自报 JSON（无 digest 锚）→ 拒绝。"""
    home = mini_db(tmp_path)
    forged = home / "self-claimed.json"
    forged.write_text(json.dumps({
        "schema_version": 1, "generated_by": "platform-export",
        "mission_id": "whatever", "deliverables": {"x.md": "c" * 64}}),
        encoding="utf-8")
    with pytest.raises(Invalid):
        load_trusted(trusted_json_path=forged, trusted_digest_path=None)


def test_conflict_db_vs_json_rejected(tmp_path):
    """真 DB + 伪造导出 JSON（带配套伪造 digest）→ 身份/内容冲突拒绝，
    JSON 不覆盖 DB。"""
    home = mini_db(tmp_path)
    info = export_trusted(home / "mission.db", home / "good.json",
                          home / "good.digest.json")
    payload = json.loads((home / "good.json").read_bytes().decode("utf-8"))
    payload["deliverables"]["alpha.md"] = "f" * 64
    raw = json.dumps(payload, ensure_ascii=False, indent=1,
                     sort_keys=True).encode("utf-8")
    (home / "forged.json").write_bytes(raw)
    # 伪造者自配 digest（纯离线伪造在技术上可构造——防线=与 DB 同在时
    # 冲突核对 + digest 位置约定；此处验证冲突核对路径）
    (home / "forged.digest.json").write_text(json.dumps({
        "kind": "dag-agent-trusted-export",
        "trusted_json_sha256": hashlib.sha256(raw).hexdigest(),
        "trusted_json_bytes": len(raw),
        "mission_id": payload["mission_id"],
        "root_attempt": payload["root_attempt"]}), encoding="utf-8")
    with pytest.raises(Invalid):
        load_trusted(trusted_json_path=home / "forged.json",
                     trusted_digest_path=home / "forged.digest.json",
                     trusted_db_path=home / "mission.db")


def test_mission_mismatch_between_json_and_db(tmp_path):
    home = mini_db(tmp_path)
    export_trusted(home / "mission.db", home / "good.json",
                   home / "good.digest.json")
    payload = json.loads((home / "good.json").read_bytes().decode("utf-8"))
    payload["mission_id"] = "m-elsewhere"
    raw = json.dumps(payload, ensure_ascii=False, indent=1,
                     sort_keys=True).encode("utf-8")
    (home / "wrong-mid.json").write_bytes(raw)
    (home / "wrong-mid.digest.json").write_text(json.dumps({
        "kind": "dag-agent-trusted-export",
        "trusted_json_sha256": hashlib.sha256(raw).hexdigest(),
        "trusted_json_bytes": len(raw),
        "mission_id": "m-elsewhere",
        "root_attempt": payload["root_attempt"]}), encoding="utf-8")
    # JSON 与 digest 自洽但 mission 与 DB 不符 → 冲突拒绝
    with pytest.raises(Invalid):
        load_trusted(trusted_json_path=home / "wrong-mid.json",
                     trusted_digest_path=home / "wrong-mid.digest.json",
                     trusted_db_path=home / "mission.db")
    # 纯离线：显式 expected_mission 核对拒绝
    with pytest.raises(Invalid):
        load_trusted(trusted_json_path=home / "wrong-mid.json",
                     trusted_digest_path=home / "wrong-mid.digest.json",
                     expected_mission="m-t1")
