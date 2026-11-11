"""v3 步骤2 → v3.2：工件版本绑定与验收后不可变存储。

v3.2 变更（协议01/02/03）：
- 发布改为两阶段：publish_prepared 只复制文件+核哈希（不写库）；
  DB 行（artifacts/snapshots）在 graph.submit_attempt 的正式接收事务内写入——
  崩溃残留的"无 DB 行文件"为孤儿，无害可回收，正式接收永不指向缺件。
- 路径包含判定按解析后的路径组件（Path.is_relative_to），废弃字符串前缀——
  同前缀兄弟目录、.. 、绝对路径与链接逃逸均拒绝。
- 新增 seed_from_snapshot：按绑定快照全量播种 + 逐文件摘要校验。
"""
from __future__ import annotations

import hashlib
import shutil
import os
from pathlib import Path

from .repository import Repository

IGNORED_DIRS = {"__pycache__", ".pytest_cache", ".git", "node_modules", ".dag_tmp"}


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def manifest_hash_of(files: list[dict]) -> str:
    """协议01：规范化 files 清单的整体摘要（rel_path 排序，逐行 rel:sha:size）。

    source 摘要协议分四类（F4 后口径，Codex t-fix-review：旧格式重算必须稳定、
    候选字段必须完整绑定）：
    - 无 source 键（最旧）：不加后缀（与 T5 前算法逐字节一致）；
    - {node,version,file} 无 basis（T5 轮旧格式）：:node@ver:file 三段
      （与 T5 轮算法逐字节一致——不得因新算法追加第四段而漂移）；
    - 新 upstream（含 basis）：:node@ver:file:basis 四段；
    - ambiguous/unknown：:origin:node@ver:file,…（候选含 file，按
      (node,version,file) 排序——候选文件名变化必改摘要）。
    """
    def _src_tag(src: dict) -> str:
        if src.get("node") is not None:
            if "basis" in src:
                return (f":{src['node']}@{src['version']}"
                        f":{src.get('file', '')}:{src['basis']}")
            # 旧格式（无 basis）——三段，保持历史摘要可复算
            return (f":{src['node']}@{src['version']}"
                    f":{src.get('file', '')}")
        cands = sorted(src.get("candidates") or [],
                       key=lambda c: (c.get("node", ""),
                                      c.get("version", 0),
                                      c.get("file", "")))
        return f":{src.get('origin', 'unknown')}:" + ",".join(
            f"{c['node']}@{c['version']}:{c.get('file', '')}"
            for c in cands)

    lines = sorted(f"{f['rel_path']}:{f['sha256']}:{f.get('size', 0)}"
                   + (_src_tag(f["source"]) if f.get("source") else "")
                   for f in files)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def lineage_of(snapshot, rel_path: str) -> dict | None:
    """T5 机器可答：某快照中 rel_path 这件文件从哪来。

    返回 {"origin":"upstream","node","version","file","basis"}（原样转交，
    basis=path+sha 强证据 / sha verbatim 推断）、{"origin":"ambiguous",
    "candidates":[…]}（多来源同哈希，保留歧义不静默取一）、
    {"origin":"unknown"}（无上游依据——T5' 起不再自动断言 self）、
    {"origin":"legacy"}（旧协议快照未记录 source，可读但不追溯性证明自产）；
    rel_path 不在该快照 → None。"""
    import json as _json
    for f in _json.loads(snapshot.files_json or "[]"):
        if f["rel_path"] == rel_path:
            s = f.get("source")
            if not s:
                return {"origin": "legacy"}
            if s.get("origin") in ("unknown", "ambiguous"):
                return s
            return {"origin": "upstream", **s}
    return None


