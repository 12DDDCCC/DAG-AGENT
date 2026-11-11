"""步骤2验收：工件与验收——独立执行目录/哈希版本绑定/真实检查/篡改检测。

对应任务清单第二步通过标准：验收后改文件不能继续冒充原通过结果。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dag_agent.artifacts import sha256_file
from dag_agent.assessment import run_check
from dag_agent.graph import GraphError

CHK_PASS = "python -c 'pass'"


def _complete_with_artifact(graph, mid, nid, content: bytes = b"result-v1"):
    aid = graph.start_attempt(mid, nid)
    workdir = Path(graph.repo.get_attempt(aid).workdir)
    (workdir / "out.txt").write_bytes(content)
    result = graph.submit_attempt(mid, nid, aid, artifact_paths=["out.txt"])
    return aid, workdir, result


def test_acceptance_publishes_immutable_copy(graph):
    mid, root = graph.create_mission("工件发布")
    a = graph.add_node(mid, "A", parent_id=root, check_cmd=CHK_PASS)
    _aid, _wd, result = _complete_with_artifact(graph, mid, a)
    assert result["verdict"] == "pass"
    arts = graph.repo.artifacts_of(a)
    assert len(arts) == 1 and arts[0].version == 1
    accepted = graph.store.accepted_root / a / "v1" / "out.txt"
    assert accepted.is_file()
    assert sha256_file(accepted) == arts[0].content_hash
    assert graph.store.verify_accepted(graph.repo, a)["ok"] is True


def test_workdir_tamper_cannot_impersonate_accepted(graph):
    mid, root = graph.create_mission("工作目录篡改")
    a = graph.add_node(mid, "A", parent_id=root, check_cmd=CHK_PASS)
    _aid, workdir, _result = _complete_with_artifact(graph, mid, a)
    # 验收后改工作文件——不能冒充已验收结果
    (workdir / "out.txt").write_bytes("被篡改的内容".encode("utf-8"))
    check = graph.store.check_workdir_against_accepted(graph.repo, a, workdir)
    assert check["ok"] is False
    assert "不符" in check["mismatch"][0]["reason"]
    # 已验收存储不受影响，仍是原内容
    v = graph.store.verify_accepted(graph.repo, a)
    assert v["ok"] is True
    arts = graph.repo.artifacts_of(a)
    assert arts[0].content_hash == sha256_file(
        graph.store.accepted_root / a / "v1" / "out.txt")


def test_accepted_store_tamper_detected(graph):
    mid, root = graph.create_mission("验收库篡改")
    a = graph.add_node(mid, "A", parent_id=root, check_cmd=CHK_PASS)
    _aid, _wd, _result = _complete_with_artifact(graph, mid, a)
    # 直接改 accepted 存储 → 复检必须报警
    p = graph.store.accepted_root / a / "v1" / "out.txt"
    p.write_bytes("黑市改造版".encode("utf-8"))
    v = graph.store.verify_accepted(graph.repo, a)
    assert v["ok"] is False and "不符" in v["tampered"][0]["reason"]


def test_accepted_missing_file_detected(graph):
    mid, root = graph.create_mission("验收库缺失")
    a = graph.add_node(mid, "A", parent_id=root, check_cmd=CHK_PASS)
    _aid, _wd, _result = _complete_with_artifact(graph, mid, a)
    p = graph.store.accepted_root / a / "v1" / "out.txt"
    p.unlink()
    v = graph.store.verify_accepted(graph.repo, a)
    assert v["ok"] is False and "缺失" in v["tampered"][0]["reason"]


def test_failed_attempt_does_not_consume_version(real_graph):
    """失败尝试不发布工件不占版本号；重启后成功的那次拿 version 1。"""
    mid, root = real_graph.create_mission("失败不占版本")
    f = real_graph.add_node(
        mid, "F", parent_id=root,
        check_cmd="python -c \"import sys; sys.exit(0) if open('out.txt','rb').read()==b'fixed' else sys.exit(1)\"")
    aid1 = real_graph.start_attempt(mid, f)
    wd1 = Path(real_graph.repo.get_attempt(aid1).workdir)
    (wd1 / "out.txt").write_bytes(b"broken")
    r1 = real_graph.submit_attempt(mid, f, aid1, artifact_paths=["out.txt"])
    assert r1["verdict"] == "fail"
    assert real_graph.repo.artifacts_of(f) == []
    aid2 = real_graph.start_attempt(mid, f)
    wd2 = Path(real_graph.repo.get_attempt(aid2).workdir)
    (wd2 / "out.txt").write_bytes(b"fixed")
    r2 = real_graph.submit_attempt(mid, f, aid2, artifact_paths=["out.txt"])
    assert r2["verdict"] == "pass"
    arts = real_graph.repo.artifacts_of(f)
    assert [x.version for x in arts] == [1]
    assert arts[0].content_hash == sha256_file(wd2 / "out.txt")


def test_zero_write_guard_fails_writing_check(real_graph):
    """检查命令在检查期间写被测目录 → 直接判失败。"""
    mid, root = real_graph.create_mission("零写入守卫")
    a = real_graph.add_node(
        mid, "A", parent_id=root,
        check_cmd="python -c \"open('cheat.txt','w').write('x')\"")
    aid = real_graph.start_attempt(mid, a)
    result = real_graph.submit_attempt(mid, a, aid)
    assert result["verdict"] == "fail"
    assert "零写入守卫" in result["evidence"]
    assert real_graph.repo.get_node(a).status == "failed"
    assert real_graph.repo.artifacts_of(a) == []  # 失败不发布工件


def test_whitelist_rejects_non_python_entry():
    ok, ev = run_check("echo hello", Path("nowhere_guard"))
    del ok
    assert "白名单" in ev


def test_fail_closed_and_no_artifact_on_fail(graph):
    mid, root = graph.create_mission("失败不发布")
    a = graph.add_node(mid, "A", statement="fail-closed探针", parent_id=root)  # 无 check_cmd
    aid = graph.start_attempt(mid, a)
    wd = Path(graph.repo.get_attempt(aid).workdir)
    (wd / "out.txt").write_bytes(b"x")
    result = graph.submit_attempt(mid, a, aid, artifact_paths=["out.txt"])
    assert result["verdict"] == "fail"
    assert graph.repo.artifacts_of(a) == []
    assert not (graph.store.accepted_root / a).exists()


def test_escape_path_rejected(graph):
    mid, root = graph.create_mission("越界")
    a = graph.add_node(mid, "A", parent_id=root, check_cmd=CHK_PASS)
    aid = graph.start_attempt(mid, a)
    with pytest.raises(GraphError, match="越界|不存在"):
        graph.submit_attempt(mid, a, aid, artifact_paths=["../../x.txt"])


def test_integration_acceptance_binds_artifact(graph):
    """整合尝试的验收同样绑工件版本。"""
    mid, root = graph.create_mission("整合绑版本", root_check=CHK_PASS)
    kids = graph.decompose(mid, root, [{"title": "A", "statement": "子块甲", "check_cmd": CHK_PASS}])
    aid = graph.start_attempt(mid, kids[0])
    wd = Path(graph.repo.get_attempt(aid).workdir)
    (wd / "part.txt").write_bytes("部件".encode("utf-8"))
    graph.submit_attempt(mid, kids[0], aid, artifact_paths=["part.txt"])
    iaid = graph.start_attempt(mid, root)
    iwd = Path(graph.repo.get_attempt(iaid).workdir)
    (iwd / "final.txt").write_bytes("整合成果".encode("utf-8"))
    result = graph.submit_attempt(mid, root, iaid, artifact_paths=["final.txt"])
    assert result["verdict"] == "pass"
    assert graph.repo.get_node(root).accepted_artifact_version == 1
    assert graph.repo.get_mission(mid).status == "done"
    assert (graph.store.accepted_root / root / "v1" / "final.txt").is_file()
