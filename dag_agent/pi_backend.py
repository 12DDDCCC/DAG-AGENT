# -*- coding: utf-8 -*-
"""pi coding agent CLI adapter (earendil-works/pi-coding-agent ≥1.5).

Executor for DAG-agent nodes: pi -p --mode json --provider glm --model <id>
Credentials via BIGMODEL_API_KEY env (models.json uses $-interpolation).
Receipt contract aligned with cc_backend (resources v2).

2026-11-08 缺陷修复（依据 work/2026-11-08-pi-independent-review P1×3）：
- 用量：增量 JSONL 解析（pi_events.PiEventStream），逐条 assistant
  message_end 累计（生命周期去重；message_update/turn_end 不计数），
  不依赖尾部缓冲；纯工具调用消息 usage 同样计入；缺失/非法如实记账
  （不补已知零）。
- 费用：Pi 的 cost.total 是本地价格表估算（缺价默认 0）——一律标
  estimated（cost_source=pi-local-pricing），不再冒充供应商 reported；
  未见过 cost 标 unknown。
- 终态：以 agent_settled / stopReason=error / aborted / length 判定
  会话结果，exit code 在 JSON 模式不作为成功依据；先正文后最终 error
  不再误报 ok。身份三分：session_id=Pi 会话 header id、response_id=
  供应商响应 ID、agent_id=Pi 会话身份（不再用 responseId 冒充）。
- 管道：stdin 写入独立线程（大 prompt 不再阻塞主循环绕过超时）；
  POSIX 端按进程组终止。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from .cc_backend import build_prompt, _utc_now_iso
from .pi_events import (PiEventStream, classify_provider_error,
                        parse_retry_after_hint)

DEFAULT_MODEL = "GLM-5.3-Flash"
REQUIRED_ENV = ("BIGMODEL_API_KEY",)


class _Tail:
    """诊断尾部缓冲（限长）：只服务于 stderr/诊断展示，用量统计走
    PiEventStream 增量解析，不受此截断影响。"""

    LIMIT = 524288

    def __init__(self, redactor=None):
        self.text = ""
        self.truncated = False
        self._redactor = redactor

    def pump(self, stream):
        try:
            while chunk := stream.read(4096):
                self.text += chunk
                if len(self.text) > self.LIMIT:
                    self.truncated = True
                    self.text = self.text[-self.LIMIT:]
        finally:
            stream.close()

    def tail(self, n: int = 300) -> str:
        t = self.text[-n:]
        if self.truncated:
            t = ("…[已截断，完整流仅用于增量统计]…\n" + t)
        return self._redactor(t) if self._redactor else t


def _stop(proc):
    """终止本次启动的进程及其子进程树；不影响其他任务。"""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
    else:
        # start_new_session=True：子进程自成进程组——按组终止（旧实现
        # 只 kill 主进程，孙进程可能残留）
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


class PiBackend:
    name = "pi-cli"

    def __init__(self, cli_path=None, model=None, env_file=None, timeout=1200,
                 allowed_tools="read,bash,edit,write", permission_mode=None,
                 thinking=None):
        self.cli = (cli_path or os.environ.get("DAG_AGENT_PI_BIN") or "pi")
        self.model = model or os.environ.get("DAG_AGENT_PI_MODEL") or DEFAULT_MODEL
        self.provider = os.environ.get("DAG_AGENT_PI_PROVIDER") or "glm"
        self.timeout = timeout
        self.allowed_tools = allowed_tools
        self.thinking = thinking
        self.extra_env = {}
        if env_file:
            p = Path(env_file)
            if p.is_file():
                for line in p.read_text(encoding="utf-8-sig").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        self.extra_env[k.strip()] = v.strip()
        key = self.extra_env.get("BIGMODEL_API_KEY") or os.environ.get("BIGMODEL_API_KEY")
        self.auth_ready = bool(key)
        self.backend_version = self._version()

    def _version(self):
        try:
            exe = self._resolve_cli()
            cmd = ["node", exe] if exe.endswith((".js", ".cjs")) else [exe]
            r = subprocess.run(cmd + ["--version"], capture_output=True, text=True, timeout=15,
                               encoding="utf8", errors="replace")
            return r.stdout.strip()[:40] or "unknown"
        except Exception:
            return "unknown"

    def _resolve_cli(self) -> str:
        """Windows 下 pi 是 npm 垫片（pi.cmd / pi）——Popen 无 shell 需真实可执行文件。"""
        import shutil
        p = Path(self.cli)
        if p.is_file() and p.suffix.lower() in (".js", ".cjs", ".exe"):
            return str(p.resolve())
        hits = shutil.which(self.cli)
        if hits:
            return hits  # which 解析出 pi.cmd/pi.exe 完整路径
        return self.cli

    def _redact(self, text: str) -> str:
        """诊断脱敏：凭据值（env 文件/BIGMODEL_API_KEY）出现即替换。"""
        secrets = [v for v in self.extra_env.values() if v and len(v) >= 8]
        env_key = os.environ.get("BIGMODEL_API_KEY")
        if env_key and len(env_key) >= 8:
            secrets.append(env_key)
        for s in secrets:
            if s in text:
                text = text.replace(s, "***REDACTED***")
        return text

    def _redact_obj(self, obj):
        """R2：递归脱敏任意返回/诊断结构（dict/list/str）——所有持久化
        与返回边界统一调用；只替换凭据值本身，保留错误类型、状态码、
        时序与统计（不做无差别删除）。"""
        if isinstance(obj, str):
            return self._redact(obj)
        if isinstance(obj, dict):
            return {k: self._redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._redact_obj(v) for v in obj]
        if isinstance(obj, tuple):
            return tuple(self._redact_obj(v) for v in obj)
        return obj

    def available(self) -> tuple[bool, str]:
        import shutil
        if not (shutil.which(self.cli) or Path(self.cli).is_file()):
            return False, f"pi CLI 不可执行：{self.cli}"
        if not self.auth_ready:
            return False, ("缺少 BIGMODEL_API_KEY（环境变量或 env_file）；"
                           "pi 的 models.json 通过 $BIGMODEL_API_KEY 插值取 key")
        return True, "ok"

    # ---- 回执组装 ----

    def _receipt(self, stream: PiEventStream, started: str, ok: bool,
                 usage_note: str) -> dict:
        """平台 resources v2 回执：用量/费用/身份/契约状态如实记录。
        started 由 run() 局部传入（R6：禁止共享可变会话字段——并发
        attempt 各自独立）。"""
        st = stream.stats
        resources = {"backend": self.name, "origin": self.name,
                     "backend_version": self.backend_version,
                     "provider_model": st.model or self.model,
                     "executor_started_at": started,
                     "cost_currency": "USD",
                     "cost": stream.cost_of(),
                     "receipt_status": "incomplete"}
        # 身份三分（P1 修复）：session_id=Pi 会话身份；response_id=供应商
        # 响应 ID；agent_id=Pi 会话身份（不再用 responseId 冒充 session）。
        resources["response_id"] = st.response_id
        if st.session_id:
            resources["agent_id"] = st.session_id
            resources["pi_session_id"] = st.session_id
        # usage_raw=最后一条 assistant 消息的原始 usage（逐条累计见
        # usage_total_*；不把最后一条冒充全程）
        last_usage = {}
        if st.messages_total:
            # result 统计里保留的最后 usage 由解析器无存储——从
            # usage 聚合字段重建摘要（非伪造：字段级缺失保持缺失）
            last_usage = {"messages_total": st.messages_total,
                          "messages_counted": st.messages_counted,
                          "tokens_sum": st.tokens_total,
                          "usage_complete": st.usage_complete}
        if st.tokens_total is not None:
            resources["tokens"] = st.tokens_total
        else:
            # 全部消息缺 totalTokens：缺失不补已知零——tokens 缺省 0 且
            # usage_complete=False / receipt incomplete 如实标注
            pass
        resources["usage_stats"] = last_usage
        if st.usage_errors:
            resources["usage_errors"] = st.usage_errors
        # R6：部分费用缺失同样使回执不完整（不只在诊断里提一句）
        cost = resources.get("cost") or {}
        cost_complete = bool(cost.get("cost_complete", False)) \
            if cost.get("basis") == "estimated" else False
        if cost.get("basis") == "unknown" and st.messages_total > 0:
            cost_complete = False
        resources["receipt_status"] = ("valid" if (ok and st.usage_complete
                                                   and cost_complete
                                                   and st.tokens_total is not None
                                                   and st.session_id)
                                       else "incomplete")
        if usage_note:
            resources["usage_note"] = usage_note
        return resources

    def run(self, statement: str, workdir: Path, check_cmd: str | None,
            title: str = "", cancel_event=None) -> dict:
        # R6：started 为本次 run 的局部变量，经参数传入 _receipt——
        # 共享 backend 并发运行时各 attempt 起始时间独立（self._started
        # 共享写曾使并发回执时间互相覆盖）
        started = _utc_now_iso()
        t0 = time.monotonic()
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        out = _Tail(redactor=self._redact)   # 诊断尾部（限长，不承载统计）
        err = _Tail(redactor=self._redact)
        stream = PiEventStream()             # 增量统计（不受尾部截断影响）
        proc = None
        cancelled = timed_out = False
        failure = ""
        outcome, outcome_detail = "incomplete", ""
        try:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                failure = "执行请求在启动前已取消"
            else:
                env = os.environ.copy()
                env.update(self.extra_env)
                prompt = build_prompt(statement, check_cmd, title)
                exe = self._resolve_cli()
                args = ([exe] if exe.lower().endswith((".cmd", ".bat", ".exe"))
                        else ["node", exe])
                args += ["-p", "--mode", "json",
                         "--provider", self.provider,
                         "--model", self.model,
                         "--no-session",
                         "-t", self.allowed_tools]
                if self.thinking:
                    args += ["--thinking", self.thinking]
                proc = subprocess.Popen(args, cwd=str(workdir), env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        stdin=subprocess.PIPE, text=True,
                                        encoding="utf8", errors="replace",
                                        shell=False,
                                        start_new_session=os.name != "nt")

                def _write_stdin():
                    # 独立线程写 stdin：大 prompt 在子进程未消费时不再
                    # 阻塞主循环（旧实现 write 卡死会绕过超时判定）
                    try:
                        proc.stdin.write(prompt)
                        proc.stdin.flush()
                    except (OSError, ValueError):
                        pass  # 进程早退/管道关闭：由收尾逻辑结算
                    finally:
                        try:
                            proc.stdin.close()
                        except (OSError, ValueError):
                            pass

                stdin_th = threading.Thread(target=_write_stdin, daemon=True)
                stdin_th.start()
                # stdout pump：逐块喂增量解析器（统计不依赖尾部缓冲）
                def _pump_stdout():
                    try:
                        while chunk := proc.stdout.read(4096):
                            stream.feed(chunk)
                            out.text += chunk
                            if len(out.text) > _Tail.LIMIT:
                                out.truncated = True
                                out.text = out.text[-_Tail.LIMIT:]
                    except (OSError, ValueError):
                        pass
                    finally:
                        try:
                            proc.stdout.close()
                        except (OSError, ValueError):
                            pass

                threads = [threading.Thread(target=_pump_stdout, daemon=True),
                           threading.Thread(target=err.pump, args=(proc.stderr,),
                                            daemon=True)]
                for th in threads:
                    th.start()
                while proc.poll() is None:
                    if cancel_event is not None and cancel_event.is_set():
                        cancelled = True
                        break
                    if time.monotonic() - t0 >= self.timeout:
                        timed_out = True
                        break
                    time.sleep(.05)
                if proc.poll() is None:
                    _stop(proc)
                stdin_th.join(timeout=3)
                for th in threads:
                    th.join(timeout=3)
                stream.close()   # 残余无 LF 收尾行仍解析并如实标记
                stats = stream.stats
                outcome, outcome_detail = stream.outcome()
                # 终态判定（P1 修复）：JSON 模式 exit code 不反映会话
                # 失败（print-mode 仅 text 模式设置）；以事件流协议判定
                if cancelled:
                    failure = "已取消"
                elif timed_out:
                    failure = f"pi 会话超时（>{self.timeout}s）"
                elif outcome == "failed":
                    error_class = classify_provider_error(
                        outcome_detail,
                        *(r.get("finalError") or r.get("errorMessage") or ""
                          for r in stats.retries),
                        provider=self.provider)
                    failure = (f"pi 会话最终失败：{outcome_detail}"
                               f"[class={error_class}]")
                elif outcome == "aborted":
                    failure = f"pi 会话中止：{outcome_detail}"
                elif outcome == "incomplete":
                    failure = f"pi 会话未确认完成：{outcome_detail}"
                elif proc.returncode not in (0, None):
                    # 事件流显示完成但进程异常退出：如实失败（诊断在 stderr）
                    failure = (f"pi 退出码 {proc.returncode}（事件流已 completed）；"
                               f"stderr尾部: {err.tail(200)}")
                ok = not failure
                error_class = ""
                if failure and outcome == "failed":
                    error_class = classify_provider_error(
                        outcome_detail,
                        *(r.get("finalError") or r.get("errorMessage") or ""
                          for r in stats.retries),
                        provider=self.provider)
                elif failure and not cancelled and not timed_out:
                    error_class = classify_provider_error(outcome_detail,
                                                          provider=self.provider)
                usage_note = ""
                if not stats.usage_complete:
                    usage_note = ("部分 assistant 消息缺 totalTokens/非法："
                                  "tokens 为合法条合计，全程总量未知")
                resources = self._receipt(stream, started, ok, usage_note)
                if error_class:
                    resources["error_class"] = error_class
                # R5：Retry-After 有效提示（结构化错误文本中提取）——
                # scheduler 优先采用；非法/缺失走有上限退避
                retry_hint = parse_retry_after_hint(outcome_detail)
                if retry_hint is None and stats.retries:
                    retry_hint = parse_retry_after_hint(
                        str(stats.retries[-1].get("errorMessage") or ""))
                if error_class == "rate_limited" and retry_hint is not None:
                    resources["retry_after_s"] = retry_hint
                resources["session_outcome"] = outcome
                resources["executor_ended_at"] = _utc_now_iso()
                resources["duration_ms"] = int((time.monotonic() - t0) * 1000)
                resources["exit_code"] = proc.returncode if proc else None
                resources["cancelled"] = cancelled
                resources["timeout"] = timed_out
                # 诊断留痕（内部 stats 的可输出投影——错误类型/状态码/
                # 时序/统计保留；出口统一脱敏见 return 前 _redact_obj）
                resources["diagnostics"] = {
                    "records_total": stats.records_total,
                    "parse_errors": stats.parse_errors,
                    "trailing_partial": stats.trailing_partial,
                    "messages_total": stats.messages_total,
                    "messages_counted": stats.messages_counted,
                    "agent_end_count": stats.agent_end_count,
                    "settled": stats.settled,
                    "aborted": stats.aborted,
                    "will_retry": stats.will_retry,
                    "duplicate_message_ends": stats.duplicate_message_ends,
                    "usage_component_errors": stats.usage_component_errors,
                    "retries": stats.retries,
                    "usage_errors": stats.usage_errors,
                    "cost_messages_missing": stats.cost_messages_missing,
                    "cost_messages_invalid": stats.cost_messages_invalid,
                    "stderr_tail": err.tail(300),
                    "stdout_truncated": out.truncated,
                }
                summary = (stats.result_text[:300] if ok and stats.result_text
                           else failure or "pi 无输出")
                # R2：出口统一脱敏——summary/resources 整树（含
                # errorMessage/finalError/usage_errors/retries/嵌套结构）
                # 在返回前替换凭据值；错误类型/状态码/时序/统计保留
                result = {"ok": ok and not cancelled and not timed_out,
                          "summary": summary,
                          "session_id": stats.session_id,
                          "resources": resources}
                return self._redact_obj(result)
        except Exception as e:
            failure = f"pi 执行异常：{type(e).__name__}"
            if proc is not None and proc.poll() is None:
                _stop(proc)
        resources = {"backend": self.name, "origin": self.name,
                     "backend_version": self.backend_version,
                     "provider_model": self.model,
                     "executor_started_at": started,
                     "cost_currency": "USD",
                     "cost": {"amount": None, "basis": "unknown",
                              "currency": "USD"},
                     "receipt_status": "incomplete",
                     "session_outcome": outcome,
                     "executor_ended_at": _utc_now_iso(),
                     "duration_ms": int((time.monotonic() - t0) * 1000),
                     "exit_code": proc.returncode if proc else None,
                     "cancelled": cancelled,
                     "timeout": timed_out}
        return self._redact_obj({"ok": False,
                                 "summary": failure or "pi 执行失败",
                                 "session_id": "",
                                 "resources": resources})
