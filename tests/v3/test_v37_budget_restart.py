"""v3.7 G8 预算重启持久化回归（六执行者协作轮，2026-10-05）。

断言：调度器实例重建后 budget_json 恢复累计（calls_used/cost_used 不归零），
续跑在已有用量上继续计数，第二实例的熔断以累计值为准。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from dag_agent.graph import Graph
from dag_agent.repository import Repository
from dag_agent.scheduler import Budget, Scheduler

BITES = ("python -c \"import pathlib,sys; sys.exit(0 if any("
         "pathlib.Path('.').glob('*.txt')) else 1)\"")
BRIEF = ("【输入】无/【交付清单】impl.txt/【接口签名】文件交付/"
         "【样例】内容 ok/【自测入口】type impl.txt")


class TickBackend:
    name = "stub"

    def __init__(self):
        self.calls = 0

    def run(self, statement, workdir, check_cmd, title=""):
        self.calls += 1
        (Path(workdir) / "impl.txt").write_text("ok", encoding="utf-8")
        return {"ok": True, "summary": "DONE", "session_id": "t",
                "resources": {"cost_usd": 0.01, "duration_ms": 1}}


@pytest.mark.slow
def test_budget_survives_scheduler_restart(tmp_path):
    """T3 抖动治理（2026-10-06）：本用例依赖 sleep(0.3) 观察窗断言
    "预算未清零前不续派"，满载/慢盘上该窗口偶发不稳定（历史 1 failed
    抖动属此类）——标记 slow，主套件默认排除，run_pytest_v3_slow.py
    单独验证。"""
    repo = Repository(tmp_path / "m" / "mission.db")
    g = Graph(repo)
    be = TickBackend()
    try:
        mid, root = g.create_mission("预算重启", root_check=BITES,
                                     root_declared_artifacts=("impl.txt",))
        for i in range(3):
            g.add_node(mid, f"N{i}", BRIEF, parent_id=root,
                       check_cmd=BITES, declared_artifacts=("impl.txt",))
        # 第一实例：上限 2 次调用 → 熔断停
        s1 = Scheduler(repo, g, be, max_concurrency=1,
                       budget=Budget(max_calls=2))
        s1.run_until_done(mid, root, timeout=30)
        m = repo._conn.execute("SELECT status, budget_json FROM missions "
                               "WHERE id=?", (mid,)).fetchone()
        b1 = json.loads(m["budget_json"] or "{}")
        assert b1.get("calls_used") == 2, b1
        assert m["status"] == "active"

        # 第二实例（重启）：加载累计值，不得归零；上限 2 已耗尽 → 仍熔断
        be2 = TickBackend()
        s2 = Scheduler(repo, g, be2, max_concurrency=1,
                       budget=Budget(max_calls=2))
        s2.tick_dispatch(mid)
        time.sleep(0.3)
        assert be2.calls == 0, "累计 2/2 未清零就续派=预算归零"
        assert s2.halt_reason and "熔断（2/2）" in s2.halt_reason, \
            s2.halt_reason

        # 第三实例：放宽上限 → 续跑在累计之上（2→4，含 ROOT 整合）
        be3 = TickBackend()
        s3 = Scheduler(repo, g, be3, max_concurrency=1,
                       budget=Budget(max_calls=8))
        s3.run_until_done(mid, root, timeout=60)
        m3 = repo._conn.execute("SELECT status, budget_json FROM missions "
                                "WHERE id=?", (mid,)).fetchone()
        b3 = json.loads(m3["budget_json"] or "{}")
        assert b3.get("calls_used", 0) > 2, f"续跑未在累计之上：{b3}"
        assert m3["status"] == "done", m3["status"]
    finally:
        repo.close()