def verify_lineage(repo, snapshot) -> list[dict]:
    """T5' 来源断链校验（Codex t-review：只查快照存在≠可溯——须核对
    来源文件在上游 manifest 内且与下游内容一致）。逐件检查：
    - upstream：上游快照存在且 valid、file 在其 manifest、原样关系的
      sha 一致（basis=sha）；
    - ambiguous：列入结果（reason=歧义来源，不冒称已溯）；
    - unknown/legacy：列入结果（reason=来源未记录），不算断链失败但
      明示不可追溯——返回列表非空即"存在未完全可溯件"。
    """
    import json as _json
    out: list[dict] = []
    for f in _json.loads(snapshot.files_json or "[]"):
        s = f.get("source")
        if not s:
            out.append({"rel_path": f["rel_path"],
                        "reason": "legacy：旧协议未记录来源"})
            continue
        if s.get("origin") == "unknown":
            out.append({"rel_path": f["rel_path"],
                        "reason": "unknown：无上游依据（本节点产物或派生）"})
            continue
        if s.get("origin") == "ambiguous":
            out.append({"rel_path": f["rel_path"],
                        "reason": "ambiguous：多来源同哈希，未声明选择",
                        "candidates": s.get("candidates", [])})
            continue
        up = repo._conn.execute(
            "SELECT id, valid, files_json FROM snapshots WHERE node_id=? AND"
            " artifact_version=? ORDER BY id DESC LIMIT 1",
            (s["node"], s["version"])).fetchone()
        if up is None or not up["valid"]:
            out.append({"rel_path": f["rel_path"], "node": s["node"],
                        "version": s["version"],
                        "reason": "上游快照缺失或已失效（来源断链）"})
            continue
        manifest = {x["rel_path"]: x["sha256"]
                    for x in _json.loads(up["files_json"] or "[]")}
        up_file = s.get("file", "")
        if up_file not in manifest:
            out.append({"rel_path": f["rel_path"], "node": s["node"],
                        "version": s["version"],
                        "reason": f"来源文件不在上游 manifest：{up_file}"})
        elif manifest[up_file] != f["sha256"]:
            out.append({"rel_path": f["rel_path"], "node": s["node"],
                        "version": s["version"],
                        "reason": "原样关系摘要不一致（内容与上游不符）"})
    return out


def _within(child: Path, parent: Path) -> bool:
    """组件级包含检查·双道（F2 后口径）。

    第一道（词法，确定性）：normcase+abspath 后组件级 relative_to——
    防绝对路径/../同前缀兄弟目录；不触文件系统的部分无形态二象性
    （Windows resolve() 对存在路径返回 \\\\\\\\?\\\\ 展开形态、不存在路径按词法，
    staging 建立前后形态翻转曾致并发发布偶发误判越界）。
    第二道（实际解析边界）：os.path.realpath 双方展开已存在段的
    junction/symlink/reparse 后再组件比较——拦"词法在内、实指在外"
    的链接逃逸（Codex t-fix-review：junction 播种可写出目标根）。
    realpath 为非严格语义（不存在尾段词法延展），双方各自展开已存在
    段，形态一致；写入前调用方应确保 parent 已存在（staging 先建目录）。
    """
    import os
    try:
        c = Path(os.path.normcase(os.path.abspath(str(child))))
        p = Path(os.path.normcase(os.path.abspath(str(parent))))
        c.relative_to(p)
    except (ValueError, OSError):
        return False
    try:
        cr = Path(os.path.normcase(os.path.realpath(str(child))))
        pr = Path(os.path.normcase(os.path.realpath(str(parent))))
        cr.relative_to(pr)
        return True
    except (ValueError, OSError):
        return False


def _safe_rel(rel: str) -> Path:
    """申报相对路径安全化：拒绝绝对路径与 .. 段。"""
    p = Path(rel.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"工件相对路径非法：{rel}")
    return p


