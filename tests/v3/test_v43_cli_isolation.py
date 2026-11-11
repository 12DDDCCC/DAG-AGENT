# -*- coding: utf-8 -*-
"""v3.12 阶段 1C 回归：CLI 原生工具/自测权限/配置隔离。

任务书 1C 判据：
- 执行者可读写工作目录并运行任务指定自测（默认 allowedTools 含 Bash——
  非交互 -p 模式无人工批准通道）；
- allowedTools（自动批准）与 --tools（可用集合）分开配置，不当 OS 沙箱；
- 节点会话不继承宿主 ~/.claude（CLAUDE_CONFIG_DIR 指向工作目录隔离区）；
  不修改用户全局设置；--strict-mcp-config 忽略宿主 MCP；
- 正式考卷在交付区外由平台冻结（既有 R1 check_digest 机制，此处核验
  防线存在）。
"""
from __future__ import annotations

from pathlib import Path

from dag_agent.cc_backend import ClaudeCliBackend
from dag_agent.graph import Graph
from dag_agent.repository import Repository

SHIM = str(Path(__file__).parent / "fake_claude_shim.py")


def _backend(tmp_path, **kw):
    envf = tmp_path / "env.local"
    envf.write_text("ANTHROPIC_BASE_URL=https://fake\n"
                    "ANTHROPIC_AUTH_TOKEN=d\n", encoding="utf-8")
    return ClaudeCliBackend(cli_path=SHIM, env_file=envf, timeout=60, **kw)


def test_default_tools_allow_selftest(tmp_path):
    """默认自动批准集含 Bash（自测入口=跑命令）；Write/Edit/Read 保留。"""
    be = _backend(tmp_path)
    tools = be.allowed_tools.split()
    assert "Bash" in tools and "Write" in tools and "Edit" in tools
    assert be.permission_mode == "acceptEdits"


def test_config_isolation_per_workdir(tmp_path):
    """CLAUDE_CONFIG_DIR 指向工作目录隔离区（每 attempt 独立），
    不指向宿主用户目录；关掉隔离开关时不设置。"""
    be = _backend(tmp_path)
    wd = tmp_path / "wd1"
    env, flags = be._prepare_env(wd)
    cfg = env.get("CLAUDE_CONFIG_DIR", "")
    assert cfg and Path(cfg) == wd / ".claude-home"
    assert Path(cfg).is_dir()
    assert "用户" not in cfg and ".claude" in Path(cfg).name
    # 隔离开关关闭：不设置 CLAUDE_CONFIG_DIR（继承调用方环境）
    be2 = _backend(tmp_path, isolate_config=False)
    env2, _ = be2._prepare_env(tmp_path / "wd2")
    assert "CLAUDE_CONFIG_DIR" not in env2


def test_strict_mcp_flag_present(tmp_path):
    """--strict-mcp-config 在 flags 中（忽略宿主 MCP 配置）；本机
    2.1.232 实测支持该参数（help 输出在案）。"""
    _, flags = _backend(tmp_path)._prepare_env(tmp_path / "wd3")
    assert "--strict-mcp-config" in flags
    assert "-p " in flags and "--output-format json" in flags


def test_exam_outside_deliverable_zone_guard_exists(tmp_path):
    """防线核验：考卷改动被 R1 check_digest 拦（认领后改考卷 → 提交拒）。
    考卷文件位于交付区之外（checks 目录），执行者改卷必被平台拒。"""
    exam = tmp_path / "exams" / "chk.py"
    exam.parent.mkdir()
    exam.write_text("import sys; sys.exit(0)\n", encoding="utf-8")
    repo = Repository(tmp_path / "m" / "mission.db")
    try:
        g = Graph(repo)
        mid, root = g.create_mission(
            "1C考卷防线", root_check=f'python "{exam}"',
            root_declared_artifacts=("o.txt",))
        nid = g.add_node(mid, "L", "【输入】无/【交付清单】o.txt/【接口签名】"
                         "文件/【样例】x/【自测入口】type o.txt",
                         parent_id=root,
                         check_cmd=f'python "{exam}"',
                         declared_artifacts=("o.txt",),
                         exam_files=(str(exam),))
        aid = g.start_attempt(mid, nid, backend="stub")
        # 执行者改卷（考卷在交付区外，但内容摘要已入认领指纹）
        exam.write_text("import sys; sys.exit(1)  # 篡改\n", encoding="utf-8")
        wd = Path(repo.get_attempt(aid).workdir)
        (wd / "o.txt").write_text("x", encoding="utf-8")
        import pytest
        from dag_agent.graph import GraphError
        with pytest.raises(GraphError, match="考卷内容已变更"):
            g.submit_attempt(mid, nid, aid, artifact_paths=["o.txt"])
    finally:
        repo.close()
