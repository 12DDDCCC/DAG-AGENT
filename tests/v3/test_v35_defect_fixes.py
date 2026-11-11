"""v3.5 已确认缺陷修复回归（2026-10-04 遗留轮）。

缺陷1：projection.py 两处 write_text 直写点（总览/节点档案）——读者可见
短暂 0 字节；修复=统一走 _atomic_write（tmp+os.replace）。
缺陷2：assessment.py 白名单按 Path(name) 判定——Windows sys.executable
指向 python.exe 被误拒；修复=stem 判定（剥 .exe/.cmd），白名单外仍拒。
"""
from __future__ import annotations

import sys
from pathlib import Path

from dag_agent import projection
from dag_agent.assessment import run_check3


# ---- 缺陷2：白名单 stem 判定 ----

def test_whitelist_accepts_python_exe(tmp_path):
    """绝对路径 python.exe（sys.executable 形态）可执行考卷。"""
    cmd = f'"{sys.executable}" -c "import sys; sys.exit(0)"'
    verdict, _ev = run_check3(cmd, tmp_path)
    assert verdict == "pass", (verdict, _ev)


def test_whitelist_still_rejects_unknown_exe(tmp_path):
    """白名单外可执行（evil.exe / notepad.exe）仍被拒。"""
    for bad in ("evil.exe", "C:/Windows/system32/notepad.exe", "curl"):
        verdict, ev = run_check3(f'"{bad}" --version', tmp_path)
        assert verdict == "error" and "白名单" in ev, (bad, verdict, ev)


# ---- 缺陷1：投影全部文件经 _atomic_write ----

def test_projection_all_files_atomic(tmp_path, monkeypatch):
    """总览/节点档案不再直写：渲染产物全部经 _atomic_write 且非空。"""
    from dag_agent.graph import Graph
    from dag_agent.repository import Repository

    calls: list[str] = []

    orig = projection._atomic_write

    def spy(path: Path, text: str):
        calls.append(Path(path).name)
        orig(path, text)

    monkeypatch.setattr(projection, "_atomic_write", spy)

    repo = Repository(tmp_path / "m" / "mission.db")
    g = Graph(repo)
    try:
        mid, root = g.create_mission("投影原子", root_check="python -c 'pass'")
        g.add_node(mid, "A", "【输入】无/【交付清单】x/【接口签名】无/"
                    "【样例】无/【自测入口】无", parent_id=root,
                   check_cmd="python -c 'pass'")
        overview_file = projection.render_mission(repo, g, mid)
        board = overview_file.parent
        files = [f for f in board.rglob("*") if f.is_file()]
        assert files, "投影无产物"
        assert all(f.stat().st_size > 0 for f in files), \
            "出现 0 字节文件（直写窗口被捕获）"
        # 总览与节点档案都在原子写清单里
        assert "任务总览.md" in calls
        assert "当前任务图.canvas" in calls
    finally:
        repo.close()
