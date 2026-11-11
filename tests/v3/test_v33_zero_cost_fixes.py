"""v3.3 ③' 零成本修复回归（现存问题总账 A 层，2026-10-03）。

来源：outputs/DAG-agent-现存问题总账-2026-10-03.html A1/A2/A3 +
work/2026-10-03-current-review/probe_exam_dependencies.py（A1 隔离负例：
改 subprocess 伴生 grader 永绿 → 摘要不变 → BAD 产物 rc=0 通过）。
通过口径：缺陷行为不再发生——伴生脚本进考卷闭包（摘要必变、漂移提交必拒）、
整合考卷逐件断言+冒烟、简报五要素校验齐全才放行。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dag_agent.acceptance import compute_check_digest
from dag_agent.assessment import default_checker3
from dag_agent.graph import Graph, GraphError
from dag_agent.repository import Repository

CHK = "python -c 'pass'"

ENTRY_SRC = (
    "import subprocess, sys\n"
    "from pathlib import Path\n"
    'child = Path(__file__).with_name("child_exam.py")\n'
    "sys.exit(subprocess.run([sys.executable, str(child)]).returncode)\n"
)


def _pkg(d: Path, child_body: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    entry = d / "entry.py"
    entry.write_text(ENTRY_SRC, encoding="utf-8")
    (d / "child_exam.py").write_text(child_body, encoding="utf-8")
    return entry


# ---- A1 考卷包闭包补子进程伴生脚本 ----

def test_exam_closure_covers_subprocess_helpers(tmp_path):
    """伴生脚本（subprocess 执行、不经 import）被改永绿 → 摘要必须变。"""
    entry = _pkg(tmp_path, "import sys\nsys.exit(1)\n")  # 严格判卷
    cmd = f'python "{entry}"'
    before = compute_check_digest(check_cmd=cmd)
    _pkg(tmp_path, "import sys\nsys.exit(0)\n")          # 改成永绿
    assert compute_check_digest(check_cmd=cmd) != before


def test_exam_closure_covers_transitive_helper_imports(tmp_path):
    """伴生脚本的本地导入也须入闭包（judge.py → oracle.py 型两层）。"""
    d = tmp_path
    entry = _pkg(d, "import sys, oracle\nsys.exit(0 if oracle.ok() else 1)\n")
    (d / "oracle.py").write_text("def ok():\n    return False\n",
                                 encoding="utf-8")
    cmd = f'python "{entry}"'
    before = compute_check_digest(check_cmd=cmd)
    (d / "oracle.py").write_text("def ok():\n    return True\n",
                                 encoding="utf-8")
    assert compute_check_digest(check_cmd=cmd) != before


def test_submit_rejects_after_subprocess_helper_drift(tmp_path):
    """完整提交链负例：认领后伴生 grader 改永绿，BAD 产物提交必须因
    考卷摘要失配被拒（修复前：摘要不变、BAD rc=0 通过——探针复现洞）。"""
    entry = _pkg(tmp_path / "pkg",
                 "import sys\nfrom pathlib import Path\n"
                 "p = Path('x.txt')\n"
                 "sys.exit(0 if p.is_file() and p.read_text() == 'GOOD' "
                 "else 1)\n")
    cmd = f'python "{entry}"'
    repo = Repository(tmp_path / "m" / "mission.db")
    g = Graph(repo, checker3=default_checker3)
    try:
        mid, root = g.create_mission("伴生脚本锁包", root_check=CHK)
        node = g.add_node(mid, "A", "交付 x.txt 内容 GOOD", parent_id=root,
                          check_cmd=cmd, declared_artifacts=("x.txt",))
        aid = g.start_attempt(mid, node)
        wd = Path(repo.get_attempt(aid).workdir)
        (wd / "x.txt").write_text("BAD", encoding="utf-8")
        # 认领后把伴生 grader 改成永绿（真实攻击面：考卷伴生件漂移）
        (tmp_path / "pkg" / "child_exam.py").write_text(
            "import sys\nsys.exit(0)\n", encoding="utf-8")
        with pytest.raises(GraphError, match="考卷内容已变更"):
            g.submit_attempt(mid, node, aid, artifact_paths=["x.txt"])
    finally:
        repo.close()


# ---- A2 整合考卷逐件判卷 + 冒烟 ----

_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE_CHECKS = _ROOT / "tests" / "fixtures" / "acceptance_checks" / "checks" / "mod_checks.py"
_LEGACY_CHECKS = _ROOT / "outputs" / "多agent协同验收项目" / "checks" / "mod_checks.py"
CHECKS = _FIXTURE_CHECKS if _FIXTURE_CHECKS.is_file() else _LEGACY_CHECKS

# 最小可运行 bundle（冒烟正例用；north/CNY 必须算出 v1 冻结值 26000）
BUNDLE = {
    "loader.py": (
        "import csv\n"
        "class InputError(Exception):\n"
        "    def __init__(self, code, message=''):\n"
        "        super().__init__(message or code)\n"
        "        self.code = code\n"
        "def _read(path, need):\n"
        "    with open(path, encoding='utf-8-sig', newline='') as f:\n"
        "        rows = list(csv.DictReader(f))\n"
        "    if rows:\n"
        "        miss = [c for c in need if c not in rows[0]]\n"
        "        if miss:\n"
        "            raise InputError('columns_missing')\n"
        "    return rows\n"
        "def load_orders(p):\n"
        "    return _read(p, ['order_id','region','currency',"
        "'amount_cents','status'])\n"
        "def load_refunds(p):\n"
        "    return _read(p, ['refund_id','order_id','currency',"
        "'amount_cents'])\n"
    ),
    "validation.py": (
        "def validate(orders, refunds):\n"
        "    co = [dict(o, amount_cents=int(o['amount_cents']))\n"
        "          for o in orders if o['status'] == 'paid']\n"
        "    canc = sum(1 for o in orders if o['status'] == 'cancelled')\n"
        "    cr = [dict(r, amount_cents=int(r['amount_cents']))\n"
        "          for r in refunds]\n"
        "    return co, cr, canc\n"
    ),
    "reconcile.py": (
        "def reconcile(orders, refunds, cancelled):\n"
        "    totals = {}\n"
        "    for o in orders:\n"
        "        k = (o['currency'], o['region'])\n"
        "        t = totals.setdefault(k, {'paid_orders': 0, 'gross_cents': 0,"
        " 'refund_cents': 0})\n"
        "        t['paid_orders'] += 1\n"
        "        t['gross_cents'] += o['amount_cents']\n"
        "    for r in refunds:\n"
        "        region = [o['region'] for o in orders\n"
        "                  if o['order_id'] == r['order_id']][0]\n"
        "        totals[(r['currency'], region)]['refund_cents'] += "
        "r['amount_cents']\n"
        "    out = []\n"
        "    for (cur, reg), v in sorted(totals.items()):\n"
        "        out.append({'region': reg, 'currency': cur,\n"
        "                    **v, 'net_cents': v['gross_cents']"
        " - v['refund_cents']})\n"
        "    return {'schema_version': 1, 'cancelled_orders': cancelled,\n"
        "            'totals': out}\n"
    ),
    "main.py": (
        "import argparse, json\n"
        "from loader import load_orders, load_refunds\n"
        "from validation import validate\n"
        "from reconcile import reconcile\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--orders', required=True)\n"
        "p.add_argument('--refunds', required=True)\n"
        "p.add_argument('--out', required=True)\n"
        "a = p.parse_args()\n"
        "co, cr, canc = validate(load_orders(a.orders),"
        " load_refunds(a.refunds))\n"
        "with open(a.out, 'w', encoding='utf-8') as f:\n"
        "    json.dump(reconcile(co, cr, canc), f, ensure_ascii=False)\n"
    ),
}


def _delivery_dir(tmp_path: Path, bundle: bool = True) -> Path:
    d = tmp_path / "delivery"
    (d / "inputs" / "n05").mkdir(parents=True)
    (d / "inputs" / "n06").mkdir()
    if bundle:
        for name, src in BUNDLE.items():
            (d / "inputs" / "n05" / name).write_text(src, encoding="utf-8")
    else:
        (d / "inputs" / "n05" / "loader.py").write_text("x = 1\n",
                                                        encoding="utf-8")
    (d / "inputs" / "n06" / "render.py").write_text("y = 2\n",
                                                    encoding="utf-8")
    return d


def _run_integration(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    import os
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, str(CHECKS), "integration", *args],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=120)


def test_integration_rejects_missing_entry(tmp_path):
    """P_delivery 型负例：点名入口 main.py 不在交付 → 拒收
    （修复前只数文件数，缺 main.py 漏到 ROOT 才炸）。"""
    r = _run_integration(_delivery_dir(tmp_path, bundle=True), "main.py")
    assert r.returncode == 1 and "点名入口不在申报清单" in r.stderr


def test_integration_rejects_broken_python(tmp_path):
    """逐件断言负例：inputs/ 内有语法坏 .py → 拒收。"""
    d = _delivery_dir(tmp_path, bundle=False)
    (d / "inputs" / "n05" / "bad.py").write_text("def (:\n",
                                                 encoding="utf-8")
    r = _run_integration(d)
    assert r.returncode == 1 and "编译失败" in r.stderr


def test_integration_smokes_assembled_cli(tmp_path):
    """冒烟正例（2026-10-04 申报集合语义）：bundle 申报在候选根，
    逐件过 + 入口冒烟 rc=0 且汇总达冻结期望。"""
    d = _delivery_dir(tmp_path)
    for name, src in BUNDLE.items():
        (d / name).write_text(src, encoding="utf-8")
    r = _run_integration(d, "main.py")
    assert r.returncode == 0, r.stderr
    assert "integration-ok" in r.stdout


def test_templates_carry_integration_rule():
    """平台模板四类别均带整合考卷规范示例（A2 模板级落地）。"""
    from dag_agent.task_templates import TEMPLATES
    for name, t in TEMPLATES.items():
        ex = t.get("integration_acceptance_example")
        assert ex and "integration" in ex["cmd"], name


# ---- A3 简报规范校验器（五要素齐全才放行）----

GOOD_BRIEF = (
    "任务：交付 loader.py\n"
    "【输入】orders.csv 位于 D:\\AI\\DAG-agent\\work\\brief\\orders.csv，"
    "UTF-8，列 order_id,region,currency,amount_cents,status\n"
    "【交付清单】loader.py（load_orders(path)->list[dict]）\n"
    "【接口签名】load_orders(path: str) -> list[dict]，键含 order_id/region/"
    "currency/amount_cents/status，金额保留字符串原样\n"
    "【样例】输入 o1,north,CNY,100,paid → 返回 "
    "[{'order_id':'o1','region':'north','currency':'CNY',"
    "'amount_cents':'100','status':'paid'}]\n"
    "【自测入口】python loader.py 自测：内置样例断言后打印 selftest-ok\n"
)


@pytest.mark.parametrize("drop", ["输入", "交付清单", "接口签名", "样例",
                                  "自测入口"])
def test_briefing_missing_section_rejected(drop):
    """缺任一要素 → 硬拒（ok=False，missing 指名缺段）。"""
    from dag_agent.briefing import check_briefing
    text = "\n".join(l for l in GOOD_BRIEF.splitlines()
                     if not l.startswith(f"【{drop}】"))
    r = check_briefing(text)
    assert r["ok"] is False and drop in r["missing"]


def test_briefing_empty_section_rejected():
    """段标记在但内容为空 → 同样算缺要素。"""
    from dag_agent.briefing import check_briefing
    r = check_briefing("【输入】a\n【交付清单】b\n【接口签名】c\n"
                       "【样例】\n【自测入口】\n")
    assert r["ok"] is False and set(r["missing"]) == {"样例", "自测入口"}


def test_briefing_complete_passes_with_warnings():
    """五要素齐全 → 放行；无绝对路径/超长只告警不硬拒。"""
    from dag_agent.briefing import check_briefing
    r = check_briefing(GOOD_BRIEF)
    assert r["ok"] is True and r["missing"] == []
    # 相对路径告警
    r2 = check_briefing(GOOD_BRIEF.replace("D:\\AI\\DAG-agent\\work\\brief\\",
                                           "work/brief/"))
    assert r2["ok"] is True and any("绝对路径" in w for w in r2["warnings"])
    # 超长告警
    r3 = check_briefing(GOOD_BRIEF + "补" * 900)
    assert r3["ok"] is True and any("800" in w for w in r3["warnings"])


def test_mcp_check_briefing_roundtrip(tmp_path):
    """真实 MCP 往返：dag3_check_briefing 工具齐全放行/缺要素硬拒。"""
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    env = os.environ.copy()
    env["DAG_AGENT_V3_HOME"] = str(tmp_path / "mcp_home")
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).resolve().parents[2] / "mcp_server_v3.py")],
        env=env)

    import anyio

    async def _drive():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as sess:
                await sess.initialize()
                good = await sess.call_tool("dag3_check_briefing",
                                            {"text": GOOD_BRIEF})
                g = json.loads(good.content[0].text)
                bad = await sess.call_tool("dag3_check_briefing",
                                           {"text": "只有一句话的简报"})
                b = json.loads(bad.content[0].text)
                return g, b

    g, b = anyio.run(_drive)
    assert g["ok"] and g["briefing"]["ok"] is True
    assert b["briefing"]["ok"] is False and len(b["briefing"]["missing"]) == 5
