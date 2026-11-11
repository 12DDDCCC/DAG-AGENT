# -*- coding: utf-8 -*-
"""v3.10 T6 回归：考卷包显式声明（manifest 式冻结）。

判据（任务书负例三项全过）：
① 显式清单内文件被改 → 摘要必变、漂移提交必拒（P0 GraphError）；
② 清单外文件被改/新增 → 摘要不变（显式模式按清单冻结，清单即权威）；
③ 未声明考卷仍走扫描闭包（改入口脚本→摘要变）；
④ 两种模式互不干扰（同图两节点各自漂移行为独立）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dag_agent.acceptance import (AcceptanceError, compute_check_digest)
from dag_agent.graph import Graph, GraphError
from dag_agent.repository import Repository

CHK_BODY = ('import sys,pathlib; sys.exit(0 if pathlib.Path('
            '"out.txt").is_file() else 1)')


@pytest.fixture
def env(tmp_path):
    pkg = tmp_path / "exampkg"
    pkg.mkdir()
    (pkg / "chk.py").write_text(CHK_BODY, encoding="utf-8")
    (pkg / "exam_data.txt").write_text("SPEC-v1", encoding="utf-8")
    chk = f'python "{pkg / "chk.py"}"'
    repo = Repository(tmp_path / "m" / "mission.db")
    g = Graph(repo)
    mid, root = g.create_mission("T6显式清单", root_check=chk,
                                 root_declared_artifacts=("out.txt",))
    n_expl = g.add_node(
        mid, "显式", "显式清单节点", parent_id=root, check_cmd=chk,
        declared_artifacts=("out.txt",),
        exam_files=(str(pkg / "chk.py"), str(pkg / "exam_data.txt")))
    n_scan = g.add_node(
        mid, "扫描", "扫描闭包默认节点", parent_id=root, check_cmd=chk,
        declared_artifacts=("out.txt",))
    yield repo, g, mid, root, pkg, chk, n_expl, n_scan
    repo.close()


def _submit_out(g, mid, nid, body="ok"):
    aid = g.start_attempt(mid, nid, backend="stub")
    wd = Path(g.repo.get_attempt(aid).workdir)
    (wd / "out.txt").write_text(body, encoding="utf-8")
    return g.submit_attempt(mid, nid, aid, artifact_paths=["out.txt"])


def test_explicit_manifest_drift_rejected(env):
    """① 清单内文件被改 → 摘要必变、漂移提交必拒。
    时序：认领（指纹=清单 v1）→ 清单文件被改 → 提交 → P0 拒收。"""
    repo, g, mid, root, pkg, chk, n_expl, n_scan = env
    aid = g.start_attempt(mid, n_expl, backend="stub")
    wd = Path(g.repo.get_attempt(aid).workdir)
    (wd / "out.txt").write_text("ok", encoding="utf-8")
    (pkg / "exam_data.txt").write_text("SPEC-v2-改", encoding="utf-8")
    with pytest.raises(GraphError, match="考卷内容已变更"):
        g.submit_attempt(mid, n_expl, aid, artifact_paths=["out.txt"])


def test_explicit_manifest_outside_files_insensitive(env):
    """② 清单外文件被改/新增 → 摘要不变（显式模式按清单冻结）。"""
    repo, g, mid, root, pkg, chk, n_expl, n_scan = env
    d0 = compute_check_digest(check_cmd=chk, exam_files=(
        str(pkg / "chk.py"), str(pkg / "exam_data.txt")))
    (pkg / "exam_data.txt").write_text("SPEC-v1", encoding="utf-8")  # 同值重写
    (pkg / "extra_resource.txt").write_text("清单外新增", encoding="utf-8")
    (pkg / "chk.py").chmod(0o644)
    d1 = compute_check_digest(check_cmd=chk, exam_files=(
        str(pkg / "chk.py"), str(pkg / "exam_data.txt")))
    assert d0 == d1  # 清单外新增文件不影响显式摘要
    # 清单内同内容 → 摘要不变；清单内被改 → 必变
    (pkg / "exam_data.txt").write_text("SPEC-v1-改动", encoding="utf-8")
    d2 = compute_check_digest(check_cmd=chk, exam_files=(
        str(pkg / "chk.py"), str(pkg / "exam_data.txt")))
    assert d2 != d1


def test_default_scan_closure_unchanged(env):
    """③ 未声明考卷仍走扫描闭包：改入口脚本 → 摘要变；且显式清单为空时
    compute_check_digest 与旧口径一致（exam_files 缺省可完全不传）。"""
    repo, g, mid, root, pkg, chk, n_expl, n_scan = env
    d0 = compute_check_digest(check_cmd=chk)
    (pkg / "chk.py").write_text(CHK_BODY + "  # 注释改动", encoding="utf-8")
    d1 = compute_check_digest(check_cmd=chk)
    assert d1 != d0
    # 缺省调用（不带 exam_files 参数）= 旧签名兼容
    d2 = compute_check_digest(check_cmd=chk)
    assert d2 == d1


def test_two_modes_independent(env):
    """④ 两种模式互不干扰：同目录新增资源——显式节点不敏感（按清单冻结）、
    扫描节点敏感（同目录资源逐件哈希）；两模式摘要互不相同、各自独立演化。"""
    repo, g, mid, root, pkg, chk, n_expl, n_scan = env
    d_expl_0 = compute_check_digest(check_cmd=chk, exam_files=(
        str(pkg / "chk.py"), str(pkg / "exam_data.txt")))
    d_scan_0 = compute_check_digest(check_cmd=chk)
    (pkg / "extra_resource.txt").write_text("新增资源", encoding="utf-8")
    d_expl_1 = compute_check_digest(check_cmd=chk, exam_files=(
        str(pkg / "chk.py"), str(pkg / "exam_data.txt")))
    d_scan_1 = compute_check_digest(check_cmd=chk)
    assert d_expl_1 == d_expl_0    # 显式：清单外新增不敏感
    assert d_scan_1 != d_scan_0    # 扫描：同目录非 .py 资源入闭包，敏感
    assert d_expl_0 != d_scan_0    # 两模式摘要互不相同（互不干扰）


def test_explicit_manifest_missing_file_rejected_on_declare(tmp_path):
    """声明时缺失文件=出题错误硬拒（add_node 闸门）。"""
    pkg = tmp_path / "pkg2"
    pkg.mkdir()
    (pkg / "chk.py").write_text(CHK_BODY, encoding="utf-8")
    repo = Repository(tmp_path / "m" / "mission.db")
    g = Graph(repo)
    mid, root = g.create_mission(
        "T6缺件", root_check=f'python "{pkg / "chk.py"}"',
        root_declared_artifacts=("out.txt",))
    with pytest.raises(GraphError, match="显式清单文件缺失"):
        g.add_node(mid, "坏声明", "清单缺件", parent_id=root,
                   check_cmd=f'python "{pkg / "chk.py"}"',
                   declared_artifacts=("out.txt",),
                   exam_files=(str(pkg / "不存在的资源.txt"),))
    repo.close()


def test_explicit_manifest_digest_missing_file_error(tmp_path):
    """compute_check_digest 直调：清单文件缺失 → AcceptanceError。"""
    with pytest.raises(AcceptanceError, match="显式清单文件缺失"):
        compute_check_digest(check_cmd="python x.py",
                             exam_files=(str(tmp_path / "nope.txt"),))
