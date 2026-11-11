"""v3 步骤1 conftest：内存外临时库 + 可注入检查器。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dag_agent.graph import Graph  # noqa: E402
from dag_agent.repository import Repository  # noqa: E402


class StubChecker:
    """可编程检查器：按节点 id 返回 pass/fail，evidence 可断言。"""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, cmd, cwd):
        self.calls.append((cmd or "", str(cwd)))
        if not cmd:
            return False, "未配置验收命令：fail-closed"
        if "FAIL" in cmd:
            return False, f"stub fail: {cmd}"
        return True, f"stub pass: {cmd}"


@pytest.fixture
def checker():
    return StubChecker()


@pytest.fixture
def graph(tmp_path, checker):
    repo = Repository(tmp_path / "mission.db")
    g = Graph(repo, checker=checker)
    yield g
    repo.close()


@pytest.fixture
def real_graph(tmp_path):
    """默认检查器（assessment.run_check）：行使白名单/超时/零写入守卫真逻辑。"""
    repo = Repository(tmp_path / "mission.db")
    g = Graph(repo)
    yield g
    repo.close()
