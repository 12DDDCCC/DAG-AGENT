# -*- coding: utf-8 -*-
"""v3.8 规模与并发回归：T2 规模压测抓出的 Repository 共享连接缺陷。

缺陷：Repository 单 SQLite 连接被多线程共用——tx() 写事务持实例锁，但
事务外的裸读路径（self._conn.execute）不持锁，与写事务在同一连接上
交错执行 → InterfaceError: bad parameter or other API misuse /
中间态读（KeyError: 尝试不存在）。并发 ≤2 时窗口小未触达，Scheduler
max_concurrency=10 规模压测下 111 节点图 5 节点中招。

回归：8 线程并发"短写事务 + 裸读"轰击同一 Repository，断言零异常、
计数一致（修复=线程本地连接，WAL 多读一写）。
"""
from __future__ import annotations

import threading

from dag_agent.graph import Graph
from dag_agent.repository import Repository, now


def test_repository_concurrent_readers_writers(tmp_path):
    repo = Repository(tmp_path / "mission.db")
    g = Graph(repo)
    mid, root = g.create_mission("并发读写压测")
    g.add_node(mid, "n1", "并发读写下保持一致", parent_id=root)
    n_nodes = len(repo.list_nodes(mid))

    errors: list[str] = []
    rounds = 40

    def worker(i: int):
        try:
            for k in range(rounds):
                with repo.tx() as cur:
                    cur.execute(
                        "UPDATE missions SET updated_at=?, budget_json=?"
                        " WHERE id=?",
                        (now(), f'{{"w":{i},"k":{k}}}', mid))
                # 写事务外的裸读（缺陷路径：与其它线程的写事务交错）
                assert len(repo.list_nodes(mid)) == n_nodes
                repo.get_node(root)
                repo.deps_of(root)
        except Exception as e:  # noqa: BLE001 —— 收集一切并发异常
            errors.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not errors, f"并发读写异常 {len(errors)} 条，首条：{errors[0]}"
    bj = repo.get_mission(mid).budget_json
    assert bj and "w" in bj  # 最后一次写落库可见
    repo.close()


def test_repository_thread_local_conn_isolation(tmp_path):
    """不同线程持有不同连接；同线程复用同一连接；close 后全部关闭。"""
    repo = Repository(tmp_path / "mission.db")
    main_conn = repo._conn
    seen: list[int] = []
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        seen.append(id(repo._conn))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert id(repo._conn) == id(main_conn)  # 主线程复用主连接
    assert len(set(seen)) == 4              # 4 个 worker 各持独立连接
    assert main_conn not in [id(c) for c in _all(repo)]
    repo.close()
    try:
        _ = repo._conn
        raise AssertionError("close 后访问 _conn 应报错")
    except RuntimeError:
        pass


def _all(repo: Repository) -> list:
    return list(repo._all_conns.values())
