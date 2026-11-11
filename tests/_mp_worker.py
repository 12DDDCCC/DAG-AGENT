"""_mp_worker.py — 跨进程压测worker（独立进程，验证rev+文件锁在真双开下的表现）

用法：python _mp_worker.py <home> <mid> <worker_id>
循环认领-提交直到任务完成或前沿为空；stdout最后一行输出JSON统计。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dag_agent.store import DagError, MissionStore  # noqa: E402


class Stub:
    def run(self, checks):
        return {"ok": True, "evidence": f"mp-worker-pass"}


def main(home: str, mid: str, wid: str) -> int:
    s = MissionStore(home=Path(home))
    stats = {"worker": wid, "submitted": 0, "conflicts": 0, "lock_timeouts": 0,
             "done": False, "errors": []}
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            if s.status(mid)["mission_done"]:
                stats["done"] = True
                break
            fr = s.frontier(mid)
            if not fr:
                time.sleep(0.05)
                continue
            node = fr[0]["id"]
            try:
                s.submit(mid, node, f"worker{wid}完成", checker=Stub())
                stats["submitted"] += 1
            except DagError as exc:
                msg = str(exc)
                if "rev不一致" in msg:
                    stats["conflicts"] += 1
                    time.sleep(0.01)
                elif "文件锁获取超时" in msg:
                    stats["lock_timeouts"] += 1
                    time.sleep(0.05)
                elif "已done" in msg or "aggregate" in msg:
                    continue  # 别的worker先做了/分解型节点，重新看前沿
                else:
                    stats["errors"].append(msg[:120])
                    time.sleep(0.02)
        except Exception as exc:  # noqa: BLE001
            stats["errors"].append(repr(exc)[:120])
            time.sleep(0.02)
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3]))
