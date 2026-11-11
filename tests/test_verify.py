"""test_verify.py — 检查运行器：通过/失败/超时/零写入守卫/白名单。"""
from __future__ import annotations

from dag_agent.verify import CheckRunner


def _write_script(mission_dir, name, content):
    p = mission_dir / name
    p.write_text(content, encoding="utf-8")
    return p


def test_pass_and_fail(tmp_path):
    _write_script(tmp_path, "ok.py", "print('全部断言通过')")
    _write_script(tmp_path, "bad.py", "import sys\nprint('缺口X')\nsys.exit(1)")
    runner = CheckRunner(tmp_path)
    v = runner.run([{"name": "ok", "command": ["python", "ok.py"]}])
    assert v["ok"] and "PASS" in v["evidence"]
    v = runner.run([{"name": "bad", "command": ["python", "bad.py"]}])
    assert not v["ok"] and "缺口X" in v["evidence"] and "exit=1" in v["evidence"]


def test_write_detection(tmp_path):
    """检查过程写任务目录→FAIL（零写入守卫），写tmp目录→放行。"""
    _write_script(tmp_path, "dirty.py",
                  "from pathlib import Path\n"
                  "Path('polluted.txt').write_text('x', encoding='utf-8')\n"
                  "print('done')")
    _write_script(tmp_path, "clean_tmp.py",
                  "import tempfile, pathlib\n"
                  "pathlib.Path(tempfile.gettempdir()) / 'x' and None\n"
                  "import sys; print('clean')")
    runner = CheckRunner(tmp_path)
    v = runner.run([{"name": "dirty", "command": ["python", "dirty.py"]}])
    assert not v["ok"] and "写入" in v["evidence"] and "polluted.txt" in v["evidence"]
    v = runner.run([{"name": "clean_tmp", "command": ["python", "clean_tmp.py"]}])
    assert v["ok"]


def test_timeout(tmp_path):
    _write_script(tmp_path, "slow.py", "import time; time.sleep(5)")
    runner = CheckRunner(tmp_path, timeout=1)
    v = runner.run([{"name": "slow", "command": ["python", "slow.py"]}])
    assert not v["ok"] and "超时" in v["evidence"]


def test_command_whitelist(tmp_path):
    runner = CheckRunner(tmp_path)
    v = runner.run([{"name": "node", "command": ["node", "x.js"]}])
    assert not v["ok"] and "python/pytest" in v["evidence"]


def test_no_checks_passes_weakly(tmp_path):
    v = CheckRunner(tmp_path).run([])
    assert v["ok"] and v.get("no_checks")


def test_multi_checks_all_must_pass(tmp_path):
    _write_script(tmp_path, "a.py", "print('a ok')")
    _write_script(tmp_path, "b.py", "import sys; sys.exit(1)")
    runner = CheckRunner(tmp_path)
    v = runner.run([{"name": "a", "command": ["python", "a.py"]},
                    {"name": "b", "command": ["python", "b.py"]}])
    assert not v["ok"] and v["evidence"].startswith("b:")
