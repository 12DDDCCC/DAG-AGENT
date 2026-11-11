# -*- coding: utf-8 -*-
"""act1d — ROOT 宿主机械整合（SPEC 允许出题方代行 N01 类机械节点）：
合并 inputs 各来源 → 正常 submit → judge 20 例 + 干净重放独立判卷闭合。"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from dag_agent.assessment import default_checker3
from dag_agent.graph import Graph
from dag_agent.repository import Repository

MID = "m-94ab8710"
BUNDLE = ["loader.py", "validation.py", "reconcile.py", "render.py",
          "main.py", "README.md", "data/orders.csv", "data/refunds.csv",
          "INTEGRATION.md"]

repo = Repository(ROOT / "data" / "missions" / MID / "mission.db")
g = Graph(repo, checker3=default_checker3)
root = repo._conn.execute(
    "SELECT id FROM nodes WHERE mission_id=? AND parent_id IS NULL",
    (MID,)).fetchone()["id"]
att = repo.get_attempt(repo._conn.execute(
    "SELECT id FROM attempts WHERE node_id=? AND status='running'",
    (root,)).fetchone()["id"]) if repo._conn.execute(
    "SELECT id FROM attempts WHERE node_id=? AND status='running'",
    (root,)).fetchone() else None
aid = att.id if att else g.start_attempt(MID, root, backend="host")
wd = Path(repo.get_attempt(aid).workdir)
# 手动播种（不经调度器）：按认领绑定快照逐源 seed
if not (wd / "inputs").is_dir():
    for b in json.loads(repo.get_attempt(aid).input_binding_json or "[]"):
        sid = b.get("snapshot_id")
        if sid:
            g.store.seed_from_snapshot(repo.snapshot_by_id(sid), wd)
# 合并：inputs/<源>/<rel> → ./<rel>；同名取来源快照版本更高者
ins = wd / "inputs"
picked: dict[str, tuple[int, Path]] = {}
for src in sorted(ins.iterdir()) if ins.is_dir() else []:
    meta = repo._conn.execute(
        "SELECT s.artifact_version v FROM snapshots s "
        "JOIN attempts a ON a.id=s.attempt_id WHERE a.id LIKE ? "
        "AND s.node_id=(SELECT id FROM nodes WHERE id=?) ORDER BY v DESC",
        (f"%{src.name}%", src.name)).fetchone()
    ver = meta["v"] if meta else 0
    for f in src.rglob("*"):
        if f.is_file():
            rel = f.relative_to(src)
            if rel.as_posix() not in picked or ver > picked[rel.as_posix()][0]:
                picked[rel.as_posix()] = (ver, f)
missing = []
for rel, (_v, src) in sorted(picked.items()):
    dest = wd / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
have = {p.as_posix() for p in wd.rglob("*") if p.is_file()}
missing = [b for b in BUNDLE if b not in have]
(wd / "INTEGRATION.md").write_text(
    "# 整合说明\n来源：inputs/ 各已验收节点快照（宿主机械合并）。\n",
    encoding="utf-8")
print("missing:", missing)
if missing:
    print("ABORT: bundle 缺件")
    repo.close()
    sys.exit(1)
r = g.submit_attempt(MID, root, aid, artifact_paths=BUNDLE)
print("verdict:", r.get("verdict"))
print("evidence:", (r.get("evidence") or "")[:300])
m = repo.get_mission(MID)
print("mission:", m.status, m.closure_basis)
budget = json.loads(m.budget_json or "{}")
print("budget:", budget)
repo.close()
sys.exit(0 if r.get("verdict") == "pass" else 1)
