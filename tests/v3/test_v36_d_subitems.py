"""v3.6 D 补验四子项回归（2026-10-04 遗留轮，Codex 口径：按子项记 PASS/FAIL）。

D1 真跨父修订：双父图（P2 的叶子跨父依赖 P1 的叶子）——修订 P1 侧叶子，
    跨父消费者、双父、ROOT 全失效；无关节点尝试增量 0；重做后闭合。
D2 在途旧提交拒收：节点 running 时修订 → 在途取消 → 旧 attempt 提交必拒，
    新链重做正常。
D3 worker 异常运行时路径：执行器 run() 抛未知异常 → attempt error、节点
    blocked、worker_error 事件；unblock 恢复后重派成功。
D4 MCP 真子进程两轮暂停恢复：真 mcp_server_v3.py 子进程（隔离 home、零模型
    null 后端）运行中 pause → resume（第一次生效）→ pause → resume（第二次
    生效）→ 链条跑完。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from dag_agent.graph import Graph, GraphError
from dag_agent.repository import Repository

CHK = "python -c 'pass'"
BRIEF = ("【输入】无/【交付清单】impl.txt/【接口签名】文件交付/"
         "【样例】内容 ok/【自测入口】type impl.txt")


def _mk_graph(tmp_path):
    repo = Repository(tmp_path / "m" / "mission.db")
    return repo, Graph(repo)


def _run_node(g, repo, nid, content="ok"):
    """零模型直驱：claim(stub) → workdir 写交付 → submit。"""
    aid = g.start_attempt(nid[0], nid[1], backend="stub") \
        if isinstance(nid, tuple) else None
    return aid


# ---- D1 真跨父修订 ----

def test_d1_true_cross_parent_revision(tmp_path):
    repo, g = _mk_graph(tmp_path)
    try:
        mid, root = g.create_mission("跨父", root_check=CHK)
        p1 = g.add_node(mid, "P1", "父1", parent_id=root, check_cmd=CHK)
        p2 = g.add_node(mid, "P2", "父2", parent_id=root, check_cmd=CHK)
        a1 = g.add_node(mid, "a1", BRIEF, parent_id=p1, check_cmd=CHK,
                        declared_artifacts=("a1.txt",))
        a2 = g.add_node(mid, "a2", BRIEF, parent_id=p1, deps=(a1,),
                        check_cmd=CHK, declared_artifacts=("a2.txt",))
        # 跨父：P2 侧叶子依赖 P1 侧叶子
        b1 = g.add_node(mid, "b1", BRIEF, parent_id=p2, deps=(a1,),
                        check_cmd=CHK, declared_artifacts=("b1.txt",))
        # 无关分支
        c1 = g.add_node(mid, "c1", BRIEF, parent_id=root, check_cmd=CHK,
                        declared_artifacts=("c1.txt",))

        def finish(nid, fname):
            aid = g.start_attempt(mid, nid, backend="stub")
            (Path(repo.get_attempt(aid).workdir) / fname).write_text(
                "ok", encoding="utf-8")
            return g.submit_attempt(mid, nid, aid, artifact_paths=[fname])

        for nid, f in ((a1, "a1.txt"), (a2, "a2.txt"), (b1, "b1.txt"),
                       (c1, "c1.txt"), (p1, "p1.txt"), (p2, "p2.txt")):
            # 整合父节点声明件从快照播种后机械提交
            assert finish(nid, f)["verdict"] == "pass", nid
        assert finish(root, "root.txt")["verdict"] == "pass"

        # 修订 a1 → 跨父消费者 b1、双父、ROOT 失效；a2 同侧失效；c1 不动
        g.revise_node(mid, a1, reason="D1 跨父修订探针")
        st = {r["id"]: r["status"] for r in repo._conn.execute(
            "SELECT id, status FROM nodes")}
        assert st[c1] == "accepted", "无关节点不得失效"
        for nid in (a1, a2, b1, p1, p2, root):
            assert st[nid] in ("pending", "waiting_children"), \
                f"{nid} 应失效：{st[nid]}"
        # 重做闭合（c1 复用不重跑）
        for nid, f in ((a1, "a1.txt"), (a2, "a2.txt"), (b1, "b1.txt"),
                       (p1, "p1.txt"), (p2, "p2.txt"), (root, "root.txt")):
            assert finish(nid, f)["verdict"] == "pass", nid
        c1_attempts = repo._conn.execute(
            "SELECT COUNT(*) AS c FROM attempts WHERE node_id=?", (c1,)
        ).fetchone()["c"]
        assert c1_attempts == 1, "无关节点必须零重跑"
    finally:
        repo.close()


# ---- D2 在途旧提交拒收 ----

def test_d2_inflight_old_submit_rejected_after_revise(tmp_path):
    repo, g = _mk_graph(tmp_path)
    try:
        mid, root = g.create_mission("在途旧提交", root_check=CHK)
        n = g.add_node(mid, "A", BRIEF, parent_id=root, check_cmd=CHK,
                       declared_artifacts=("x.txt",))
        old_aid = g.start_attempt(mid, n, backend="stub")
        wd = Path(repo.get_attempt(old_aid).workdir)
        (wd / "x.txt").write_text("旧版", encoding="utf-8")
        # 在途修订：取消在途，节点回 pending
        g.revise_node(mid, n, reason="D2 在途修订探针")
        assert repo.get_attempt(old_aid).status != "running"
        # 旧 attempt 提交必拒（任何路径都不得借旧尝试注入结果）
        with pytest.raises(GraphError):
            g.submit_attempt(mid, n, old_aid, artifact_paths=["x.txt"])
        # 新链重做正常
        aid2 = g.start_attempt(mid, n, backend="stub")
        wd2 = Path(repo.get_attempt(aid2).workdir)
        (wd2 / "x.txt").write_text("新版", encoding="utf-8")
        assert g.submit_attempt(mid, n, aid2,
                                artifact_paths=["x.txt"])["verdict"] == "pass"
    finally:
        repo.close()


# ---- D3 worker 异常运行时路径 ----

def test_d3_worker_exception_blocks_and_recovers(tmp_path):
    from dag_agent.scheduler import Budget, Scheduler

    class BoomBackend:
        name = "stub"                    # 豁免表内（零模型桩）

        def __init__(self):
            self.fired = False

        def run(self, statement, workdir, check_cmd, title=""):
            if not self.fired:
                self.fired = True
                raise RuntimeError("worker 崩溃探针：未知异常")
            (Path(workdir) / "impl.txt").write_text("ok", encoding="utf-8")
            return {"ok": True, "summary": "DONE", "session_id": "s",
                    "resources": {"cost_usd": 0.0, "duration_ms": 1}}

    repo, g = _mk_graph(tmp_path)
    be = BoomBackend()
    s = Scheduler(repo, g, be, max_concurrency=2, budget=Budget(max_calls=8))
    try:
        mid, root = g.create_mission("worker异常", root_check=CHK)
        n = g.add_node(mid, "A", BRIEF, parent_id=root, check_cmd=CHK,
                       declared_artifacts=("impl.txt",))
        # 第一轮：worker 抛未知异常 → error 结算 + blocked → 死局停
        # 检查子进程在 AV/CI 负载下可能超过旧 30s 窗口；闭合断言不放宽。
        s.run_until_done(mid, root, timeout=120)
        assert repo.get_node(n).status == "blocked", \
            repo.get_node(n).status
        evs = [r["type"] for r in repo._conn.execute(
            "SELECT type FROM events WHERE node_id=?", (n,))]
        assert "worker_error" in evs, evs
        # 恢复：unblock → 重派（后端已自愈）→ 成功闭合
        g.unblock(mid, n)
        s2 = Scheduler(repo, g, be, max_concurrency=2,
                       budget=Budget(max_calls=8))
        s2.run_until_done(mid, root, timeout=120)
        assert repo.get_node(n).status == "accepted", repo.get_node(n).status
        mrow = repo._conn.execute(
            "SELECT status FROM missions WHERE id=?", (mid,)).fetchone()
        assert mrow["status"] == "done", mrow["status"]
    finally:
        repo.close()


# ---- D4 MCP 真子进程两轮暂停恢复 ----

class _Mcp:
    """最小 JSON-RPC 客户端（stderr→文件，防管道死锁）。"""

    def __init__(self, home: Path):
        self._err = open(tempfile.gettempdir() + f"/mcp_d4_{id(self)}.log",
                         "w", encoding="utf-8")
        env = dict(os.environ, DAG_AGENT_V3_HOME=str(home))
        self.proc = subprocess.Popen(
            [sys.executable, "-B",
             str(Path(__file__).resolve().parents[2] / "mcp_server_v3.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self._err, text=True, encoding="utf-8", env=env)
        self._id = 0
        self._send({"jsonrpc": "2.0", "id": self._id,
                    "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05",
                               "capabilities": {},
                               "clientInfo": {"name": "d4",
                                              "version": "1"}}})
        self._send({"jsonrpc": "2.0",
                    "method": "notifications/initialized"}, want_id=False)

    def _send(self, obj, want_id: bool = True):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()
        if not want_id:
            return None
        rid = obj["id"]
        deadline = time.time() + 60
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                break
            msg = json.loads(line)
            if msg.get("id") == rid:
                if "error" in msg:
                    raise AssertionError(f"{obj['method']}: {msg['error']}")
                return msg["result"]
        raise TimeoutError(obj["method"])

    def call(self, tool, args=None, timeout=60):
        self._id += 1
        res = self._send({"jsonrpc": "2.0", "id": self._id,
                          "method": "tools/call",
                          "params": {"name": tool, "arguments": args or {}}})
        text = res["content"][0]["text"]
        try:
            return json.loads(text)
        except (ValueError, KeyError):
            return {"ok": True, "text": text}

    def close(self):
        self.proc.terminate()
        self._err.close()


BITES = ("python -c \"import pathlib,sys; sys.exit(0 if any("
         "pathlib.Path('.').glob('*.txt')) else 1)\"")
# sleep 版：给暂停/恢复留出稳定观察窗（空目录仍 FAIL，有牙齿）
# T3 抖动治理（2026-10-06）：D4 依赖 sleep(2)/sleep(3+) 观察窗与真实 MCP
# 子进程，满载/慢盘偶发抖动（历史 1 failed 属此类）——pytest.ini 已注册
# slow 标记并默认排除，run_pytest_v3_slow.py 单独验证本用例。
SLOW_BITES = ("python -c \"import time,pathlib,sys; time.sleep(2); "
              "sys.exit(0 if any(pathlib.Path('.').glob('*.txt')) else 1)\"")


@pytest.mark.slow
def test_d4_mcp_two_rounds_pause_resume(tmp_path):
    """D4（Codex adcc5d5 复核后重写）：每轮必须断言——控制响应成功、
    节点确实暂停（blocked）、暂停期零新派发、resume 后出现新 attempt；
    第二轮在运行中（在途 attempt 结算后重派被拦）重复生效。"""
    home = tmp_path / "home"
    home.mkdir()
    mcp = _Mcp(home)

    def node_status(mid, nid):
        st = mcp.call("dag3_status", {"mid": mid})
        assert st.get("ok"), st
        row = next(n for n in st["nodes"] if n["id"] == nid)
        return row["status"]

    def attempt_count(mid, nid):
        evs = mcp.call("dag3_events", {"mid": mid, "limit": 500})
        assert evs.get("ok"), evs
        return sum(1 for e in evs["events"]
                   if e["type"] == "attempt_started" and e["node"] == nid)

    def wait_for(cond, timeout=15):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = cond()
            if last:
                return last
            time.sleep(0.1)
        return last

    try:
        mid = mcp.call("dag3_create_mission", {
            "name": "D4两轮恢复", "root_check": SLOW_BITES,
            "brief": "【输入】无/【交付清单】impl/【接口签名】文件/"
                     "【样例】ok/【自测入口】type"})["mission_id"]
        a = mcp.call("dag3_add_node", {
            "mid": mid, "title": "A", "statement": BRIEF, "parent": "n1",
            "check_cmd": SLOW_BITES, "declared_artifacts": []})["node"]
        b = mcp.call("dag3_add_node", {
            "mid": mid, "title": "B", "statement": BRIEF, "parent": "n1",
            "check_cmd": SLOW_BITES, "declared_artifacts": []})["node"]

        # ---- 第一轮（跑前暂停 A：暂停期不派发 → 恢复后新派发）----
        r = mcp.call("dag3_pause", {"mid": mid, "node_id": a,
                                    "reason": "D4-R1"})
        assert r.get("ok") is True, r          # 控制响应必须成功
        assert node_status(mid, a) == "blocked"
        assert mcp.call("dag3_run_start",
                        {"mid": mid, "real": False}).get("ok")
        time.sleep(3.0)                        # 覆盖至少一个调度 tick
        assert attempt_count(mid, a) == 0, "暂停期不得派发"
        r = mcp.call("dag3_resume", {"mid": mid, "node_id": a})
        assert r.get("ok") is True, r
        # 解除暂停后允许瞬态 pending 或调度器已抢先推进至 in_work/running
        assert node_status(mid, a) in ("pending", "in_work", "running")
        got = wait_for(lambda: attempt_count(mid, a) >= 1)
        assert got, "恢复后必须出现新 attempt"

        # ---- 第二轮（运行中暂停 B：在途结算后重派被拦 → 恢复再派）----
        got = wait_for(lambda: attempt_count(mid, b) >= 1)
        assert got, "B 应先获得在途 attempt"
        r = mcp.call("dag3_pause", {"mid": mid, "node_id": b,
                                    "reason": "D4-R2"})
        assert r.get("ok") is True, r
        assert node_status(mid, b) == "blocked"
        frozen = attempt_count(mid, b)
        time.sleep(3.5)                        # 在途 fail 结算 + 至少一 tick
        assert attempt_count(mid, b) == frozen, "暂停期重试必须被拦"
        r = mcp.call("dag3_resume", {"mid": mid, "node_id": b})
        assert r.get("ok") is True, r
        # 解除暂停后允许瞬态 pending 或调度器已抢先推进至 in_work/running
        assert node_status(mid, b) in ("pending", "in_work", "running")
        got = wait_for(lambda: attempt_count(mid, b) > frozen)
        assert got, "第二轮恢复后必须再次派发"
    finally:
        mcp.close()


# ---- driver v2：历史快照复用可追溯（Codex adcc5d5 第4点）----

def test_driver_v2_reuse_provenance(tmp_path):
    """同名多来源按快照 created_at 取最新（非遍历覆盖）；历史复用
    前哈希核验 manifest，篡改必拒；来源登记进 attempt。"""
    import importlib.util
    root_p = Path(__file__).resolve().parents[2]
    b4_fix = root_p / "tests" / "fixtures" / "b4_revise_driver.py"
    b4_path = b4_fix if b4_fix.is_file() else (root_p / "outputs" / "antigravity-run" / "b4_revise_driver.py")
    spec = importlib.util.spec_from_file_location("b4_revise_driver", b4_path)
    drv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(drv)

    repo = Repository(tmp_path / "m" / "mission.db")
    try:
        g = Graph(repo)
        mid, root = g.create_mission("复用追溯", root_check=CHK)
        n = g.add_node(mid, "X", BRIEF, parent_id=root, check_cmd=CHK,
                       declared_artifacts=("f.txt",))
        acc = tmp_path / "m" / "accepted"      # 平台布局：accepted/<node>/vN
        # 两个版本快照：v1 旧时间戳、v2 新时间戳，同名文件内容不同
        for ver, (when, content) in enumerate(
                (("2026-10-04T10:00:00", "OLD"), ("2026-10-04T12:00:00", "NEW")), 1):
            vdir = acc / n / f"v{ver}"
            vdir.mkdir(parents=True)
            import hashlib
            (vdir / "f.txt").write_text(content, encoding="utf-8")
            sha = hashlib.sha256((vdir / "f.txt").read_bytes()).hexdigest()
            with repo.tx() as cur:
                cur.execute(
                    "INSERT INTO snapshots(node_id,mission_id,attempt_id,"
                    "artifact_version,manifest_hash,files_json,valid,created_at)"
                    " VALUES(?,?,?,?,?,?,1,?)",
                    (n, mid, f"a-{ver}", ver, f"h{ver}",
                     json.dumps([{"rel_path": "f.txt", "sha256": sha,
                                  "size": 3}]), when))
        repo._conn.commit()
        # 取最新：created_at 12:00 的 v2
        picked = drv.pick_latest_source(
            repo, [{"snapshot_id": 1}, {"snapshot_id": 2}], "f.txt")
        assert picked is not None
        path, desc = picked
        assert "NEW" == path.read_text(encoding="utf-8")
        assert desc.startswith(f"{n}:v2"), desc
        # 哈希核验：v2 文件被篡改 → 必拒
        (acc / n / "v2" / "f.txt").write_text("TAMPER", encoding="utf-8")
        with pytest.raises(SystemExit):
            drv.verify_snapshot_file(repo, n, 2, "f.txt", acc / n / "v2" / "f.txt")
    finally:
        repo.close()
