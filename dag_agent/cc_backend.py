"""v3 步骤3：执行后端——claude CLI 子进程（MiniMax 模型驱动）。

边界与安全：
- API 密钥只从 env 文件（DAG_AGENT_ENV_FILE，默认 <项目>/.env.local）读入
  子进程环境变量，不落日志、不进命令行、不入库；
- 工作目录锁定为尝试独立目录（work/<nid>/<aid>/）；
- 默认 permission-mode=acceptEdits + 只开 Write/Edit/Read（不开 Bash），
  需要提权由 backend 参数显式给；
- 超时杀进程；资源用量（cost/duration/session_id）回填 attempts.resources_json。
- 单元测试用假 CLI 垫片（DAG_AGENT_CC_BIN 指向 python 脚本），不打真 API。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

DEFAULT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env.local"
DEFAULT_MODEL = "MiniMax-M3.1"
DEFAULT_TIMEOUT = 600

_REQUIRED_ENV_KEYS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")


def _utc_now_iso() -> str:
    """UTC ISO 起止（回执契约 v2）：带时区，进程区间口径。"""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _cost_of(payload: dict) -> dict:
    """结构化费用口径（阶段 1A）：多模型分项求和，不取首项；分项之和与
    CLI 汇总一致时用汇总（reported）；缺汇总用分项和（estimated，模型侧
    未给权威总额）；分项无数值且无汇总 → unknown（缺失不得按已确认零
    费用展示，也不重复累加相同汇总值）。"""
    model_usage = payload.get("modelUsage") or {}
    parts = [v.get("costUSD") for v in model_usage.values()
             if isinstance(v, dict)]
    nums = [float(x) for x in parts if isinstance(x, (int, float))]
    total = payload.get("total_cost_usd")
    if model_usage and nums:
        s = round(sum(nums), 9)
        if isinstance(total, (int, float)) and abs(s - float(total)) < 1e-6:
            return {"amount": float(total), "basis": "reported",
                    "currency": "USD"}
        return {"amount": s,
                "basis": "reported" if isinstance(total, (int, float))
                else "estimated",
                "currency": "USD"}
    if isinstance(total, (int, float)):
        return {"amount": float(total), "basis": "reported",
                "currency": "USD"}
    return {"amount": None, "basis": "unknown", "currency": "USD"}


_version_cache: dict[str, str] = {}


def _cli_version(cli: str) -> str:
    """CLI 版本一次性探测（缓存）；垫片与探测失败如实标注，不致命。"""
    if cli in _version_cache:
        return _version_cache[cli]
    if cli.endswith(".py"):
        _version_cache[cli] = "fake-shim"
        return "fake-shim"
    try:
        r = subprocess.run(f'"{cli}" --version', shell=True,
                           capture_output=True, text=True, timeout=30)
        out = (r.stdout or "").strip().splitlines()
        _version_cache[cli] = (out[0].strip() if out and out[0].strip()
                               else "unknown")
    except Exception:
        _version_cache[cli] = "unknown"
    return _version_cache[cli]


def load_env_file(path: Path | None = None) -> dict[str, str]:
    """解析 KEY=VALUE 行；缺失文件返回空 dict（真实调用前再校验）。"""
    p = Path(path or os.environ.get("DAG_AGENT_ENV_FILE") or DEFAULT_ENV_FILE)
    if not p.is_file():
        return {}
    out: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def build_prompt(statement: str, check_cmd: str | None, title: str) -> str:
    check_line = (f"交付后将运行验收命令：`{check_cmd}`（在交付目录内执行，"
                  "验收器文件在目录外，你不可修改验收标准）。"
                  if check_cmd else
                  "注意：本节点未配置验收命令，提交会按 fail-closed 判失败。")
    return (
        "你是 DAG-agent 的执行者，只做下面这一个节点的范围内的事。\n\n"
        f"节点：{title}\n目标：{statement}\n\n{check_line}\n"
        "要求：交付物写成文件放在当前目录；不要在当前目录之外留下任何文件；"
        "完成后回复一行 DONE 加一句交付说明。"
    )


class _BoundedSink:
    """子进程输出持续排空（reader 线程用）：有界收集防管道堵塞，
    超限截断但保留尾部（错误信息多在尾部）。"""

    LIMIT = 262144  # 256KB/流

    def __init__(self):
        self._buf: list[str] = []
        self._size = 0
        self._lock = threading.Lock()

    def pump(self, stream):
        try:
            for line in iter(stream.readline, ""):
                with self._lock:
                    self._buf.append(line)
                    self._size += len(line)
                    while self._size > self.LIMIT and self._buf:
                        self._size -= len(self._buf[0])
                        self._buf.pop(0)
        except Exception:
            pass  # 流早闭/中断：已有内容即证据
        finally:
            try:
                stream.close()
            except Exception:
                pass

    @property
    def text(self) -> str:
        with self._lock:
            return "".join(self._buf)


def _dump_session_logs(workdir: Path, sink_out: "_BoundedSink",
                       sink_err: "_BoundedSink", resources: dict) -> None:
    """原始会话日志落盘（阶段 2 判据：原始日志证明 CLI 会话完成读写与
    自测）——resources.logs 记引用与字节数；DB 正文外仍可复查全文。"""
    try:
        log_dir = Path(workdir) / ".dag_session_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        p_out = log_dir / "cli-stdout.log"
        p_err = log_dir / "cli-stderr.log"
        p_out.write_text(sink_out.text, encoding="utf-8", errors="replace")
        p_err.write_text(sink_err.text, encoding="utf-8", errors="replace")
        resources["logs"] = {
            "stdout": p_out.name, "stderr": p_err.name,
            "dir": str(log_dir),
            "stdout_bytes": len(sink_out.text.encode("utf-8")),
            "stderr_bytes": len(sink_err.text.encode("utf-8"))}
    except Exception:
        pass  # 日志落盘失败不阻断结算（回执正文仍是权威证据）


class ClaudeCliBackend:
    name = "claude-cli"

    def __init__(self, cli_path: str | None = None, model: str | None = None,
                 env_file: Path | None = None, timeout: int = DEFAULT_TIMEOUT,
                 allowed_tools: str = "Write Edit Read Bash",
                 permission_mode: str = "acceptEdits",
                 isolate_config: bool = True):
        env = load_env_file(env_file)
        self.model = model or os.environ.get("DAG_AGENT_CC_MODEL") or env.get(
            "ANTHROPIC_MODEL") or DEFAULT_MODEL
        self.cli = cli_path or os.environ.get("DAG_AGENT_CC_BIN") or "claude"
        if self.cli.endswith(".py"):
            self.cli = str(Path(self.cli).resolve())  # 垫片按绝对路径引用
        self.timeout = timeout
        # 1C：allowedTools（自动批准规则）与 CLI 的 --tools（工具可用集合）
        # 是两个旋钮；默认含 Bash——执行者要亲自运行任务指定自测（非交互
        # -p 模式无人工批准通道，不自批即无法自测）。allowedTools 不是 OS
        # 沙箱证明；越权防线=平台侧（考卷内容摘要 P0/P4 双核验、判卷在
        # 交付区外独立执行、accepted 只能经平台 submit 发布）。
        self.allowed_tools = allowed_tools
        self.permission_mode = permission_mode
        # 1C 配置隔离：节点会话不继承宿主 ~/.claude（settings/MCP/hooks/
        # auto-memory 全部落隔离目录）；不改用户全局设置。
        self.isolate_config = isolate_config
        self.extra_env = {
            k: v for k, v in env.items()
            if k in _REQUIRED_ENV_KEYS or k in ("ANTHROPIC_MODEL",)
        }
        # claude CLI 2.1.232 对未知模型强制校验上下文窗口并中止；MiniMax 系列
        # （M2/M3.1）都不在其目录里。该开关恢复"等 API 裁决"行为（API 仍按
        # 真实窗口限制）。
        self.extra_env.setdefault(
            "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT", "1")
        self.backend_version = _cli_version(self.cli)

    def _prepare_env(self, workdir: Path) -> tuple[dict, str]:
        """子进程 env 与 flags（1C 隔离与权限配置，可单测）。"""
        env = os.environ.copy()
        env.update(self.extra_env)
        if self.isolate_config:
            cfg_home = Path(workdir) / ".claude-home"
            cfg_home.mkdir(parents=True, exist_ok=True)
            env["CLAUDE_CONFIG_DIR"] = str(cfg_home)
        # --strict-mcp-config（2.1.232 实测支持）：不带 --mcp-config 即
        # 忽略宿主一切 MCP 配置——节点会话不继承宿主 MCP 工具
        flags = (f'-p --model {self.model} --output-format json '
                 f'--allowedTools {self.allowed_tools} '
                 f'--permission-mode {self.permission_mode} '
                 f'--strict-mcp-config')
        return env, flags

    def available(self) -> tuple[bool, str]:
        missing = [k for k in _REQUIRED_ENV_KEYS if k not in self.extra_env]
        if missing:
            return False, f"缺少环境变量：{missing}（env 文件未配置？）"
        if shutil.which(self.cli) is None and not Path(self.cli).is_file():
            return False, f"claude CLI 不可执行：{self.cli}"
        return True, "ok"

    def run(self, statement: str, workdir: Path, check_cmd: str | None,
            title: str = "", cancel_event=None) -> dict:
        """执行一个节点。返回 {ok, summary, session_id, resources}。

        ok 只代表 agent 会话正常结束且自报完成；真假由验收命令裁决。
        resources 为契约 v2 回执：backend/provider_model/session_id、
        UTC 起止（datetime timezone.utc）与单调钟 duration_ms（进程区间
        口径，非 attempt 认领-提交窗口）、usage 原始字段、结构化费用
        {amount, basis: reported|estimated|unknown}、receipt_status。
        cancel_event（阶段 1B）：置位 → 杀进程树并在 10s 内确认退出；
        stdout/stderr 由 reader 线程持续排空（有界收集，防管道堵塞）。
        """
        # Codex N5：启动入口拒绝预取消的执行请求（取消事件在调用前
        # 已置位 → 不创建任何子进程，返回 cancelled 回执）
        if cancel_event is not None and cancel_event.is_set():
            return {"ok": False,
                    "summary": "执行请求在启动前已被取消：未创建子进程",
                    "session_id": "",
                    "resources": {
                        "backend": self.name,
                        "backend_version": self.backend_version,
                        "provider_model": self.model,
                        "session_id": "",
                        "executor_started_at": _utc_now_iso(),
                        "executor_ended_at": _utc_now_iso(),
                        "duration_ms": 0,
                        "exit_code": None,
                        "usage_raw": {},
                        "cost": {"amount": None, "basis": "unknown",
                                 "currency": "USD"},
                        "receipt_status": "incomplete",
                        "cancelled": True,
                        "pre_cancel": True}}
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        prompt = build_prompt(statement, check_cmd, title)
        env, flags = self._prepare_env(workdir)
        # prompt 走 stdin（-p 无参数时从 stdin 读指令）：
        # cmd.exe 不支持带换行的引号参数，任务书会在第一个换行处被截断。
        if self.cli.endswith(".py"):
            cmd = f'"{sys.executable}" "{self.cli}" {flags}'  # 测试垫片
        else:
            cmd = f'"{self.cli}" {flags}'
        t0 = time.monotonic()
        started_iso = _utc_now_iso()
        proc = subprocess.Popen(
            cmd, shell=True, cwd=str(workdir),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.PIPE, text=True, errors="replace", env=env,
        )
        sink_out, sink_err = _BoundedSink(), _BoundedSink()
        readers = [
            threading.Thread(target=sink_out.pump, args=(proc.stdout,),
                             daemon=True),
            threading.Thread(target=sink_err.pump, args=(proc.stderr,),
                             daemon=True),
        ]
        for th in readers:
            th.start()
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except Exception:
            pass  # 进程早退时 stdin 写失败不阻断取消/超时路径
        cancelled = False
        timed_out = False
        deadline = t0 + self.timeout
        try:
            while True:
                try:
                    proc.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    if cancel_event is not None and cancel_event.is_set():
                        cancelled = True
                        _kill_tree(proc.pid)
                        break
                    if time.monotonic() > deadline:
                        timed_out = True
                        _kill_tree(proc.pid)
                        break
        finally:
            for th in readers:
                th.join(timeout=3)
        if timed_out:
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass
            timeout_result = {
                "ok": False,
                "summary": f"执行超时（{self.timeout}s），已终止进程树",
                "session_id": "",
                "resources": {
                    "backend": self.name,
                    "backend_version": self.backend_version,
                    "provider_model": self.model,
                    "session_id": "",
                    "executor_started_at": started_iso,
                    "executor_ended_at": _utc_now_iso(),
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                    "exit_code": proc.returncode,
                    "usage_raw": {},
                    "cost": {"amount": None, "basis": "unknown",
                             "currency": "USD"},
                    "receipt_status": "incomplete",
                    "timeout": True}}
            _dump_session_logs(workdir, sink_out, sink_err,
                               timeout_result["resources"])
            return timeout_result
        duration_ms = int((time.monotonic() - t0) * 1000)
        if cancelled:
            cancelled_result = {
                "ok": False,
                "summary": "执行被取消：已终止进程树并确认退出",
                "session_id": "",
                "resources": {
                    "backend": self.name,
                    "backend_version": self.backend_version,
                    "provider_model": self.model,
                    "session_id": "",
                    "executor_started_at": started_iso,
                    "executor_ended_at": _utc_now_iso(),
                    "duration_ms": duration_ms,
                    "exit_code": proc.returncode,
                    "usage_raw": {},
                    "cost": {"amount": None, "basis": "unknown",
                             "currency": "USD"},
                    "receipt_status": "incomplete",
                    "cancelled": True}}
            _dump_session_logs(workdir, sink_out, sink_err,
                               cancelled_result["resources"])
            return cancelled_result
        stdout = sink_out.text
        stderr = sink_err.text
        payload = self._parse_output(stdout)
        ok = (proc.returncode == 0 and bool(payload)
              and not payload.get("is_error", False))
        usage = payload.get("usage") or {}
        cost = _cost_of(payload)
        # v1 核心字段映射（CLI 会话身份来源=真实 session_id）：
        # origin=执行器名；agent_id←session_id（未返回时如实缺失，不伪造）；
        # tokens←usage 输入+输出求和（CLI 无独立 tokens 总数字段）；
        # cost_currency←费用币种 USD。映射规则即此处代码，账目可溯。
        in_tok = usage.get("input_tokens")
        out_tok = usage.get("output_tokens")
        tokens = (in_tok + out_tok) if isinstance(in_tok, (int, float)) \
            and isinstance(out_tok, (int, float)) else None
        # 输出不可解析（garbage/崩溃）：有 exit_code 与进程区间，无会话与
        # 用量——如实 incomplete，不伪造 valid 成功回执
        receipt_status = "valid" if payload else "incomplete"
        resources = {
            "backend": self.name,
            "backend_version": self.backend_version,
            "provider_model": self.model,
            "origin": self.name,
            "cost_currency": "USD",
            "session_id": payload.get("session_id", ""),
            "executor_started_at": started_iso,
            "executor_ended_at": _utc_now_iso(),
            "duration_ms": duration_ms,
            "exit_code": proc.returncode,
            "num_turns": payload.get("num_turns"),
            "usage_raw": usage,
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "model": self.model,
            "cost": cost,
            "receipt_status": receipt_status,
        }
        if not payload:
            resources["incomplete_reason"] = "CLI 输出不可解析（无 JSON 载荷）"
        else:
            # v1 核心字段缺失时省键（不带 None/null——契约对在场键校验非空）
            if payload.get("session_id"):
                resources["agent_id"] = payload["session_id"]
            if tokens is not None:
                resources["tokens"] = int(tokens)
        session_id = payload.get("session_id", "")
        summary = (payload.get("result") or "")[:300] or \
            (stderr or "")[-300:] or f"exit={proc.returncode}"
        _dump_session_logs(workdir, sink_out, sink_err, resources)
        return {"ok": ok, "summary": summary, "session_id": session_id,
                "resources": resources}

    def _parse_output(self, stdout: str) -> dict:
        """claude --output-format json 输出可能带前导杂行；取最后一个 JSON 对象。"""
        for line in reversed([l for l in stdout.splitlines() if l.strip()]):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        # 整段尝试（垫片可能单行输出）
        try:
            return json.loads(stdout.strip())
        except (json.JSONDecodeError, ValueError):
            return {}


def _kill_tree(pid: int):
    """Windows 杀整棵进程树（shell 的孙进程不随 shell 死）。"""
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=15)
    except Exception:
        pass


def record_resources(repo, attempt_id: str, run_result: dict):
    """（兼容薄壳）转调 executor_contract.record_execution 统一结算入口。

    阶段 1A 起正式路径（scheduler）已直用统一入口；本壳保留给既有直调
    测试与脚本——归属按 attempt 自身解析，同样受契约校验与幂等约束。
    """
    from .executor_contract import record_execution
    return record_execution(repo, attempt_id, run_result)
