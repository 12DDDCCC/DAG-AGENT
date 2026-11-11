"""soak_runner.py — 长稳浸泡：连续真实mission生命周期循环，向目标时长推进。

每轮：随机选模板 → create_from_template → 逐子任务写真实产物+提交（穿插
block/unblock与偶发FAIL-重试）→ 级联闭合 → 逐轮JSONL记录（轮号/模板/耗时/
节点数/冲突/结果）→ 每5轮刷新聚合看板。Ctrl+C优雅停止并写soak_summary.json。

用法：
    python soak_runner.py --duration-seconds 21600 [--home <dir>] [--quiet]
后台长跑建议（向8小时推进）：
    start /b python soak_runner.py --duration-seconds 28800 >> soak.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import random
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from dag_agent.render import render_index
from dag_agent.store import DagError, MissionStore
from dag_agent.templates import TEMPLATES, create_from_template

ARTIFACT_TEMPLATES = {
    "draft.md": "资料：本节收集素材。",
    "facts_check.md": "核对：逐条完成。",
    "report.md": "结论：可行。信息源：内部。",
    "impl_notes.md": "实现：核心逻辑与接口。",
    "test_evidence.log": "passed: 全部通过",
    "docs.md": "用法：见示例。",
    "final.md": "用法：见示例。实测：已验证。",
    "test_evidence2.log": "passed: 全部通过",
    "release_notes.md": "变更：本轮迭代。边界：见README。",
    "readback.txt": "证词：字面描述。边界：无。",
    "audit_report.md": "差距：无。处置：关闭。",
    "index_draft.md": "索引：本目录条目。",
    "index_final.md": "一致：条目与目录一致。",
}


class SoakRunner:
    def __init__(self, home: Path, duration: float, quiet: bool = False,
                 seed: int | None = None):
        self.home = home
        self.duration = duration
        self.quiet = quiet
        self.rng = random.Random(seed)
        self.store = MissionStore(home=home)
        self.stop = False
        self.rounds: list[dict] = []
        self.counters = {"missions": 0, "submits": 0, "fails": 0,
                         "conflicts": 0, "blocks": 0, "readbacks": 0}

    def log(self, msg: str) -> None:
        if not self.quiet:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

    def request_stop(self, *_) -> None:
        self.stop = True

    def _write_artifacts(self, mid: str, children: list[str]) -> None:
        ws = self.store.mission_dir(mid) / "workspace"
        ws.mkdir(parents=True, exist_ok=True)
        keys = list(ARTIFACT_TEMPLATES)
        for nid in children:
            fn = self.rng.choice(keys)
            (ws / fn).write_text(ARTIFACT_TEMPLATES[fn], encoding="utf-8")

    def _round(self, i: int) -> dict:
        t0 = time.monotonic()
        mid = f"soak-{datetime.now().strftime('%Y%m%d')}-{i:04d}"
        tpl = self.rng.choice(sorted(TEMPLATES))
        r = create_from_template(self.store, mid, f"浸泡轮{i}", "长稳soak", tpl)
        children = r["children"]
        self.counters["missions"] += 1
        # 真实产物：每个child写一个含全部可能关键词的内容文件
        self._write_artifacts(mid, children)
        # 偶发阻塞-解锁演练（10%概率，选一个child）
        if children and self.rng.random() < 0.10:
            victim = self.rng.choice(children)
            self.store.block(mid, victim, f"soak演练阻塞 r{i}")
            self.counters["blocks"] += 1
            self.store.unblock(mid, victim)
        # 偶发读回（10%概率）
        if self.rng.random() < 0.10 and children:
            self.store.set_readback(mid, children[0],
                                    f"证词r{i}。边界：soak演练。", model="soak")
            self.counters["readbacks"] += 1
        # 逐个提交（偶发先FAIL一次再重试，5%概率）
        for nid in children:
            if self.rng.random() < 0.05:
                self.store.submit(mid, nid, "故意失败演练", checker=_AlwaysFail())
                self.counters["fails"] += 1
            s = self.store.submit(mid, nid, f"soak r{i}", checker=StubPass())
            self.counters["submits"] += 1
            del s
        st = self.store.status(mid)
        return {"round": i, "template": tpl, "mission": mid,
                "mission_done": st["mission_done"], "nodes": st["total"],
                "seconds": round(time.monotonic() - t0, 2),
                **self.counters}

    def run(self) -> dict:
        t_start = time.monotonic()
        rnd = 0
        while not self.stop and time.monotonic() - t_start < self.duration:
            try:
                rec = self._round(rnd)
                self.rounds.append(rec)
                rnd += 1
                if rnd % 5 == 0:
                    render_index(self.store)
                    self.log(f"round {rnd}: missions={self.counters['missions']} "
                             f"submits={self.counters['submits']} "
                             f"fails={self.counters['fails']}")
                # 逐轮JSONL
                with (self.home / "soak_rounds.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except DagError as exc:
                self.counters["conflicts"] += 1
                self.log(f"round {rnd} DagError(预期内): {exc}")
                time.sleep(0.2)
            except Exception as exc:  # noqa: BLE001
                self.log(f"round {rnd} 未预期异常: {exc!r}")
                self.rounds.append({"round": rnd, "error": repr(exc)})
                time.sleep(0.5)
        summary = {"started": datetime.now().isoformat(timespec="seconds"),
                   "rounds": len(self.rounds),
                   "errors": sum(1 for r in self.rounds if "error" in r),
                   "counters": self.counters,
                   "duration_seconds": round(time.monotonic() - t_start, 1)}
        (self.home / "soak_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        self.log(f"soak结束: {summary}")
        return summary


class StubPass:
    def run(self, checks):
        return {"ok": True, "evidence": "soak-stub-pass"}


class _AlwaysFail:
    def run(self, checks):
        return {"ok": False, "evidence": "soak故意失败演练", "hint": "演练FAIL-重试路径"}


def main() -> int:
    ap = argparse.ArgumentParser(prog="soak_runner")
    ap.add_argument("--duration-seconds", type=float, default=28800.0)
    ap.add_argument("--home", default=None)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    home = Path(args.home) if args.home else MissionStore().home
    home.mkdir(parents=True, exist_ok=True)
    runner = SoakRunner(home, args.duration_seconds, args.quiet, args.seed)
    signal.signal(signal.SIGINT, runner.request_stop)
    runner.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
