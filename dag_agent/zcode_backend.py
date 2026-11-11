"""ZCode Agent CLI adapter (local 0.17.0 protocol).

Each attempt gets an isolated HOME/data store and a temporary provider config.
No desktop database access, shared sessions, implicit login or credential export.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from .cc_backend import build_prompt, _utc_now_iso


class _Tail:
    LIMIT = 524288

    def __init__(self):
        self.text = ""
        self.truncated = False

    def pump(self, stream):
        try:
            while chunk := stream.read(4096):
                self.text += chunk
                if len(self.text) > self.LIMIT:
                    self.truncated = True
                    self.text = self.text[-self.LIMIT:]
        finally:
            stream.close()


def _stop(proc):
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _payload(text):
    try:
        value = json.loads(text.strip())
        if isinstance(value, dict) and "response" in value:
            return value
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        try:
            value = json.loads(line)
            if isinstance(value, dict) and "response" in value:
                return value
        except ValueError:
            continue
    return {}


def _secrets(value):
    found = []
    if isinstance(value, dict):
        for k, v in value.items():
            if any(t in k.lower() for t in ("key", "token", "secret", "password")) and isinstance(v, str) and v:
                found.append(v)
            else:
                found.extend(_secrets(v))
    elif isinstance(value, list):
        for v in value:
            found.extend(_secrets(v))
    return found


class ZCodeCliBackend:
    name = "zcode-cli"

    def __init__(self, cli_path=None, node_path=None, provider_config=None, timeout=600,
                 permission_mode="edit"):
        candidate = cli_path or os.environ.get("DAG_AGENT_ZCODE_BIN")
        if not candidate:
            local = Path("D:/zcode/resources/glm/zcode.cjs")
            candidate = str(local) if local.is_file() else shutil.which("zcode") or "zcode"
        self.cli = str(Path(candidate).resolve()) if Path(candidate).is_file() else candidate
        self.node = node_path or os.environ.get("DAG_AGENT_ZCODE_NODE") or shutil.which("node")
        self.provider_config = provider_config or os.environ.get("DAG_AGENT_ZCODE_PROVIDER_CONFIG")
        self.timeout = timeout
        if permission_mode not in {"edit", "build", "guarded", "yolo"}:
            raise ValueError("不支持的ZCode权限模式")
        self.permission_mode = permission_mode
        self.backend_version = "unknown"

    def _command(self):
        suffix = Path(self.cli).suffix.lower()
        if suffix in (".cjs", ".mjs", ".js"):
            if not self.node:
                raise ValueError("ZCode bundle 需要 Node.js")
            return [self.node, self.cli]
        if suffix == ".py":  # deterministic test adapter
            return [sys.executable, self.cli]
        if suffix in (".cmd", ".bat"):
            raise ValueError("请指定 zcode.cjs 或原生可执行文件，不通过 shell 执行 .cmd/.bat")
        return [self.cli]

    def available(self):
        if not (Path(self.cli).is_file() or shutil.which(self.cli)):
            return False, "找不到 ZCode CLI；设置 DAG_AGENT_ZCODE_BIN"
        try:
            cmd = self._command()
            if not self.provider_config or not Path(self.provider_config).is_file():
                return False, "需要独立模型配置文件：DAG_AGENT_ZCODE_PROVIDER_CONFIG（不自动读取桌面登录）"
            # Validate format without exposing any values or launching a session.
            cfg = json.loads(Path(self.provider_config).read_text(encoding="utf-8-sig"))
            selection = cfg.get("config", {}).get("defaultModelSelection", {})
            if not selection.get("providerId") or not selection.get("modelId"):
                return False, "模型配置缺少 config.defaultModelSelection 的 providerId/modelId"
            res = subprocess.run(cmd + ["--version"], capture_output=True, text=True,
                                 encoding="utf8", errors="replace", timeout=15)
            if res.returncode:
                return False, "ZCode --version 执行失败"
            self.backend_version = res.stdout.strip()[:80]
            return True, "CLI与配置可读取（未验证模型认证或额度）"
        except Exception as e:
            return False, f"ZCode配置或可执行文件不可用：{type(e).__name__}"

    def run(self, statement, workdir, check_cmd, title="", cancel_event=None):
        started = _utc_now_iso()
        t0 = time.monotonic()
        workdir = Path(workdir).resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        resources = {"backend": self.name, "origin": self.name,
                     "backend_version": self.backend_version,
                     "executor_started_at": started, "cost_currency": "USD",
                     "cost": {"amount": None, "basis": "unknown", "currency": "USD"},
                     "receipt_status": "incomplete"}
        out, err = _Tail(), _Tail()
        proc = None
        secrets = []
        payload = {}
        failure = ""
        cancelled = timed_out = False
        try:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                failure = "执行请求在启动前已取消"
            else:
                if not self.provider_config:
                    raise ValueError("缺少独立模型配置路径")
                cfg = json.loads(Path(self.provider_config).read_text(encoding="utf-8-sig"))
                secrets = _secrets(cfg)
                selection = cfg.get("config", {}).get("defaultModelSelection", {})
                resources["provider_model"] = str(selection.get("modelId") or "unknown")
                # Sensitive config lives outside the artifact workspace and is removed.
                with tempfile.TemporaryDirectory(prefix="dag-zcode-") as td:
                    home = Path(td)
                    profile = home / ".zcode/v2/provider_config.json"
                    profile.parent.mkdir(parents=True)
                    profile.write_text(json.dumps(cfg), encoding="utf8")
                    runtime = home / ".zcode/cli/config.json"
                    runtime.parent.mkdir(parents=True)
                    runtime.write_text(json.dumps({
                        "features": {"mcp": False, "subagent": False, "memory": False, "skill": False},
                        "memory": {"use": False}, "plugins": {"enabled": False},
                        "skills": {"enabled": False}, "hooks": {"enabled": False}}), encoding="utf8")
                    env = {k: v for k, v in os.environ.items() if k.lower() in
                           {"path", "systemroot", "windir", "comspec", "pathext", "temp", "tmp"}}
                    env.update(HOME=td, USERPROFILE=td, ZCODE_DATA_BASE_DIR=td,
                               ZCODE_STORAGE_DIR=str(home / ".zcode"),
                               ZCODE_PERSONAL_PROVIDER_CONFIG_FILE=str(profile),
                               PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
                    bundled = Path(self.cli).parent.parent / "config/provider/zcode-builtin.json"
                    if bundled.is_file():
                        env["ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"] = str(bundled)
                    args = self._command() + ["--prompt", build_prompt(statement, check_cmd, title),
                        "--cwd", str(workdir), "--mode", self.permission_mode, "--output-format", "json",
                        "--disallowed-tools", "AskUserQuestion EnterPlanMode ExitPlanMode",
                        "--workflow-mode", "disabled", "--no-color"]
                    proc = subprocess.Popen(args, cwd=workdir, env=env, shell=False,
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, encoding="utf8", errors="replace", start_new_session=os.name != "nt")
                    threads = [threading.Thread(target=s.pump, args=(stream,), daemon=True)
                               for s, stream in [(out, proc.stdout), (err, proc.stderr)]]
                    for th in threads:
                        th.start()
                    try:
                        while proc.poll() is None:
                            if cancel_event is not None and cancel_event.is_set():
                                cancelled = True
                                break
                            if time.monotonic() - t0 >= self.timeout:
                                timed_out = True
                                break
                            time.sleep(.05)
                    finally:
                        if proc.poll() is None:
                            _stop(proc)
                        for th in threads:
                            th.join(timeout=3)
                    payload = _payload(out.text)
        except Exception as e:
            failure = f"ZCode执行失败：{type(e).__name__}"  # don't leak config content
            if proc is not None and proc.poll() is None:
                try:
                    _stop(proc)
                except Exception:
                    failure += "；进程停止未确认"
        sid = payload.get("sessionId")
        response = payload.get("response")
        status = (payload.get("projection") or {}).get("status") if isinstance(payload.get("projection"), dict) else None
        complete = isinstance(sid, str) and bool(sid) and isinstance(response, str) and bool(response.strip())
        ok = bool(proc is not None and proc.returncode == 0 and complete and not failure
                  and not cancelled and not timed_out and status not in {"error", "failed", "cancelled", "interrupted"})
        resources.update(executor_ended_at=_utc_now_iso(), duration_ms=int((time.monotonic()-t0)*1000),
                         exit_code=proc.returncode if proc else None, cancelled=cancelled,
                         timeout=timed_out, session_id=sid if isinstance(sid, str) else "",
                         output_truncated=out.truncated or err.truncated)
        if complete:
            resources["agent_id"] = sid
        usage = payload.get("usage")
        if isinstance(usage, dict):
            # Never infer token counts from context size, text length or estimates.
            total = usage.get("totalTokens")
            if isinstance(total, (int, float)) and not isinstance(total, bool) and math.isfinite(total) and total >= 0 and total == int(total):
                resources["tokens"] = int(total)
        if ok and "tokens" in resources:
            resources["receipt_status"] = "valid"
        summary = failure or ("执行已取消" if cancelled else "执行超时" if timed_out else
                              response if isinstance(response, str) and response else "CLI未返回完整JSON结果")
        def redact(text):
            for secret in secrets:
                text = text.replace(secret, "[REDACTED]")
            return text
        logs = workdir / ".dag_session_logs"
        logs.mkdir(exist_ok=True)
        (logs / "zcode-stdout.log").write_text(redact(out.text), encoding="utf8")
        (logs / "zcode-stderr.log").write_text(redact(err.text), encoding="utf8")
        resources["logs"] = {"dir": str(logs), "stdout": "zcode-stdout.log", "stderr": "zcode-stderr.log"}
        return {"ok": ok, "summary": redact(summary)[:500], "session_id": resources["session_id"], "resources": resources}
