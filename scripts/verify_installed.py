# -*- coding: utf-8 -*-
"""在全新虚拟环境安装 wheel/sdist，并执行真实公开命令的零模型验收。"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import anyio

MCP_ROOT_CHECK = (
    "python -c \"import pathlib,sys; "
    "sys.exit(0 if pathlib.Path('report.txt').is_file() else 1)\"")
MCP_LEAF_CHECK = (
    "python -c \"import pathlib,sys; "
    "sys.exit(0 if pathlib.Path('leaf.txt').is_file() else 1)\"")


def _run(cmd: list[str], cwd: Path, log_dir: Path, name: str,
         env: dict[str, str] | None = None, timeout: int = 300) -> dict:
    proc = subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    (log_dir / f"{name}.stdout.log").write_text(
        proc.stdout, encoding="utf-8")
    (log_dir / f"{name}.stderr.log").write_text(
        proc.stderr, encoding="utf-8")
    if proc.returncode != 0:
        raise RuntimeError(
            f"{name} 退出码 {proc.returncode}；日志：{log_dir / (name + '.stderr.log')}")
    return {"command": cmd[0], "args": cmd[1:], "returncode": proc.returncode}


def _entry(venv: Path, name: str) -> Path:
    directory = venv / ("Scripts" if os.name == "nt" else "bin")
    candidates = [directory / f"{name}.exe", directory / name]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError(f"安装后缺少命令：{name}")


def _tool_json(result) -> dict:
    if getattr(result, "isError", False):
        raise RuntimeError(f"MCP 工具返回错误：{result.content}")
    return json.loads(result.content[0].text)


def _check_mcp(executable: Path, home: Path, cwd: Path,
               base_env: dict[str, str]) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = dict(base_env)
    env["DAG_AGENT_V3_HOME"] = str(home)
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    async def drive():
        with anyio.fail_after(90):
            params = StdioServerParameters(
                command=str(executable), args=[], env=env, cwd=str(cwd))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    names = {tool.name for tool in tools.tools}
                    required = {
                        "dag3_create_mission", "dag3_add_node", "dag3_status",
                        "dag3_board", "dag3_run_start", "dag3_run_stop",
                    }
                    if not required <= names:
                        raise RuntimeError(f"MCP 工具缺失：{sorted(required - names)}")
                    created = _tool_json(await session.call_tool(
                        "dag3_create_mission", {
                            "name": "Installed MCP acceptance",
                            "brief": "public package smoke",
                            "root_check": MCP_ROOT_CHECK,
                            "root_artifacts": ["report.txt"],
                        }))
                    if not created.get("ok"):
                        raise RuntimeError(f"MCP 创建失败：{created}")
                    mid = created["mission_id"]
                    added = _tool_json(await session.call_tool(
                        "dag3_add_node", {
                            "mid": mid, "title": "leaf",
                            "statement": "installed package fixture",
                            "parent": created["root"],
                            "check_cmd": MCP_LEAF_CHECK,
                            "artifacts": ["leaf.txt"],
                        }))
                    if not added.get("ok"):
                        raise RuntimeError(f"MCP 加节点失败：{added}")
                    status = _tool_json(await session.call_tool(
                        "dag3_status", {"mid": mid}))
                    if not status.get("ok") or len(status.get("nodes", [])) != 2:
                        raise RuntimeError(f"MCP 状态异常：{status}")
                    board = _tool_json(await session.call_tool(
                        "dag3_board", {"mid": mid}))
                    overview = Path(board["overview"]).resolve()
                    if not overview.is_file():
                        raise RuntimeError(f"MCP 看板不存在：{overview}")
                    overview.relative_to(home.resolve())
                    return {
                        "tool_count": len(names), "mission_id": mid,
                        "overview": str(overview),
                    }

    return anyio.run(drive)


def verify_installed(artifact: Path, work_dir: Path) -> dict:
    artifact = Path(artifact).resolve()
    work_dir = Path(work_dir).resolve()
    if not artifact.is_file():
        raise ValueError(f"制品不存在：{artifact}")
    sdist_source_commit = None
    if artifact.name.endswith(".tar.gz"):
        with tarfile.open(artifact, "r:gz") as archive:
            manifests = [
                member for member in archive.getmembers()
                if member.isfile() and member.name.endswith("/RELEASE_MANIFEST.json")
            ]
            if len(manifests) != 1:
                raise RuntimeError("sdist 必须且只能包含一份 RELEASE_MANIFEST.json")
            raw = archive.extractfile(manifests[0]).read()
            sdist_manifest = json.loads(raw.decode("utf-8"))
            sdist_source_commit = sdist_manifest.get("source_commit")
            if not isinstance(sdist_source_commit, str) or len(sdist_source_commit) != 40:
                raise RuntimeError("sdist 内清单缺少完整 source_commit")
    if work_dir.exists() and any(work_dir.iterdir()):
        raise ValueError(f"验收目录必须为空或不存在：{work_dir}")
    venv = work_dir / "venv"
    logs = work_dir / "logs"
    cwd = work_dir / "outside-source-cwd"
    logs.mkdir(parents=True, exist_ok=True)
    cwd.mkdir(parents=True, exist_ok=True)
    steps = []

    steps.append(_run([sys.executable, "-m", "venv", str(venv)], cwd, logs, "venv"))
    python = _entry(venv, "python")
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["VIRTUAL_ENV"] = str(venv)
    env["PATH"] = str(python.parent) + os.pathsep + env.get("PATH", "")
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    steps.append(_run([str(python), "-m", "pip", "install", str(artifact)],
                      cwd, logs, "install", env=env, timeout=600))
    steps.append(_run([str(python), "-m", "pip", "check"],
                      cwd, logs, "pip-check", env=env))

    v1_home = work_dir / "v1-home"
    v1 = _entry(venv, "dag-agent")
    steps.append(_run([str(v1), "--help"], cwd, logs, "v1-help", env=env))
    steps.append(_run([
        str(v1), "--home", str(v1_home), "create", "demo", "Demo",
        "Brief", "Root", "Statement",
    ], cwd, logs, "v1-create", env=env))
    steps.append(_run([str(v1), "--home", str(v1_home), "status", "demo"],
                      cwd, logs, "v1-status", env=env))
    if not (v1_home / "demo" / "mission.json").is_file():
        raise RuntimeError("v1 实际命令未创建预期 mission.json")

    full50 = _entry(venv, "dag-agent-full50")
    run_out = work_dir / "full50-output"
    steps.append(_run([
        str(full50), "--mode", "zero-model", "--output", str(run_out),
    ], cwd, logs, "full50-zero-model", env=env, timeout=600))
    result = json.loads((run_out / "run_results.json").read_text(encoding="utf-8"))
    if not (
        result.get("success") is True
        and result.get("node_count") == 50
        and result.get("applied_splits") == 12
        and result.get("mission_status") == "done"
        and result.get("checks") and all(result["checks"].values())
    ):
        raise RuntimeError(f"安装后 full50 验收失败：{result}")

    mcp = _entry(venv, "dag-agent-mcp")
    mcp_result = _check_mcp(mcp, work_dir / "v3-home", cwd, env)
    runtime_artifacts = [
        str(p.relative_to(venv)) for p in venv.rglob("*")
        if p.is_file() and (
            p.name in {"mission.db", "mission.json", "run_results.json", ".run.lock"}
            or "board" in p.parts
        )
    ]
    if runtime_artifacts:
        raise RuntimeError(f"运行数据写入安装环境：{runtime_artifacts}")

    return {
        "ok": True,
        "artifact": str(artifact),
        "artifact_bytes": artifact.stat().st_size,
        "sdist_source_commit": sdist_source_commit,
        "python": sys.version.split()[0],
        "steps": steps,
        "full50": {
            "node_count": result["node_count"],
            "applied_splits": result["applied_splits"],
            "mission_status": result["mission_status"],
            "checks": result["checks"],
        },
        "mcp": mcp_result,
        "v1_mission": str(v1_home / "demo" / "mission.json"),
        "install_tree_runtime_artifacts": runtime_artifacts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = verify_installed(args.artifact, args.work_dir)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 1
    result_path = args.work_dir / "installed-acceptance.json"
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "ok": True, "artifact": result["artifact"],
        "full50": result["full50"]["node_count"],
        "mcp_tools": result["mcp"]["tool_count"],
        "results": str(result_path),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
