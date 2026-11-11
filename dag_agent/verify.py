"""verify.py — 检查运行器：子进程执行checks + 超时 + 零写入守卫。

零写入守卫（对应政事堂受控执行器同款纪律）：检查必须是纯断言——
运行前后对mission目录做内容快照（相对路径→sha256），出现任何写入即FAIL。
检查命令白名单：python/pytest文件入口（与政事堂受控执行器一致）。
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path

IGNORED_DIRS = {".git", "__pycache__", ".pytest_cache"}
IGNORED_FILES = {"events.jsonl", "dag.md", "mission.json", "mission.json.tmp",
                 "mission.json.lock"}
DEFAULT_TIMEOUT = 120


def _snapshot(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]  # 原地剪枝
        for fn in filenames:
            if fn in IGNORED_FILES:
                continue
            rp = (rel / fn).as_posix()
            try:
                out[rp] = hashlib.sha256(
                    (Path(dirpath) / fn).read_bytes()).hexdigest()
            except OSError:
                out[rp] = "<unreadable>"
    return out


class CheckRunner:
    """在mission目录内运行checks；注入root便于测试。"""

    def __init__(self, mission_dir: Path, timeout: int = DEFAULT_TIMEOUT,
                 cwd: Path | None = None):
        self.dir = Path(mission_dir)
        self.cwd = Path(cwd) if cwd else self.dir
        self.timeout = timeout

    def run(self, checks: list[dict]) -> dict:
        """返回 {ok, evidence, hint?}。任一check失败即FAIL（一票否决）。"""
        if not checks:
            return {"ok": True, "evidence": "无checks：按陈述与summary直接通过",
                    "no_checks": True}
        for c in checks:
            v = self._run_one(c)
            if not v["ok"]:
                return v
        return {"ok": True, "evidence": "; ".join(
            f"{c['name']}: PASS" for c in checks)}

    def _run_one(self, check: dict) -> dict:
        name = check.get("name", "<unnamed>")
        cmd = check.get("command") or []
        if not (isinstance(cmd, list) and cmd and cmd[0] in ("python", "pytest")):
            return {"ok": False, "evidence": f"{name}: 非法command（仅允许python/pytest入口）",
                    "hint": "把command改成 [\"python\", \"check_x.py\"] 形式"}
        before = _snapshot(self.dir)
        t0 = time.monotonic()
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        try:
            proc = subprocess.run(
                cmd, cwd=str(self.cwd), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=self.timeout,
                stdin=subprocess.DEVNULL, env=env)
            exit_code, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            return {"ok": False, "evidence": f"{name}: 超时（>{self.timeout}s）",
                    "hint": "把检查拆小或降低数据规模；受控检查应秒级返回"}
        except OSError as exc:
            return {"ok": False, "evidence": f"{name}: 启动失败 {exc}",
                    "hint": "确认command首个元素是python且脚本存在"}
        dur = round(time.monotonic() - t0, 2)
        after = _snapshot(self.dir)
        tail = "\n".join(out.strip().splitlines()[-20:])[:2000]
        if before != after:
            written = sorted(set(after) - set(before))[:5]
            changed = sorted(k for k in set(after) & set(before)
                             if after[k] != before[k])[:5]
            detail = ", ".join(
                [*(f"新增:{x}" for x in written), *(f"变更:{x}" for x in changed)])
            return {"ok": False,
                    "evidence": f"{name}: 检测到检查过程写入了任务目录（{detail}）",
                    "hint": "检查必须是零写入纯断言：输出用print，产物写到任务目录之外"}
        if exit_code != 0:
            return {"ok": False,
                    "evidence": f"{name}: exit={exit_code} ({dur}s)\n{tail}",
                    "hint": "按输出修复被测对象后重新提交；不要为过检查而改检查"}
        return {"ok": True, "evidence": f"{name}: PASS exit=0 ({dur}s) {tail}"[:2000]}
