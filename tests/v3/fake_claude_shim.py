"""假 claude CLI 垫片：模拟 --output-format json 的输出契约，不打真 API。

行为由环境变量 FAKE_MODE 控制：
- deliver（默认）：在 cwd 写 hello.txt（内容 DAG-OK）→ 输出成功 JSON
- sleep：睡 60s（测超时杀进程）
- slow_deliver：睡 3s 后正常交付（测 drain/取消不误杀在途）
- garbage：输出非 JSON 文本
- error：输出 is_error=true JSON
- multi：多模型 modelUsage（两键）+ 汇总一致 → 费用应取汇总 reported
- multi_nototal：多模型分项、缺汇总 → 费用应为分项求和 estimated
- multi_nonumeric：modelUsage 键缺 costUSD 数值 → 费用 unknown
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

mode = os.environ.get("FAKE_MODE", "deliver")

payload = {
    "is_error": mode == "error",
    "result": "DONE 假执行完成" if mode != "error" else "模拟失败",
    "session_id": "fake-sess-0001",
    "num_turns": 1,
    "total_cost_usd": 0.01,
    "usage": {"input_tokens": 10, "output_tokens": 5},
    "modelUsage": {"MiniMax-M3.1": {"costUSD": 0.01}},
}

if mode == "sleep":
    time.sleep(60)
    sys.exit(0)

if mode == "slow_deliver":
    time.sleep(3)
    (Path.cwd() / "hello.txt").write_text("DAG-OK", encoding="utf-8")
    print(json.dumps({**payload, "result": "DONE slow"}, ensure_ascii=False))
    sys.exit(0)

if mode == "garbage":
    print("这不是JSON输出")
    sys.exit(0)

if mode == "multi":
    payload["modelUsage"] = {"MiniMax-M3.1": {"costUSD": 0.02},
                             "MiniMax-M2": {"costUSD": 0.03}}
    payload["total_cost_usd"] = 0.05
elif mode == "multi_nototal":
    payload["modelUsage"] = {"MiniMax-M3.1": {"costUSD": 0.02},
                             "MiniMax-M2": {"costUSD": 0.03}}
    payload.pop("total_cost_usd")
elif mode == "multi_nonumeric":
    payload["modelUsage"] = {"MiniMax-M3.1": {}}
    payload.pop("total_cost_usd")

if mode == "deliver":
    (Path.cwd() / "hello.txt").write_text("DAG-OK", encoding="utf-8")
print(json.dumps(payload, ensure_ascii=False))
