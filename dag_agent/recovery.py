"""v3 步骤5：恢复层——暂停/取消/孤儿收割/重启恢复/失败自动重试/回执幂等。

对应任务清单第五步通过标准：中断可恢复，迟到结果不能覆盖，操作不盲目重复。
诚实边界：孤儿判定按"本机单服务"假设——服务启动时把所有 running 尝试视为
孤儿（多服务并发写同一任务不受支持，pid 存活检查仅作辅助）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from .repository import Repository, now


class RetryPolicy:
    """失败自动重试（v3.2 协议03§6）：只计产物失败（fail 类终态），
    error/cancel/orphan/reject 不耗额度；允许派发 ⇔ fail 数 ≤ max_retries
    （默认 2=首试+最多两次重试）。"""

    def __init__(self, max_retries: int = 2):
        self.max_retries = max_retries

    def allows(self, repo: Repository, nid: str) -> bool:
        n = repo._conn.execute(
            "SELECT COUNT(*) AS c FROM attempts WHERE node_id=?"
            " AND status='failed'", (nid,)).fetchone()["c"]
        return n <= self.max_retries


def guarded_action(repo: Repository, key: str, fn, *args, **kwargs):
    """动作回执门面（R2 修订）：成功登记 status='done'；异常登记 status='failed'
    并抛出——失败回执允许同 key 重试（receipt_first 会删除重登）；重复成功
    请求返回首次结果，不盲目重复副作用。"""
    if not repo.receipt_first(key):
        return repo.receipt_get(key)
    try:
        result = fn(*args, **kwargs)
    except BaseException as e:
        with repo.tx() as cur:
            cur.execute(
                "UPDATE action_receipts SET result_json=?, status='failed'"
                " WHERE key=?",
                (json.dumps({"ok": False,
                             "error": f"{type(e).__name__}: {e}"},
                            ensure_ascii=False, default=str), key))
        raise
    with repo.tx() as cur:
        cur.execute("UPDATE action_receipts SET result_json=?, status='done'"
                    " WHERE key=?",
                    (json.dumps(result, ensure_ascii=False, default=str), key))
    return result


def pause(repo: Repository, graph, mid: str, node_id: str, reason: str = "paused"):
    """暂停（R2+：可重复直达——同原因第二次 pause 必须真实生效，
    不做回执缓存；状态前置校验由状态机负责）。"""
    return graph.block(mid, node_id, reason)


def resume(repo: Repository, graph, mid: str, node_id: str):
    """恢复（R2：可重复动作，不走幂等缓存——重新 blocked 后可再次生效；
    状态前置校验由状态机负责：非 blocked 即报错，不假成功）。"""
    return graph.unblock(mid, node_id)


def cancel(repo: Repository, graph, mid: str, node_id: str, reason: str = "cancelled"):
    """取消节点（R2+：可重复直达）：在途尝试一并取消（迟到提交将被状态机拒绝）。"""

    def _do():
        with repo.tx() as cur:
            run = cur.execute(
                "SELECT id FROM attempts WHERE node_id=? AND status='running'",
                (node_id,)).fetchone()
            if run:
                cur.execute(
                    "UPDATE attempts SET status='cancelled', ended_at=? WHERE id=?",
                    (now(), run["id"]))
                repo.append_event(cur, mid, "attempt_cancelled", node_id,
                                  attempt=run["id"], reason=reason)
        graph.cancel_node(mid, node_id, reason)
        return {"cancelled": node_id}

    return _do()  # R2+：可重复动作，直达状态机（同参数重试真实执行）


def heartbeat(repo: Repository, attempt_id: str):
    repo.update_attempt(attempt_id, heartbeat_at=now())


def reap_orphans(repo: Repository, graph, mid: str, stale_seconds: int = 300,
                 protected: set[str] | None = None) -> list[str]:
    """收割过期执行：running 且心跳超时的尝试 → orphaned，节点回 pending。
    v3.2：SELECT 移入事务内复核（收割竞态防护）；protected=调度器在途集合，
    本服务持有者不收割。"""
    cutoff = (datetime.now() - timedelta(seconds=stale_seconds)).isoformat(
        timespec="seconds")
    protected = protected or set()
    reaped: list[str] = []
    with repo.tx() as cur:
        rows = cur.execute(
            "SELECT id, node_id, heartbeat_at FROM attempts WHERE mission_id=?"
            " AND status='running'", (mid,)).fetchall()
        for r in rows:
            if r["id"] in protected:
                continue
            if r["heartbeat_at"] and r["heartbeat_at"] >= cutoff:
                continue  # 事务内核验：心跳仍新鲜则跳过
            cur.execute(
                "UPDATE attempts SET status='orphaned', category='orphan',"
                " ended_at=? WHERE id=? AND status='running'", (now(), r["id"]))
            node = cur.execute("SELECT status FROM nodes WHERE id=?",
                               (r["node_id"],)).fetchone()
            if node and node["status"] == "in_work":
                cur.execute(
                    "UPDATE nodes SET status='pending', updated_at=? WHERE id=?",
                    (now(), r["node_id"]))
            repo.append_event(cur, mid, "orphan_reaped", r["node_id"],
                              attempt=r["id"])
            reaped.append(r["id"])
    return reaped


def recover_mission(repo: Repository, graph, mid: str) -> dict:
    """服务重启后的恢复扫描：本机单服务假设下，一切 running 均为孤儿。"""
    reaped = reap_orphans(repo, graph, mid, stale_seconds=0)
    # 心跳为空的也要收：stale_seconds=0 会把心跳=now 的也算过期 ✓
    nodes = repo.list_nodes(mid)
    return {
        "reaped_attempts": reaped,
        "resumable_nodes": [n.id for n in nodes if n.status == "pending"],
        "blocked_nodes": [n.id for n in nodes if n.status == "blocked"],
        "done": repo.get_mission(mid).status,
    }
