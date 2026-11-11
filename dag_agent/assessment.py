"""v3 步骤2：真实检查器——白名单入口+超时+检查期零写入守卫。

对被测执行目录做前后 sha256 快照：检查命令若在检查期间写入/删除/改动任何
未申报内容，检查直接判失败（杜绝"改文件让检查通过"的作弊面）。
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import shlex
import subprocess
import threading
from pathlib import Path

IGNORED_DIRS = {"__pycache__", ".pytest_cache", ".git", "node_modules", ".dag_tmp"}
IGNORED_FILES = {".run.lock"}
WHITELIST = {"python", "pytest", "py"}
# 判定用 Path(parts[0]).stem：Windows 下 sys.executable 是绝对路径指向
# python.exe（name 不在白名单会被误拒）——stem 剥掉 .exe/.cmd 等可执行
# 后缀，白名单语义跨平台一致；白名单外名称（evil.exe 等）仍被拒
DEFAULT_TIMEOUT = 120

_VERIFY_CTX = threading.local()


@contextlib.contextmanager
def verify_context(*, mission_id: str | None = None,
                   node_id: str | None = None,
                   attempt_id: str | None = None):
    """A2（第三轮）：平台向检查器子进程显式注入当前验收对象权威身份
    （mission/node/attempt），使根检查器精确绑定本次 attempt 而无需从
    历史 attempt 数量猜测。"""
    prev = getattr(_VERIFY_CTX, "env", None)
    env = dict(prev or {})
    if mission_id is not None:
        env["DAG_AGENT_VERIFY_MISSION_ID"] = str(mission_id)
    if node_id is not None:
        env["DAG_AGENT_VERIFY_NODE_ID"] = str(node_id)
    if attempt_id is not None:
        env["DAG_AGENT_VERIFY_ATTEMPT_ID"] = str(attempt_id)
    _VERIFY_CTX.env = env
    try:
        yield
    finally:
        _VERIFY_CTX.env = prev


def get_verify_env() -> dict[str, str]:
    base = dict(os.environ)
    extra = getattr(_VERIFY_CTX, "env", None)
    if extra:
        base.update(extra)
    return base


def _snapshot(root: Path) -> dict[str, str]:
    items: dict[str, str] = {}
    if not root.exists():
        return items
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if set(p.parts) & {d for d in IGNORED_DIRS}:
            continue
        if p.name in IGNORED_FILES:
            continue
        items[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return items


def _kill_tree(pid: int):
    """Windows 杀整棵进程树（检查命令的孙进程不随 shell 死）。"""
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=15)
    except Exception:
        pass


def run_check(cmd: str | None, cwd: Path, timeout: int = DEFAULT_TIMEOUT) -> tuple[bool, str]:
    """执行验收命令。返回 (ok, evidence)。无命令=fail-closed。"""
    if not cmd:
        return False, "未配置验收命令：fail-closed"
    try:
        parts = shlex.split(cmd)
    except ValueError as e:
        return False, f"命令解析失败：{e}"
    if not parts or Path(parts[0]).stem not in WHITELIST:
        return False, f"命令入口不在白名单：{parts[:1]}"
    cwd.mkdir(parents=True, exist_ok=True)
    before = _snapshot(cwd)
    proc = subprocess.Popen(
        cmd, shell=True, cwd=str(cwd),
        env=get_verify_env(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, text=True, errors="replace",
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        return False, f"检查超时（{timeout}s），已终止进程树"
    tail = (out or "") + (err or "")
    evidence = tail.strip() or f"exit={proc.returncode}"
    after = _snapshot(cwd)
    if before != after:
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        changed = sorted(k for k in (set(before) & set(after)) if before[k] != after[k])
        return False, (f"检查期间写入了被测目录（零写入守卫）："
                       f"新增{added[:5]} 删除{removed[:5]} 改动{changed[:5]}；{evidence[:200]}")
    return proc.returncode == 0, evidence


# ---- v3.1 三分判决 ----
# pass=产物合格；fail=产物不合格（打回执行者）；error=考卷/环境故障（不打回，
# 走维修通道）。判定规则：解析失败/白名单外/超时/检查器写现场 → error；
# **约定退出码 2 = 检查器自身故障 → error**（python 找不到脚本报 exit 2、
# pytest 用法/收集错误 exit 2、check_http 网络故障 exit 2——"考卷跑不起来"
# 与"产物不合格"借此区分）；其余非 0 → fail；无命令 → fail（fail-closed）。

def run_check3(cmd: str | None, cwd: Path,
               timeout: int = DEFAULT_TIMEOUT) -> tuple[str, str]:
    """三分版检查器。返回 (verdict ∈ pass|fail|error, evidence)。"""
    if not cmd:
        return "fail", "未配置验收命令：fail-closed"
    try:
        parts = shlex.split(cmd)
    except ValueError as e:
        return "error", f"考卷故障·命令解析失败：{e}"
    if not parts or Path(parts[0]).stem not in WHITELIST:
        return "error", f"考卷故障·入口不在白名单：{parts[:1]}"
    cwd.mkdir(parents=True, exist_ok=True)
    before = _snapshot(cwd)
    try:
        proc = subprocess.Popen(
            cmd, shell=True, cwd=str(cwd),
            env=get_verify_env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, text=True, errors="replace",
        )
    except OSError as e:
        return "error", f"考卷故障·无法启动：{e}"
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        return "error", f"考卷故障·检查超时（{timeout}s），已终止进程树"
    tail = (out or "") + (err or "")
    evidence = tail.strip() or f"exit={proc.returncode}"
    after = _snapshot(cwd)
    if before != after:
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        changed = sorted(k for k in (set(before) & set(after)) if before[k] != after[k])
        return "error", (f"考卷故障·检查期间写入被测目录（考卷不得咬坏现场）："
                         f"新增{added[:5]} 删除{removed[:5]} 改动{changed[:5]}；"
                         f"{evidence[:200]}")
    if proc.returncode == 0:
        return "pass", evidence
    if proc.returncode == 2:   # 约定：2=检查器自身故障（找不到脚本/用法/网络）
        return "error", f"考卷故障·exit=2：{evidence[:300]}"
    return "fail", evidence


def default_checker3(cmd: str | None, cwd: Path,
                     timeout: int = DEFAULT_TIMEOUT) -> tuple[str, str]:
    """Graph 的三分默认检查器。"""
    return run_check3(cmd, cwd, timeout=timeout)