class ArtifactStore:
    """<home>/<mid>/accepted/<nid>/v<version>/<rel>：write-once 内容寻址。"""

    def __init__(self, mission_dir: Path):
        self.mission_dir = Path(mission_dir)
        self.accepted_root = self.mission_dir / "accepted"

    def _version_dir(self, nid: str, version: int) -> Path:
        return self.accepted_root / nid / f"v{version}"

    # ---- v3.2-R1 两阶段发布：独占 staging + 事务内原子 rename ----
    #
    # 所有权规则（修复并发拒收删赢家工件 / 崩溃孤儿卡死）：
    # - P3 只写 accepted/<nid>/.staging/<aid>/（独占目录，撞车不可能）；
    # - P4 在持写锁事务内取 version=MAX+1 并 rename staging→v<version>——
    #   败者/拒收只回收自己的 .staging/<aid>，永远碰不到 vN 目录；
    # - 崩溃残留：rename 后未提交 → vN 无 DB 行=孤儿；下次 commit 见
    #   dest 存在且无行引用 → 幂等替换（锁内判读，安全）。

    def _staging_dir(self, nid: str, attempt_id: str) -> Path:
        import uuid as _uuid
        return self.accepted_root / nid / ".staging" / \
            f"{attempt_id}-{_uuid.uuid4().hex[:8]}"

    def stage_prepared(self, mid: str, nid: str, attempt_id: str,
                       workdir: Path, rel_paths: list[str]) -> tuple[Path, list[dict]]:
        """P3：把申报文件从冻结候选复制进独占 staging 目录（不写库、不占版本）。
        R2：staging 每次提交唯一（<aid>-<uuid>）——同 attempt 并发重复提交
        不共享目录。返回 (staging_dir, files_meta)。"""
        staging = self._staging_dir(nid, attempt_id)
        shutil.rmtree(staging, ignore_errors=True)
        # F2：先建 staging 再逐件检查——_within 第二道 realpath 需双方
        # 已存在段稳定（不存在→mkdir 前后的形态/展开竞态是旧偶发根因）
        staging.mkdir(parents=True, exist_ok=True)
        rows = []
        for rel in rel_paths:
            rp = _safe_rel(rel)
            src = workdir / rp
            if not src.is_file() or not _within(src, workdir):
                raise ValueError(f"工件不存在或越界：{rel}")
            dest = staging / rp
            if not _within(dest, staging):
                # T3 诊断：带上双方绝对路径与存在性（复发时可直接定位形态/竞态）
                raise ValueError(
                    f"staging 目标越界：{rel}（dest={os.path.abspath(str(dest))}"
                    f" staging={os.path.abspath(str(staging))}"
                    f" staging_exists={staging.exists()}）")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            h = sha256_file(dest)
            rows.append({"rel_path": rp.as_posix(), "sha256": h,
                         "size": dest.stat().st_size})
        return staging, rows

    def commit_staged(self, cur, nid: str, staging: Path,
                      attempt_id: str) -> int:
        """P4（事务内，调用方持写锁）：version=MAX+1；rename staging→vN。
        vN 已存在且无 DB 行=崩溃孤儿 → 幂等替换；有行=异常冲突（防御）。"""
        row = cur.execute(
            "SELECT COALESCE(MAX(version),0)+1 AS v FROM artifacts WHERE node_id=?",
            (nid,)).fetchone()
        version = row["v"]
        dest = self._version_dir(nid, version)
        if dest.exists():
            ref = cur.execute(
                "SELECT COUNT(*) AS c FROM artifacts WHERE node_id=? AND version=?",
                (nid, version)).fetchone()["c"]
            if ref:
                raise ValueError(f"版本 {version} 已被正式接收（不应发生，锁内）")
            shutil.rmtree(dest)          # 崩溃孤儿（rename后未提交），替换
        # Windows 瞬态共享冲突防御：短重试消除防病毒/句柄瞬态占用 WinError 5
        for _try in range(5):
            try:
                staging.rename(dest)             # 同卷 rename，事务持锁期间原子换主
                break
            except PermissionError:
                if _try == 4:
                    raise
                import time as _t
                _t.sleep(0.02)
        return version

    def discard_staged(self, staging: Path | None):
        """拒收/失败回收：只删本次提交的独占 staging（永不触碰 vN）。"""
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)

    # 兼容旧签名（仅历史调用/测试）：等价 stage→直接以当前 MAX+1 落位
    def publish_prepared(self, repo: Repository, mid: str, nid: str,
                         attempt_id: str, workdir: Path, rel_paths: list[str],
                         version: int) -> list[dict]:
        staging, rows = self.stage_prepared(mid, nid, attempt_id, workdir,
                                            rel_paths)
        with repo.tx() as cur:
            v = self.commit_staged(cur, nid, staging, attempt_id)
        return [{**r, "version": v} for r in rows]

    def next_version(self, repo: Repository, nid: str) -> int:
        row = repo._conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 AS v FROM artifacts WHERE node_id=?",
            (nid,)).fetchone()
        return row["v"]

    def discard_prepared(self, nid: str, version: int):
        """废弃兼容入口：不再按版本目录回收（所有权已隔离），保留空实现
        以兼容旧调用面。"""
        return None

    # ---- 播种（协议02） ----

    def seed_from_snapshot(self, snapshot, dest_root: Path) -> list[str]:
        """按绑定快照全量播种到 dest_root/inputs/<source>/…，播种前逐文件校验。
        缺件/摘要不符/越界 → ValueError（调用方终止尝试为 error，不改绑）。"""
        import json as _json
        files = _json.loads(snapshot.files_json or "[]")
        copied: list[str] = []
        src_root = self._version_dir(snapshot.node_id, snapshot.artifact_version)
        for f in files:
            rp = _safe_rel(f["rel_path"])
            src = src_root / rp
            if not src.is_file() or not _within(src, src_root):
                raise ValueError(f"快照文件缺失或越界：{snapshot.node_id}:{f['rel_path']}")
            if sha256_file(src) != f["sha256"]:
                raise ValueError(f"快照文件摘要不符：{snapshot.node_id}:{f['rel_path']}")
            dest = dest_root / "inputs" / snapshot.node_id / rp
            if not _within(dest, dest_root):
                raise ValueError(f"播种目标越界：{f['rel_path']}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            copied.append(f["rel_path"])
        return copied

    def seed_interface_view(self, snapshots, dest_root: Path, contracts=None, dedup: bool = True) -> dict:
        """v3.6 模块化试点：接口优先展示视图播种。"""
        from .modular import seed_interface_view as _siv
        return _siv(self, snapshots, dest_root, contracts=contracts, dedup=dedup)

    # ---- 复检（既有） ----

    def verify_accepted(self, repo: Repository, nid: str) -> dict:
        """复检：accepted 存储与 DB 登记哈希逐一比对。返回 {ok, tampered:[...]}。"""
        tampered = []
        for art in repo.artifacts_of(nid):
            p = self.accepted_root / nid / f"v{art.version}" / art.rel_path
            if not p.is_file():
                tampered.append({"rel_path": art.rel_path, "version": art.version,
                                 "reason": "accepted 文件缺失"})
            elif sha256_file(p) != art.content_hash:
                tampered.append({"rel_path": art.rel_path, "version": art.version,
                                 "reason": "accepted 内容与登记哈希不符"})
        return {"ok": not tampered, "tampered": tampered}

    def check_workdir_against_accepted(self, repo: Repository, nid: str,
                                       workdir: Path) -> dict:
        """工作目录最新申报工件是否仍与已验收版本一致（防"改了文件继续冒充"）。"""
        arts = repo.artifacts_of(nid)
        if not arts:
            return {"ok": True, "mismatch": []}
        latest_version = arts[-1].version
        mismatch = []
        for art in [a for a in arts if a.version == latest_version]:
            p = workdir / art.rel_path
            if not p.is_file():
                mismatch.append({"rel_path": art.rel_path, "reason": "工作文件缺失"})
            elif sha256_file(p) != art.content_hash:
                mismatch.append({"rel_path": art.rel_path,
                                 "reason": "工作文件内容与已验收版本不符"})
        return {"ok": not mismatch, "mismatch": mismatch}
