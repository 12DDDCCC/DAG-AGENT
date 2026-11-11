"""步骤3单元验收：执行后端（claude CLI × MiniMax）——假垫片，不打真 API。

真实闭环（真 key + 真 CLI）在 check_step3.py / zst 受控检查中行使。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from dag_agent.cc_backend import (ClaudeCliBackend, build_prompt, load_env_file,
                                  record_resources)

SHIM = str(Path(__file__).parent / "fake_claude_shim.py")
CHECKER = str(Path(__file__).resolve().parents[2] / "dag_agent" / "checkers"
              / "check_file_contains.py")


def _env_file(tmp_path, **vals):
    p = tmp_path / "env.local"
    lines = ["# 注释行", ""] + [f"{k}={v}" for k, v in vals.items()]
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def test_env_loader(tmp_path):
    p = _env_file(tmp_path, ANTHROPIC_BASE_URL="https://x", ANTHROPIC_AUTH_TOKEN="k1")
    env = load_env_file(p)
    assert env["ANTHROPIC_BASE_URL"] == "https://x"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "k1"
    assert load_env_file(tmp_path / "nope") == {}


def test_build_prompt_contract():
    p = build_prompt("做X", "python c.py", "节点甲")
    assert "做X" in p and "节点甲" in p and "python c.py" in p
    assert "不可修改验收标准" in p
    p2 = build_prompt("做Y", None, "节点乙")
    assert "fail-closed" in p2


def test_backend_fake_run_end_to_end(tmp_path, real_graph):
    """假垫片执行 → 交付文件落地 → 资源回填 → 真验收命令裁决通过 → 工件发布。"""
    envf = _env_file(tmp_path, ANTHROPIC_BASE_URL="https://fake",
                     ANTHROPIC_AUTH_TOKEN="dummy")
    be = ClaudeCliBackend(cli_path=SHIM, env_file=envf, timeout=60)
    ok_avail, why = be.available()
    assert ok_avail, why

    mid, root = real_graph.create_mission("步骤3闭环")
    a_id = real_graph.add_node(
        mid, "交付hello", parent_id=root,
        statement="【输入】无/【交付清单】hello.txt 内容 DAG-OK/【接口签名】文件交付/【样例】DAG-OK/【自测入口】cat hello.txt",
        check_cmd=f"python \"{CHECKER}\" hello.txt DAG-OK")
    a = real_graph.repo.get_node(a_id)
    aid = real_graph.start_attempt(mid, a_id, backend=be.name)
    workdir = Path(real_graph.repo.get_attempt(aid).workdir)

    run1 = be.run(a.statement, workdir, a.check_cmd, a.title)
    assert run1["ok"] is True and run1["session_id"] == "fake-sess-0001"
    assert (workdir / "hello.txt").read_text(encoding="utf-8") == "DAG-OK"
    # 契约 v2：费用结构化（多模型分项求和口径），不再取 modelUsage 首项
    assert run1["resources"]["cost"] == {"amount": 0.01, "basis": "reported",
                                         "currency": "USD"}
    assert run1["resources"]["usage_raw"] == {"input_tokens": 10,
                                              "output_tokens": 5}
    assert run1["resources"]["receipt_status"] == "valid"
    record_resources(real_graph.repo, aid, run1)
    att = real_graph.repo.get_attempt(aid)
    assert json.loads(att.resources_json)["session_id"] == "fake-sess-0001"

    result = real_graph.submit_attempt(mid, a_id, aid, artifact_paths=["hello.txt"])
    assert result["verdict"] == "pass"
    assert real_graph.repo.get_node(a_id).status == "accepted"
    assert (real_graph.store.accepted_root / a_id / "v1" / "hello.txt").is_file()


def test_backend_timeout(tmp_path):
    envf = _env_file(tmp_path, ANTHROPIC_BASE_URL="https://x",
                     ANTHROPIC_AUTH_TOKEN="k")
    be = ClaudeCliBackend(cli_path=SHIM, env_file=envf, timeout=2)
    os.environ["FAKE_MODE"] = "sleep"
    try:
        out = be.run("任何事", tmp_path / "wd_timeout", None, "超时节点")
    finally:
        os.environ.pop("FAKE_MODE", None)
    assert out["ok"] is False and out["resources"].get("timeout") is True


def test_backend_garbage_and_error(tmp_path):
    envf = _env_file(tmp_path, ANTHROPIC_BASE_URL="https://x",
                     ANTHROPIC_AUTH_TOKEN="k")
    wd = tmp_path / "wd_g"
    bad = ClaudeCliBackend(cli_path=SHIM, env_file=envf, timeout=30)
    os.environ["FAKE_MODE"] = "garbage"
    try:
        out = bad.run("x", wd, None, "g")
        assert out["ok"] is False  # 不可解析输出=失败证据
        os.environ["FAKE_MODE"] = "error"
        out2 = bad.run("x", wd, None, "e")
        assert out2["ok"] is False
    finally:
        os.environ.pop("FAKE_MODE", None)


def test_available_missing_env(tmp_path):
    be = ClaudeCliBackend(cli_path=SHIM, env_file=tmp_path / "none")
    ok, why = be.available()
    assert ok is False and "缺少环境变量" in why
