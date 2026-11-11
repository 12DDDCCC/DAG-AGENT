"""test_stress.py — 多mission并发压测 + 同mission乐观锁冲突重试。"""
from __future__ import annotations

import threading
import time
import traceback

from dag_agent.store import DagError, MissionStore


class Stub:
    def run(self, checks):
        return {"ok": True, "evidence": "stub-pass"}


def test_multi_mission_parallel(tmp_path):
    """8线程各建各的mission全生命周期：互不干扰、全部完成。"""
    errors: list[str] = []

    def work(i: int):
        try:
            s = MissionStore(home=tmp_path)
            mid = f"p{i:02d}"
            s.create_mission(mid, f"任务{i}", "b", "交付", "完成")
            s.decompose(mid, "n0", [{"title": "a", "statement": "s"},
                                    {"title": "c", "statement": "s2"}])
            s.submit(mid, "n1", "x", checker=Stub())
            r = s.submit(mid, "n2", "y", checker=Stub())
            assert r["status"]["mission_done"]
        except Exception as exc:  # noqa: BLE001
            tail = traceback.format_exc().strip().splitlines()[-2:]
            errors.append(f"mission{i}: {exc!r} @ {' | '.join(tail)}")

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    s = MissionStore(home=tmp_path)
    done = sum(1 for i in range(8) if s.status(f"p{i:02d}")["mission_done"])
    assert done == 8


def test_same_mission_rev_conflicts_resolve(tmp_path):
    """6线程并发提交同一mission的不同节点：rev冲突被拒绝且重试后全部成功，
    已验证的done无一丢失，根节点级联闭合。"""
    s = MissionStore(home=tmp_path)
    s.create_mission("race", "并发", "b", "交付", "完成")
    s.decompose("race", "n0", [{"title": f"c{i}", "statement": "s"}
                               for i in range(6)])
    conflicts = {"n": 0}
    lock = threading.Lock()

    def work(j: int):
        node = f"n{j + 1}"
        for attempt in range(100):
            try:
                s.submit("race", node, "ok", checker=Stub())
                return
            except DagError as exc:
                msg = str(exc)
                if "并发冲突" in msg or "文件锁获取超时" in msg:
                    with lock:
                        conflicts["n"] += 1
                    time.sleep(0.005 * (attempt + 1))
                    continue
                raise
        raise AssertionError(f"{node} 重试耗尽")

    threads = [threading.Thread(target=work, args=(j,)) for j in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not any(t.is_alive() for t in threads)
    st = s.status("race")
    assert st["counts"]["done"] == 7, "6子节点+根全部闭合"
    assert st["mission_done"]
    print(f"并发冲突发生{conflicts['n']}次，全部经重试收敛")
