# -*- coding: utf-8 -*-
"""ag-recon B4 正式修复驱动：平台 revise → n5 修复（子代理执行）→ 下游机械整合 → ROOT 22 例复验。

用法（宿主分段驱动）：
  python b4_revise_driver.py revise     # revise n5 + 全链五段简报 + claim n5(host) → 打印 workdir
  python b4_revise_driver.py submit_n5  # 提交 n5（考卷 validation）
  python b4_revise_driver.py chain      # n2→n6→n10→n11→n9 机械整合提交 → ROOT judge 22 例
  python b4_revise_driver.py status     # 只读状态
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT_DIR = Path(r"D:\AI\DAG-agent")
sys.path.insert(0, str(ROOT_DIR))

from dag_agent.artifacts import ArtifactStore  # noqa: E402
from dag_agent.graph import Graph  # noqa: E402
from dag_agent.repository import Repository  # noqa: E402

DB = ROOT_DIR / "data" / "missions" / "ag-recon" / "mission.db"
MID = "ag-recon"

# 依整合顺序（依赖序）：n2 先行；n7 是 n2 消费者也已级联失效（重交旧产物过新考卷）
CHAIN = ["n2", "n7", "n6", "n10", "n11", "n9", "n1"]

FIX_BRIEF = {
    "n5": (
        "【输入】工作目录即本节点现场（绝对路径见交付清单所在目录）；"
        "修复对象 validation.py 现有缺陷：AMOUNT_RE = re.compile(r\"^[0-9]+$\")"
        "——Python 正则 $ 容忍尾随换行，金额 \"8000\\n\" 被误接受（judge v2 新增"
        "两变例负例要求拒收）。\n"
        "【交付清单】validation.py（写到本节点工作目录根）。\n"
        "【接口签名】保持既有冻结接口：InputError(code, message='')；"
        "validate(orders, refunds) -> (paid_orders, refunds, cancelled_count)；"
        "内部 _parse_amount(value: str) -> int 且 "
        "AMOUNT_RE = re.compile(r\"[0-9]+\\Z\")（严格结尾，无 ^ 前缀也可，"
        "配合 .fullmatch/ match 语义不放松）。\n"
        "【样例】_parse_amount(\"8000\\n\") 必须 raise InputError(\"amount_invalid\")；"
        "_parse_amount(\"00800\") == 800（前导零合法）；"
        "_parse_amount(str(10**25 + 37)) 值精确保留（不得走 float）。\n"
        "【自测入口】在本节点工作目录执行："
        "python -B \"D:\\AI\\DAG-agent\\outputs\\多agent协同验收项目\\checks\\"
        "mod_checks.py\" validation"
    ),
}

INTEG_BRIEF = (
    "【输入】本节点工作目录 inputs/ 已按绑定快照播种（上游各来源最新版，"
    "播种前逐文件哈希校验）。\n"
    "【交付清单】节点定义的全部申报文件（机械整合：从 inputs 各来源取各文件"
    "最新版本合并到工作目录根，内容不做创造性修改）。\n"
    "【接口签名】整合不改变任何文件内容签名；同名文件取版本更新的来源。\n"
    "【样例】合并后工作目录根应能逐件通过整合考卷（存在/非空/可编译）。\n"
    "【自测入口】在本节点工作目录执行本节点考卷命令（见节点 check_cmd）。"
)


def _repo():
    return Repository(DB)


def _graph(repo):
    return Graph(repo)


def phase_revise() -> None:
    repo = _repo()
    g = _graph(repo)
    try:
        before = {r["id"]: r["status"] for r in
                  repo._conn.execute("SELECT id, status FROM nodes")}
        g.revise_node(MID, "n5", reason=(
            "B4 正式修复：validation 金额正则 $ 容忍尾随换行，判卷器 v2 新增"
            " amount_trailing_newline 两变例负例，须以 \\Z 严格结尾重做"))
        after = {r["id"]: r["status"] for r in
                 repo._conn.execute("SELECT id, status FROM nodes")}
        invalidated = [k for k, v in after.items()
                       if v in ("pending",) and before.get(k) == "accepted"]
        print("失效节点:", sorted(invalidated))
        # 全链五段简报（宿主出题职责）
        with repo.tx() as cur:
            cur.execute("UPDATE nodes SET statement=? WHERE id=?",
                        (FIX_BRIEF["n5"], "n5"))
            for nid in ("n2", "n6", "n9", "n10", "n11"):
                cur.execute("UPDATE nodes SET statement=? WHERE id=?",
                            (INTEG_BRIEF, nid))
        aid = g.start_attempt(MID, "n5", backend="host")
        att = repo.get_attempt(aid)
        print("n5 attempt:", aid)
        print("n5 workdir:", att.workdir)
    finally:
        repo.close()


def _submit(g, repo, nid: str) -> dict:
    node = repo.get_node(nid)
    declared = json.loads(node.declared_artifacts_json or "[]")
    row = repo._conn.execute(
        "SELECT id FROM attempts WHERE node_id=? AND status='running' "
        "ORDER BY started_at DESC LIMIT 1", (nid,)).fetchone()
    aid = row["id"]
    return g.submit_attempt(MID, nid, aid, artifact_paths=declared)


def phase_submit_n5() -> None:
    repo = _repo()
    g = _graph(repo)
    try:
        res = _submit(g, repo, "n5")
        print("n5 submit:", res["verdict"], "|", (res.get("evidence") or "")[:160])
    finally:
        repo.close()


def _sha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pick_latest_source(repo, bindings: list[dict], name: str):
    """同名文件多来源时按**快照 created_at（全局时间戳）**显式取最新，
    version 仅作同节点 tiebreak（Codex adcc5d5：不同节点版本号不可当
    全局先后序，遍历覆盖≠取最新）。返回 (path, 来源描述) 或 None。"""
    best = None
    home = Path(repo.db_path).parent
    for b in bindings:
        sid = b.get("snapshot_id")
        if not sid:
            continue
        snap = repo.snapshot_by_id(sid)
        for f in json.loads(snap.files_json or "[]"):
            if Path(f["rel_path"]).name == name:
                key = (snap.created_at or "", snap.artifact_version)
                if best is None or key > best[0]:
                    best = (key,
                            home / "accepted" / snap.node_id
                            / f"v{snap.artifact_version}" / f["rel_path"],
                            f"{snap.node_id}:v{snap.artifact_version}"
                            f"/{f['rel_path']}")
    return (best[1], best[2]) if best else None


def verify_snapshot_file(repo, nid: str, version: int, rel: str,
                         path: Path) -> None:
    """历史快照复用核验：文件 sha256 必须与快照 manifest 一致
    （Codex adcc5d5：直接复制不核验、无来源登记=不可追溯）。"""
    row = repo._conn.execute(
        "SELECT files_json FROM snapshots WHERE node_id=? AND artifact_version=?"
        " ORDER BY id DESC LIMIT 1", (nid, version)).fetchone()
    if row is None:
        raise SystemExit(f"复用核验失败：{nid} v{version} 无快照记录")
    want = {f["rel_path"]: f["sha256"] for f in json.loads(row["files_json"])}
    if rel not in want:
        raise SystemExit(f"复用核验失败：{rel} 不在 {nid} v{version} manifest")
    if _sha256(path) != want[rel]:
        raise SystemExit(f"复用核验失败：{rel} 与 {nid} v{version} manifest 哈希不符")


def _merge_integ(g, repo, store, nid: str) -> dict:
    node = repo.get_node(nid)
    declared = json.loads(node.declared_artifacts_json or "[]")
    aid = g.start_attempt(MID, nid, backend="host")
    att = repo.get_attempt(aid)
    wd = Path(att.workdir)
    bindings = json.loads(att.input_binding_json or "[]")
    # 播种绑定快照（整合尝试的绑定=孩子/依赖快照）
    for b in bindings:
        sid = b.get("snapshot_id")
        if sid:
            store.seed_from_snapshot(repo.snapshot_by_id(sid), wd)
    # 机械合并（v2：来源显式化+可追溯）：
    # - 同名多来源 → pick_latest_source 按快照时间戳取最新（并登记来源）；
    # - 缺件 → 回退本节点历史快照（复制前 sha256 核验 manifest）；
    # - 全部复用来源记入 attempt resources_json（origin=host-driver-mechanical）
    sources: dict[str, str] = {}
    for rel in declared:
        name = Path(rel).name
        picked = pick_latest_source(repo, bindings, name)
        if picked is not None:
            src_path, src_desc = picked
            dest = wd / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dest)
            sources[rel] = src_desc
            continue
        if (wd / rel).is_file():
            continue
        old = repo._conn.execute(
            "SELECT artifact_version FROM snapshots WHERE node_id=? "
            "ORDER BY artifact_version DESC LIMIT 1", (nid,)).fetchone()
        fallback = None
        if old:
            vdir = (DB.parent / "accepted" / nid / f"v{old['artifact_version']}"
                    / rel)
            if vdir.is_file():
                verify_snapshot_file(repo, nid, old["artifact_version"],
                                     rel, vdir)
                fallback = vdir
                sources[rel] = f"{nid}:v{old['artifact_version']}/{rel}(复用)"
        if fallback is None:
            raise SystemExit(f"整合缺件：{nid} {rel}（inputs 与历史快照均无）")
        dest = wd / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fallback, dest)
    res = g.submit_attempt(MID, nid, aid, artifact_paths=declared)
    # 来源登记（宿主记账：不伪造 session/token，只记机械来源）
    if res.get("verdict") == "pass":
        with repo.tx() as cur:
            row = cur.execute("SELECT resources_json FROM attempts WHERE id=?",
                              (aid,)).fetchone()
            try:
                rj = json.loads(row["resources_json"] or "{}")
            except Exception:
                rj = {}
            rj["origin"] = "host-driver-mechanical"
            rj["sources"] = sources
            cur.execute("UPDATE attempts SET resources_json=? WHERE id=?",
                        (json.dumps(rj, ensure_ascii=False), aid))
    return res


def phase_chain() -> None:
    repo = _repo()
    g = _graph(repo)
    store = ArtifactStore(DB.parent)
    try:
        for nid in CHAIN:
            node = repo.get_node(nid)
            if node.status == "accepted" and nid != "n1":
                # revise 后仍 accepted 的（未受影响）跳过；n1 若已 done 需重开
                print(f"{nid}: accepted（未受影响，跳过）")
                continue
            res = _merge_integ(g, repo, store, nid)
            print(f"{nid} submit:", res["verdict"],
                  "|", (res.get("evidence") or "")[:200])
            if res["verdict"] != "pass":
                break
    finally:
        repo.close()


def phase_status() -> None:
    repo = _repo()
    try:
        m = repo._conn.execute(
            "SELECT status FROM missions WHERE id=?", (MID,)).fetchone()
        print("mission:", m["status"])
        for r in repo._conn.execute(
                "SELECT id, title, status FROM nodes ORDER BY rowid"):
            print(f"  {r['id']:<5} {r['title']:<14} {r['status']}")
    finally:
        repo.close()


if __name__ == "__main__":
    {"revise": phase_revise, "submit_n5": phase_submit_n5,
     "chain": phase_chain, "status": phase_status}[sys.argv[1]]()
