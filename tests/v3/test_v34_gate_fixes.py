"""v3.4 查看轮修复回归（Codex 旧14项核对后，2026-10-04）。

来源：outputs/DAG-agent-旧14项问题-现状核对.md A1/A2/A3 残留缺口 +
work/2026-10-04-dual-review/checker/results.json（坏申报输出 accepted、
空来源 rc0、cmd/包导入漏锁负例）。
通过口径：结构化 cmd/test 入口与包内导入进考卷摘要；整合考卷断言本次
申报产物（逐件+申报目录冒烟+空来源拒）；简报五要素成为真实派发硬闸门。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from dag_agent.acceptance import compute_check_digest
from dag_agent.graph import Graph, GraphError
from dag_agent.repository import Repository

# 复用 v33 的最小 bundle 与 integration 调用器（同目录直导）
if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))
from test_v33_zero_cost_fixes import (  # noqa: E402
    BUNDLE, _delivery_dir, _run_integration)

CHK = "python -c 'pass'"

GOOD_BRIEF = (
    "【输入】读取 D:/tmp/in.csv（绝对路径示例）\n"
    "【交付清单】out.py\n"
    "【接口签名】def run(s: str) -> int\n"
    "【样例】run('a') == 1\n"
    "【自测入口】python out.py --selftest"
)


# ---- A1 结构化 cmd/test 入口与包内导入闭包 ----

def test_structured_cmd_entry_locked(tmp_path):
    """action=cmd 的 python 入口改永绿 → 摘要必须变（cmd 漏锁负例翻转）。"""
    entry = tmp_path / "entry.py"
    entry.write_text("import sys\n"
                     "sys.exit(0 if open('p.txt').read() == 'good' else 1)\n",
                     encoding="utf-8")
    (tmp_path / "p.txt").write_text("good", encoding="utf-8")
    acc = [{"action": "cmd", "cmd": f'python "{entry}"'}]
    before = compute_check_digest(acceptance=acc)
    entry.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")  # 永绿
    assert compute_check_digest(acceptance=acc) != before


def test_structured_test_target_locked(tmp_path):
    """action=test 的目标测试文件被改 → 摘要必须变。"""
    tgt = tmp_path / "test_x.py"
    tgt.write_text("def test_x():\n    assert 1\n", encoding="utf-8")
    acc = [{"action": "test", "args": f'"{tgt}"'}]
    before = compute_check_digest(acceptance=acc)
    tgt.write_text("def test_x():\n    assert 0  # 反转\n", encoding="utf-8")
    assert compute_check_digest(acceptance=acc) != before


def test_pytest_selector_target_locked(tmp_path):
    """action=test 的 pytest 选择器（test.py::name）目标被改 → 摘要必变。"""
    tgt = tmp_path / "test_exam.py"
    tgt.write_text("def test_exam():\n    assert 0\n", encoding="utf-8")
    acc = [{"action": "test",
            "args": f'"{tgt}"::test_exam'}]
    before = compute_check_digest(acceptance=acc)
    tgt.write_text("def test_exam():\n    assert 1  # 永绿\n", encoding="utf-8")
    assert compute_check_digest(acceptance=acc) != before


def test_relative_import_closure(tmp_path):
    """包模块相对导入 from . import helper：helper 入闭包，改动必变。"""
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core.py").write_text("from . import helper\nimport sys\n"
                                 "sys.exit(0 if helper.ok() else 1)\n",
                                 encoding="utf-8")
    (pkg / "helper.py").write_text("def ok():\n    return False\n",
                                   encoding="utf-8")
    entry = tmp_path / "entry.py"
    entry.write_text("from mypkg import core\n", encoding="utf-8")
    cmd = f'python "{entry}"'
    before = compute_check_digest(check_cmd=cmd)
    (pkg / "helper.py").write_text("def ok():\n    return True\n",
                                   encoding="utf-8")
    assert compute_check_digest(check_cmd=cmd) != before


def test_package_import_closure(tmp_path):
    """from pkg import util：pkg/__init__.py 与包内模块入闭包，改动必变摘要。"""
    entry = tmp_path / "entry.py"
    entry.write_text("from mypkg import util\nimport sys\n"
                     "sys.exit(0 if util.ok() else 1)\n", encoding="utf-8")
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "util.py").write_text("def ok():\n    return True\n",
                                 encoding="utf-8")
    cmd = f'python "{entry}"'
    before = compute_check_digest(check_cmd=cmd)
    (pkg / "util.py").write_text("def ok():\n    return False\n",
                                 encoding="utf-8")
    assert compute_check_digest(check_cmd=cmd) != before
    # 深层：import mypkg.deep.mod 的逐级文件也入闭包
    entry2 = tmp_path / "entry2.py"
    entry2.write_text("import mypkg.deep.mod, sys\n"
                      "sys.exit(0 if mypkg.deep.mod.ok() else 1)\n",
                      encoding="utf-8")
    deep = pkg / "deep"
    deep.mkdir()
    (deep / "__init__.py").write_text("", encoding="utf-8")
    (deep / "mod.py").write_text("def ok():\n    return True\n",
                                 encoding="utf-8")
    cmd2 = f'python "{entry2}"'
    b2 = compute_check_digest(check_cmd=cmd2)
    (deep / "mod.py").write_text("def ok():\n    return False\n",
                                 encoding="utf-8")
    assert compute_check_digest(check_cmd=cmd2) != b2


def test_structured_cmd_drift_rejected_at_submit(tmp_path):
    """结构化 cmd 入口漂移后正式提交必须被拒（平台级 P0/P4 摘要核验）。"""
    from dag_agent.assessment import default_checker3
    entry = tmp_path / "pkg" / "entry.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")  # 严格
    acc = [{"action": "cmd", "cmd": f'python "{entry}"'}]
    repo = Repository(tmp_path / "m" / "mission.db")
    g = Graph(repo, checker3=default_checker3)
    try:
        mid, root = g.create_mission("cmd锁包", root_check=CHK)
        node = g.add_node(mid, "A", "交付 x.txt", parent_id=root,
                          acceptance=acc, declared_artifacts=("x.txt",))
        aid = g.start_attempt(mid, node)
        wd = Path(repo.get_attempt(aid).workdir)
        (wd / "x.txt").write_text("BAD", encoding="utf-8")
        # 认领后把 cmd 入口改成永绿 → 摘要失配，提交必须拒
        entry.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        with pytest.raises(GraphError, match="考卷内容已变更"):
            g.submit_attempt(mid, node, aid, artifact_paths=["x.txt"])
    finally:
        repo.close()


# ---- A2 整合考卷绑定本次申报（候选根逐件断言+申报目录冒烟）----

def test_integration_rejects_broken_declared_output(tmp_path):
    """申报输出损坏（main.py 运行即崩）→ 拒（Codex 负例：曾 accepted）。"""
    d = _delivery_dir(tmp_path)          # inputs/ 侧齐备
    (d / "main.py").write_text(          # 本次申报的坏入口
        "import sys\nsys.exit(3)\n", encoding="utf-8")
    r = _run_integration(d, "main.py")
    assert r.returncode == 1 and "冒烟失败" in r.stderr


def test_integration_rejects_declared_syntax_error(tmp_path):
    """申报 render.py 语法错 → 编译当场拒（不再漏到 ROOT）。"""
    d = _delivery_dir(tmp_path)
    (d / "main.py").write_text(BUNDLE["main.py"], encoding="utf-8")
    (d / "render.py").write_text("def (:\n", encoding="utf-8")
    r = _run_integration(d, "main.py")
    assert r.returncode == 1 and "申报 .py 编译失败" in r.stderr


def test_integration_rejects_empty_declared_readme(tmp_path):
    """申报 README.md 为空 → 拒。"""
    d = _delivery_dir(tmp_path)
    for name, src in BUNDLE.items():
        (d / name).write_text(src, encoding="utf-8")
    (d / "README.md").write_text("", encoding="utf-8")
    r = _run_integration(d, "main.py")
    assert r.returncode == 1 and "申报工件为空：README.md" in r.stderr


def test_integration_rejects_empty_source_dir(tmp_path):
    """inputs/ 空来源目录（0 文件）→ 显式拒（此前被静默跳过）。"""
    import shutil
    d = _delivery_dir(tmp_path, bundle=False)
    shutil.rmtree(d / "inputs" / "n06")
    (d / "inputs" / "n06").mkdir()       # 重建为空来源目录
    r = _run_integration(d)
    assert r.returncode == 1 and "空来源目录" in r.stderr


def test_integration_declared_bundle_smoke_positive(tmp_path):
    """申报侧正例：完整 bundle 申报在候选根 → 冒烟过、汇总达冻结期望。"""
    d = _delivery_dir(tmp_path)
    for name, src in BUNDLE.items():
        (d / name).write_text(src, encoding="utf-8")
    (d / "README.md").write_text("usage: --orders --refunds --out; "
                                 "error codes: order_id_duplicate "
                                 "refund_exceeds_order columns_missing",
                                 encoding="utf-8")
    r = _run_integration(d, "main.py")
    assert r.returncode == 0 and "integration-ok" in r.stdout, r.stderr


# ---- A2' 验收集合=发布集合（Codex 63bf8d2 复核四例）----

def test_integration_declared_subset_smoke_fails(tmp_path):
    """例2：工作目录完整可运行、_declared 只申报 main+README →
    冒烟跑在仅申报集合副本上（缺 loader）必须拒。"""
    d = _delivery_dir(tmp_path)
    for name, src in BUNDLE.items():
        (d / name).write_text(src, encoding="utf-8")
    (d / "README.md").write_text("usage 见 main.py", encoding="utf-8")
    (d / "_declared.json").write_text(
        json.dumps(["README.md", "main.py"]), encoding="utf-8")
    r = _run_integration(d, "main.py")
    assert r.returncode == 1 and "冒烟失败" in r.stderr


def test_integration_bare_mode_rejects_broken_output(tmp_path):
    """例4：无参数整合 + _declared 申报语法坏 loader.py → 编译当场拒。"""
    d = _delivery_dir(tmp_path)
    (d / "loader.py").write_text("def (:\n", encoding="utf-8")
    (d / "_declared.json").write_text(json.dumps(["loader.py"]),
                                      encoding="utf-8")
    r = _run_integration(d)
    assert r.returncode == 1 and "申报 .py 编译失败" in r.stderr


def test_integration_empty_declared_only_readme_rejected(tmp_path):
    """例1 变体：申报集合仅一个空 README → 空文件拒。"""
    d = _delivery_dir(tmp_path)
    (d / "README.md").write_text("", encoding="utf-8")
    (d / "_declared.json").write_text(json.dumps(["README.md"]),
                                      encoding="utf-8")
    r = _run_integration(d)
    assert r.returncode == 1 and "申报工件为空：README.md" in r.stderr


def test_submit_rejects_missing_required_artifacts(tmp_path):
    """例3（平台级）：节点定义必需工件，提交清单缺 README → GraphError，
    不得以提交清单替换节点定义清单。"""
    from dag_agent.assessment import default_checker3
    repo = Repository(tmp_path / "m" / "mission.db")
    g = Graph(repo, checker3=default_checker3)
    try:
        mid, root = g.create_mission("必需工件", root_check=CHK)
        node = g.add_node(mid, "A", "交付两件", parent_id=root,
                          check_cmd=CHK,
                          declared_artifacts=("x.txt", "README.md"))
        aid = g.start_attempt(mid, node)
        wd = Path(repo.get_attempt(aid).workdir)
        (wd / "x.txt").write_text("GOOD", encoding="utf-8")
        (wd / "README.md").write_text("r", encoding="utf-8")
        with pytest.raises(GraphError, match="申报清单缺节点必需工件"):
            g.submit_attempt(mid, node, aid, artifact_paths=["x.txt"])
        # 同一尝试补齐必需项后重新提交（早抛不改变在途状态）
        res = g.submit_attempt(mid, node, aid,
                               artifact_paths=["x.txt", "README.md"])
        assert res["verdict"] == "pass"
    finally:
        repo.close()


# ---- B4 金额尾随换行负例入 judge（判卷器 20→22 例）----

def test_judge_amount_trailing_newline_variants():
    """判卷器含金额尾随换行两变体负例：self-check 15 负例全被 oracle 拒。"""
    import subprocess
    from test_v33_zero_cost_fixes import CHECKS as _CHECKS
    judge = _CHECKS.parent.parent / "judge.py"
    r = subprocess.run([sys.executable, "-B", str(judge), "--self-check"],
                       capture_output=True, text=True, cwd=str(judge.parent),
                       timeout=180)
    assert r.returncode == 0, r.stderr[-300:]
    rep = json.loads(r.stdout)
    # 13 基础负例 + 2 尾随换行变体（SPEC 31 行：金额只能是 ASCII 非负十进制整数）
    assert rep["negative_checks"] == 15


# ---- A3 简报五要素派发硬闸门（start_attempt 前置）----

def _gate_env(tmp_path):
    from dag_agent.assessment import default_checker3
    repo = Repository(tmp_path / "m" / "mission.db")
    g = Graph(repo, checker3=default_checker3)
    return repo, g


def test_dispatch_gate_rejects_real_backend_without_brief(tmp_path):
    """真实后端（claude-cli）缺五要素简报 → 认领被硬拒；补齐后放行。"""
    repo, g = _gate_env(tmp_path)
    try:
        mid, root = g.create_mission("闸门", root_check=CHK)
        node = g.add_node(mid, "A", "交付 x.txt（无五段格式）",
                          parent_id=root, check_cmd=CHK,
                          declared_artifacts=("x.txt",))
        with pytest.raises(GraphError, match="简报五要素缺段"):
            g.start_attempt(mid, node, backend="claude-cli")
        # 补齐五要素简报后再派发 → 放行
        with repo.tx() as cur:
            cur.execute("UPDATE nodes SET statement=? WHERE id=?",
                        (GOOD_BRIEF, node))
        aid = g.start_attempt(mid, node, backend="claude-cli")
        assert repo.get_attempt(aid).status == "running"
    finally:
        repo.close()


def test_dispatch_gate_stub_backend_warns_only(tmp_path):
    """测试桩后端缺段 → 不拒，但落 brief_gate_warning 告警事件。"""
    repo, g = _gate_env(tmp_path)
    try:
        mid, root = g.create_mission("闸门桩", root_check=CHK)
        node = g.add_node(mid, "A", "交付 x.txt（无五段格式）",
                          parent_id=root, check_cmd=CHK,
                          declared_artifacts=("x.txt",))
        aid = g.start_attempt(mid, node, backend="fake")
        assert repo.get_attempt(aid).status == "running"
        with repo.tx() as cur:
            evs = cur.execute(
                "SELECT type, payload_json FROM events WHERE type=?",
                ("brief_gate_warning",)).fetchall()
        assert evs and "输入" in evs[0]["payload_json"]
    finally:
        repo.close()


def test_scheduler_reports_brief_gate_reason(tmp_path):
    """A3' 调度可见性：缺段简报节点经真实后端派发 → brief_gate_rejected
    事件落库说明原因（不再静默 pending、外界只看到"无进展"）。"""
    from dag_agent.assessment import default_checker3
    from dag_agent.scheduler import Budget, Scheduler

    class RealishBackend:
        name = "claude-cli"          # 真实后端名 → 触发硬闸门

        def run(self, statement, workdir, check_cmd, title=""):
            raise AssertionError("缺段简报不应被派发到执行器")

    repo = Repository(tmp_path / "m2" / "mission.db")
    g = Graph(repo, checker3=default_checker3)
    s = Scheduler(repo, g, RealishBackend(), max_concurrency=2,
                  budget=Budget(max_calls=8))
    try:
        mid, root = g.create_mission("调度可见", root_check=CHK)
        g.add_node(mid, "A", "无五段格式的陈述", parent_id=root,
                   check_cmd=CHK, declared_artifacts=("x.txt",))
        s.tick_dispatch(mid)
        with repo.tx() as cur:
            evs = cur.execute(
                "SELECT payload_json FROM events WHERE type=?",
                ("brief_gate_rejected",)).fetchall()
        assert evs and "简报五要素" in evs[0]["payload_json"]
        # 二次 tick 同因不重复刷屏
        s.tick_dispatch(mid)
        with repo.tx() as cur:
            n2 = cur.execute(
                "SELECT COUNT(*) AS c FROM events WHERE type=?",
                ("brief_gate_rejected",)).fetchone()["c"]
        assert n2 == 1
    finally:
        repo.close()


def test_pytest_directory_target_locked(tmp_path):
    """action=test 的目录形态（python -m pytest <目录>）：目录内测试与
    conftest 全部入摘要，改动必变（Codex adcc5d5 复核的目录漏锁形态）。"""
    d = tmp_path / "suite"
    d.mkdir()
    (d / "test_a.py").write_text("def t():\n    assert 0\n", encoding="utf-8")
    (d / "conftest.py").write_text("", encoding="utf-8")
    acc = [{"action": "test", "args": str(d)}]   # 裸路径（无引号形态）
    before = compute_check_digest(acceptance=acc)
    (d / "test_a.py").write_text("def t():\n    assert 1\n", encoding="utf-8")
    assert compute_check_digest(acceptance=acc) != before
    mid_digest = compute_check_digest(acceptance=acc)
    (d / "conftest.py").write_text("X = 1\n", encoding="utf-8")
    assert compute_check_digest(acceptance=acc) != mid_digest


def test_plain_cmd_directory_all_files_locked(tmp_path):
    """普通命令（check_cmd 字符串）跑 pytest 目录：目录内全部测试入包，
    改第二件摘要必变（旧 [:1] 截取首入口的漏锁形态）。"""
    for i in range(3):
        (tmp_path / f"test_{i}.py").write_text(
            f"def t():\n    assert 0\n", encoding="utf-8")
    cmd = f'python -m pytest "{tmp_path}"'
    before = compute_check_digest(check_cmd=cmd)
    (tmp_path / "test_1.py").write_text("def t():\n    assert 1\n",
                                        encoding="utf-8")
    assert compute_check_digest(check_cmd=cmd) != before


def test_directory_over_limit_rejected(tmp_path):
    """目录内 .py 超过 60 件：考卷摘要计算显式拒绝（不静默截断）。"""
    from dag_agent.acceptance import AcceptanceError
    for i in range(61):
        (tmp_path / f"t{i:03d}.py").write_text("x = 0\n", encoding="utf-8")
    with pytest.raises(AcceptanceError, match="考卷目录过大"):
        compute_check_digest(check_cmd=f'python -m pytest "{tmp_path}"')
