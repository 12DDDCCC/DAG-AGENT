"""v3 语义层：建图/分解/尝试生命周期/整合闭合。

完成语义（步骤1验收标准对着写）：
- 孩子全 accepted 只解锁父节点的"整合尝试"（integration），父节点必须整合尝试
  过检才 accepted——"不提前完成"。
- 节点自身执行依赖未全 accepted，不得开始尝试——"不绕过依赖"。
- 未配置验收命令 = fail-closed（提交即失败），不许静默通过。
- 每次状态迁移与对应事件在同一个短事务里落库——"状态与事件一致"。
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Callable, Optional

from .artifacts import ArtifactStore
from .assessment import run_check as _assessment_run_check
from .assessment import default_checker3 as _default_checker3
from .memory import find_similar as _find_similar
from .models import (Artifact, Assessment, Attempt, Dependency, Mission, Node,
                     Review, Snapshot)
from .repository import Repository, now

CheckFn = Callable[[Optional[str], Path], tuple[bool, str]]
Check3Fn = Callable[[Optional[str], Path], tuple[str, str]]

# v3.1 生长闸门参数（粒度：单节点=单次执行者会话能完成的规模）
MAX_STATEMENT_CHARS = 2000
MAX_DEPS = 8


def default_checker(cmd: Optional[str], cwd: Path) -> tuple[bool, str]:
    """步骤2起默认检查器：assessment.run_check（白名单+超时+检查期零写入守卫）。"""
    return _assessment_run_check(cmd, cwd)


class GraphError(Exception):
    """语义拒绝：调用方拿到的都是结构化错误字符串。"""


class Graph:
    def __init__(self, repo: Repository, checker: Optional[CheckFn] = None,
                 checker3: Optional[Check3Fn] = None):
        self.repo = repo
        self.checker = checker or default_checker
        # v3.1 三分检查器：提供时 submit/self_check 走 pass|fail|error；
        # 未提供时回落 bool 检查器（pass|fail），兼容旧调用面。
        self.checker3 = checker3
        self.store = ArtifactStore(Path(repo.db_path).parent)

    # ---- 创建 ----

    def create_mission(self, name: str, brief: str = "",
                       root_check: Optional[str] = None,
                       root_declared_artifacts: tuple[str, ...] = (),
                       anchor: str = "") -> tuple[str, str]:
        import json as _json
        mid = f"m-{hashlib.sha1((name + now()).encode()).hexdigest()[:8]}"
        ts = now()
        with self.repo.tx() as cur:
            cur.execute(
                "INSERT INTO missions(id,name,brief,status,goal_version,anchor,"
                "created_at,updated_at) VALUES(?,?,?,?,1,?,?,?)",
                (mid, name, brief, "active", anchor, ts, ts))
            root_id = self.repo.next_node_id(cur)
            cur.execute(
                "INSERT INTO nodes(id,mission_id,title,statement,parent_id,kind,status,"
                "check_cmd,check_version,declared_artifacts_json,created_at,updated_at)"
                " VALUES(?,?,?,?,NULL,'aggregate','pending',?,1,?,?,?)",
                (root_id, mid, name, brief, root_check,
                 _json.dumps(list(root_declared_artifacts), ensure_ascii=False), ts, ts))
            self.repo.append_event(cur, mid, "create", root_id, name=name,
                                   anchor=anchor or None)
        return mid, root_id

    def add_node(self, mid: str, title: str, statement: str = "",
                 parent_id: Optional[str] = None, deps: tuple[str, ...] = (),
                 check_cmd: Optional[str] = None, kind: str = "task",
                 declared_artifacts: tuple[str, ...] = (),
                 acceptance: Optional[list[dict]] = None,
                 priority: int = 0, group_id: Optional[str] = None,
                 group_order: int = 0, switch_after: int = 1,
                 exam_files: tuple[str, ...] = ()) -> str:
        """v3.1 生长闸门（六条，全过才落库）：
        ①三件套（title/statement 必填）②无环 ③归属（父必填，无主节点禁止）
        ④依赖可满足（不得依赖 cancelled 节点）⑤粒度（陈述≤2000字、deps≤8）
        ⑥查重（等价节点已存在→打回引用，不长新的）。
        T6：exam_files=考卷包显式清单——清单文件内容哈希进考卷摘要
        （漂移必拒）；空=默认扫描闭包。声明时缺失文件硬拒（出题错误）。
        """
        ts = now()
        with self.repo.tx() as cur:
            mission = self._mission_active(cur, mid)
            del mission
            # ① 三件套齐全 + ⑤ 粒度
            if not title.strip():
                raise GraphError("生长闸门①：title 必填（三件套不齐）")
            if not (statement or "").strip() and check_cmd is None and acceptance is None:
                raise GraphError("生长闸门①：statement 与验收至少给其一（三件套不齐）")
            if len(statement or "") > MAX_STATEMENT_CHARS:
                raise GraphError(f"生长闸门⑤：陈述 {len(statement)} 字超上限"
                                 f" {MAX_STATEMENT_CHARS}——粒度过大，先拆")
            if len(deps) > MAX_DEPS:
                raise GraphError(f"生长闸门⑤：依赖 {len(deps)} 条超上限 {MAX_DEPS}")
            # ③ 归属：无主节点禁止（根由 create_mission 建立）
            if parent_id is None:
                raise GraphError("生长闸门③：必须归属父节点——无主节点禁止")
            prow = cur.execute("SELECT * FROM nodes WHERE id=? AND mission_id=?",
                               (parent_id, mid)).fetchone()
            if prow is None:
                raise GraphError(f"父节点不存在：{parent_id}")
            parent = parent_id
            # ④ 依赖可满足 + ② 无环防御（新边只指向已存在节点，结构上无环；
            #    此处再走依赖闭包防御，防未来语义变化引入环）
            dep_rows = {}
            for d in deps:
                if d == parent:
                    raise GraphError("孩子依赖父节点会死锁：归属不是依赖")
                row = cur.execute(
                    "SELECT id, status, group_id FROM nodes WHERE id=?"
                    " AND mission_id=?", (d, mid)).fetchone()
                if row is None:
                    raise GraphError(f"依赖节点不存在：{d}")
                if row["status"] == "cancelled":
                    # v3.2：OR 组成员败选（lost_race）——组有赢家则依赖可满足
                    # （认领时解析到赢家）；无赢家才是悬空依赖
                    ok_dep = False
                    if row["group_id"]:
                        prow2 = cur.execute(
                            "SELECT parent_id FROM nodes WHERE id=?",
                            (d,)).fetchone()
                        if prow2 and prow2["parent_id"]:
                            ok_dep = self._group_winner(
                                cur, prow2["parent_id"], row["group_id"]) is not None
                    if not ok_dep:
                        raise GraphError(f"生长闸门④：依赖 {d} 已取消——"
                                         f"不可满足（悬空依赖）")
                dep_rows[d] = row
            nid = self.repo.next_node_id(cur)
            if deps:
                seen = set()
                stack = list(deps)
                while stack:
                    cur_id = stack.pop()
                    if cur_id in seen:
                        continue
                    seen.add(cur_id)
                    if cur_id == nid:
                        raise GraphError("生长闸门②：依赖闭包包含自身（成环）")
                    for r in cur.execute(
                            "SELECT depends_on FROM dependencies WHERE node_id=?",
                            (cur_id,)).fetchall():
                        stack.append(r["depends_on"])
            # ⑥ 查重：等价节点已存在 → 打回引用
            dup = _find_similar(self.repo, mid, title, statement)
            if dup:
                cands = ", ".join(f"{d['id']}({d['title']}/{d['status']})"
                                  for d in dup[:3])
                raise GraphError(f"生长闸门⑥：疑似等价节点已存在——{cands}；"
                                 f"请先 recall 检索并复用，或改写目标后再提议")
            import json as _json
            # v3.1：受限枚举清单编译为可执行考卷（MCP 层已做，直连层同样生效）
            if acceptance is not None and check_cmd is None:
                from .acceptance import compile_all as _compile_all
                check_cmd = " && ".join(_compile_all(acceptance))
            # T6：显式清单声明时逐件存在校验（缺件=出题错误，硬拒）
            for rel in exam_files:
                if not Path(rel).is_file():
                    raise GraphError(f"考卷包显式清单文件缺失：{rel}")
            # T6'：清单必须覆盖考卷实际执行的业务入口（漏入口=未冻结的
            # 判卷代码仍在跑，Codex t-review 例3：漏 exam.py → BAD accepted）
            if exam_files:
                from .acceptance import verify_exam_files_cover as _vef
                ok_cov, missing = _vef(check_cmd, acceptance, exam_files)
                if not ok_cov:
                    raise GraphError(
                        f"考卷包显式清单未覆盖实际执行入口：{missing}——"
                        "业务判卷代码必须在清单内（完整冻结而非按清单计算摘要）")
            cur.execute(
                "INSERT INTO nodes(id,mission_id,title,statement,parent_id,kind,status,"
                "check_cmd,check_version,acceptance_json,priority,"
                "declared_artifacts_json,group_id,group_order,switch_after,"
                "exam_files_json,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?)",
                (nid, mid, title, statement, parent, kind, "pending", check_cmd,
                 _json.dumps(acceptance, ensure_ascii=False) if acceptance else None,
                 int(priority),
                 _json.dumps(list(declared_artifacts), ensure_ascii=False),
                 group_id, int(group_order), max(int(switch_after or 1), 1),
                 _json.dumps(list(exam_files), ensure_ascii=False), ts, ts))
            for d in deps:
                cur.execute(
                    "INSERT INTO dependencies(node_id,depends_on,created_at) VALUES(?,?,?)",
                    (nid, d, ts))
            self.repo.append_event(cur, mid, "add_node", nid, title=title,
                                   parent=parent, deps=list(deps),
                                   priority=int(priority))
            if parent is not None:
                prow = cur.execute("SELECT * FROM nodes WHERE id=?",
                                   (parent,)).fetchone()
                if prow["status"] in ("pending", "in_work"):
                    run = cur.execute(
                        "SELECT id FROM attempts WHERE node_id=? AND status='running'",
                        (parent,)).fetchone()
                    if run:
                        cur.execute(
                            "UPDATE attempts SET status='cancelled', ended_at=?"
                            " WHERE id=?", (ts, run["id"]))
                        self.repo.append_event(cur, mid, "attempt_cancelled",
                                               parent, attempt=run["id"],
                                               reason="dynamic_child")
                    cur.execute(
                        "UPDATE nodes SET status='waiting_children', updated_at=?"
                        " WHERE id=?", (ts, parent))
                    self.repo.append_event(cur, mid, "node_to_waiting", parent,
                                           reason="dynamic_child")
        return nid

    def decompose(self, mid: str, node_id: str,
                  children: list[dict], integration_check: Optional[str] = None) -> list[str]:
        """受控生长：一次事务里建孩子+父转 waiting_children；父原有依赖原样保留。

        v3.1 闸门：孩子 statement 必填（三件套齐全）；同提议内孩子不得重复。
        """
        ts = now()
        with self.repo.tx() as cur:
            node = self._node(cur, mid, node_id)
            if node.status not in ("pending", "in_work", "waiting_children"):
                raise GraphError(f"节点状态 {node.status} 不可分解")
            seen_titles: set[str] = set()
            for spec in children:
                if not (spec.get("statement") or "").strip():
                    raise GraphError(f"生长闸门①：孩子 {spec.get('title')} 缺陈述"
                                     "（三件套不齐）")
                if len(spec.get("statement", "")) > MAX_STATEMENT_CHARS:
                    raise GraphError(f"生长闸门⑤：孩子 {spec.get('title')} 陈述超上限"
                                     f"——粒度过大，先拆")
                norm = __import__("re").sub(r"\s+", "",
                                            (spec.get("title") or "").lower())
                if norm and norm in seen_titles:
                    raise GraphError(f"生长闸门⑥：提议内孩子重复：{spec.get('title')}")
                seen_titles.add(norm)
            running = cur.execute(
                "SELECT id FROM attempts WHERE node_id=? AND status='running'",
                (node_id,)).fetchone()
            if running:  # 提案即释放执行位
                cur.execute(
                    "UPDATE attempts SET status='cancelled', ended_at=? WHERE id=?",
                    (ts, running["id"]))
                self.repo.append_event(cur, mid, "attempt_cancelled", node_id,
                                       attempt=running["id"],
                                       reason="proposed_decomposition")
            ids: list[str] = []
            for spec in children:
                cid = self.repo.next_node_id(cur)
                for d in spec.get("deps", ()):
                    if d == cid or d == node_id:
                        raise GraphError(f"孩子 {spec.get('title')} 的依赖非法：{d}")
                    row = cur.execute("SELECT id FROM nodes WHERE id=? AND mission_id=?",
                                      (d, mid)).fetchone()
                    if row is None:
                        raise GraphError(f"孩子依赖不存在：{d}")
                import json as _json
                child_cmd = spec.get("check_cmd")
                child_acc = spec.get("acceptance")
                if child_cmd is None and child_acc:
                    from .acceptance import compile_all as _cacc
                    child_cmd = " && ".join(_cacc(child_acc))  # 展示用；判卷走 items
                cur.execute(
                    "INSERT INTO nodes(id,mission_id,title,statement,parent_id,kind,status,"
                    "check_cmd,check_version,acceptance_json,priority,"
                    "declared_artifacts_json,group_id,group_order,switch_after,"
                    "created_at,updated_at)"
                    " VALUES(?,?,?,?,?,'task','pending',?,1,?,?,?,?,?,?,?,?)",
                    (cid, mid, spec.get("title", ""), spec.get("statement", ""),
                     node_id, child_cmd,
                     _json.dumps(child_acc, ensure_ascii=False) if child_acc else None,
                     int(spec.get("priority", 0)),
                     _json.dumps(list(spec.get("declared_artifacts", ())),
                                 ensure_ascii=False),
                     spec.get("group_id"),
                     int(spec.get("group_order", 0)),
                     max(int(spec.get("switch_after", 1) or 1), 1), ts, ts))
                for d in spec.get("deps", ()):
                    cur.execute(
                        "INSERT INTO dependencies(node_id,depends_on,created_at)"
                        " VALUES(?,?,?)", (cid, d, ts))
                ids.append(cid)
            if integration_check and node.check_cmd is None:
                cur.execute(
                    "UPDATE nodes SET check_cmd=?, check_version=check_version+1,"
                    " updated_at=? WHERE id=?", (integration_check, ts, node_id))
            cur.execute(
                "UPDATE nodes SET status='waiting_children', updated_at=? WHERE id=?",
                (ts, node_id))
            self.repo.append_event(cur, mid, "decompose", node_id, children=ids)
        return ids

    # ---- A-G1：自动生长权威应用（原子事务） ----

    def _node_depth(self, cur, node_id: str) -> int:
        """节点深度（root=0）：沿 parent 链向上计数。"""
        depth = 0
        cur_id = node_id
        while cur_id and depth < 64:
            row = cur.execute("SELECT parent_id FROM nodes WHERE id=?",
                              (cur_id,)).fetchone()
            if row is None or row["parent_id"] is None:
                break
            cur_id = row["parent_id"]
            depth += 1
        return depth

    def apply_auto_split(self, mid: str, node_id: str, source_attempt_id: str,
                         proposal_row_id: str, children_spec: list[dict],
                         fingerprint: dict,
                         max_splits_per_mission: int = 5,
                         max_depth: int = 5,
                         node_cap: int | None = None,
                         apply_gate=None,
                         source_binding_json: str | None = None) -> list[str]:
        """自动生长权威应用：单事务完成核验+落图+计数+事件（A-G1 §4）。

        事务内逐项重核（preflight 在事务外已做，此处为最终核对）：
        mission 状态/目标版本、父节点 waiting_split、源尝试 split 终态、
        认领指纹（goal/revision/check 版本与摘要——检测后发生 revise/
        repair/change_goal 一律拒绝）、幂等（proposal 仍 queued）、护栏
        （每节点≤1/每任务≤max/树深/孩子数/node_cap）、查重（批内规范化
        同名 + 与库内已有节点）、孩子考卷 digest 冻结重核（出题时刻
        _frozen_digest vs 落库时刻——体检后漂移拒绝）、运行权限 gate
        （停止/kill 后撤销应用权，事务内读停止信号与 SQLite 写锁串行化）。
        任一核验失败 → 事务回滚（无半图/半计数），GraphError 带原因。
        成功：孩子+依赖落库（完整字段；外部依赖锁定父认领快照
        expected_version）、父转 waiting_children、proposal 落
        applied+实际新增数、split_applied 事件。
        """
        import json as _json
        if not (2 <= len(children_spec) <= 5):
            raise GraphError(f"孩子数须 2–5，收到 {len(children_spec)}")
        ts = now()
        with self.repo.tx() as cur:
            # --- 运行权限 gate（F2/R2）+ 考卷核验（F1/R1）：全部在
            # BEGIN IMMEDIATE 写锁内。R2：gate 读库内 run_control 旗标
            # ——stop 经 repo.tx 写同一旗标，两侧共用写锁构成线性化点
            # （Event.set 在锁外，不构成）；R1：考卷摘要在事务内**重算**
            # （Codex 复核：事务外算好仅比较，算完到开事务之间漂移检测
            # 不到）——重算含考卷文件读取，属短字节读，不违反"事务内
            # 禁模型调用/检查"约束。
            if apply_gate is not None and not apply_gate():
                raise GraphError("运行权限已撤销（停止/kill）：旧提案拒绝"
                                 "应用，不落图")
            from .acceptance import (compute_check_digest as _ccd,
                                     business_entry_paths as _bep,
                                     verify_exam_files_cover as _vef,
                                     AcceptanceError)
            frozen_digests: list[str] = []
            for i, spec in enumerate(children_spec):
                if not spec.get("_frozen_digest"):
                    raise GraphError(
                        f"孩子{i}({spec.get('title')}) 出题产物缺冻结考卷摘要"
                        "（_frozen_digest）——体检后未锁定的子卷不得应用")
                exam_files_i = tuple(spec.get("exam_files") or ())
                try:
                    entries = _bep(check_cmd=spec.get("check_cmd"),
                                   acceptance=spec.get("acceptance"))
                except AcceptanceError as e:
                    raise GraphError(
                        f"孩子{i}({spec.get('title')}) 考卷入口解析失败：{e}")
                # R1：入口识别不按扩展名猜（business_entry_paths——
                # .pyw/无扩展名/目录都算入口）；引用本地文件入口而清单为
                # 空=绕过冻结，拒绝。python -c 内联考卷无文件引用，可空。
                if entries and not exam_files_i:
                    raise GraphError(
                        f"孩子{i}({spec.get('title')}) 考卷引用本地文件入口"
                        f" {[str(e) for e in entries[:3]]} 但未声明"
                        " exam_files——出题产物必须显式冻结考卷包清单")
                for rel in exam_files_i:
                    if not Path(rel).is_file():
                        raise GraphError(
                            f"孩子{i} 考卷包清单文件缺失：{rel}")
                try:
                    ok_cov, missing = _vef(spec.get("check_cmd"),
                                           spec.get("acceptance"),
                                           exam_files_i)
                except AcceptanceError as e:
                    raise GraphError(
                        f"孩子{i}({spec.get('title')}) 考卷覆盖校验失败：{e}")
                if not ok_cov:
                    raise GraphError(
                        f"孩子{i}({spec.get('title')}) 考卷包显式清单未覆盖"
                        f"实际执行入口：{missing}")
                try:
                    digest_now = _ccd(
                        check_cmd=spec.get("check_cmd"),
                        acceptance=spec.get("acceptance"),
                        exam_files=exam_files_i)
                except AcceptanceError as e:
                    raise GraphError(
                        f"孩子{i}({spec.get('title')}) 考卷摘要计算失败：{e}")
                if digest_now != spec["_frozen_digest"]:
                    raise GraphError(
                        f"孩子{i}({spec.get('title')}) 考卷内容已漂移"
                        "（出题体检后摘要变化）：拒绝应用，无半图")
                frozen_digests.append(digest_now)
            # --- 幂等：提案仍 queued（并发/重复只应用一次） ---
            prow = cur.execute(
                "SELECT status, fingerprint_json FROM split_proposals"
                " WHERE id=?", (proposal_row_id,)).fetchone()
            if prow is None:
                raise GraphError(f"提案不存在：{proposal_row_id}")
            if prow["status"] != "queued":
                raise GraphError(f"提案状态 {prow['status']} 不可应用"
                                 "（重复/并发提案只应用一次）")
            # --- mission / 父节点 / 源尝试 ---
            mission = self._mission_active(cur, mid)
            del mission
            gv = self._goal_version(cur, mid)
            if gv != fingerprint.get("goal_version"):
                raise GraphError("目标版本已变更（修订/改目标后旧提案拒绝）")
            parent = self._node(cur, mid, node_id)
            if parent.status != "waiting_split":
                raise GraphError(
                    f"父节点状态 {parent.status} 非 waiting_split"
                    "（已取消/已应用/已被人工处置）")
            if parent.check_cmd is None and parent.acceptance_json is None:
                raise GraphError("父节点无正式考卷：拒绝自动生长"
                                 "（交出题方处理，父卷不得弱化或代拟）")
            att = cur.execute(
                "SELECT status, category FROM attempts WHERE id=?",
                (source_attempt_id,)).fetchone()
            # split=无交付直接激活；failed=有交付按原卷判 FAIL 后激活
            # （产物 FAIL 事实保留，不因激活改写）
            if att is None or att["status"] not in ("split", "failed"):
                raise GraphError("源尝试非 split/failed 终态"
                                 "（迟到/越权提案拒绝）")
            if att["status"] == "split" and att["category"] != "split":
                raise GraphError("源尝试终态分类异常（迟到提案拒绝）")
            # --- 认领指纹重核（检测后 revise/repair/kill 一律拒绝） ---
            if parent.check_version != fingerprint.get("check_version"):
                raise GraphError("考卷已维修（check_version 变更）：提案过期")
            if parent.revision != fingerprint.get("node_revision"):
                raise GraphError("节点修订已变更：提案过期")
            from .acceptance import compute_check_digest as _ccd
            digest_now = _ccd(
                check_cmd=parent.check_cmd,
                acceptance=_json.loads(parent.acceptance_json)
                if parent.acceptance_json else None,
                exam_files=self._exam_files_of(parent))
            if digest_now != fingerprint.get("check_digest"):
                raise GraphError("考卷内容已变更：提案过期")
            # --- 护栏（全部事务内；计数=split_proposals 持久表，重启不清零）---
            node_splits = cur.execute(
                "SELECT COUNT(*) AS c FROM split_proposals WHERE node_id=?"
                " AND status='applied'", (node_id,)).fetchone()["c"]
            if node_splits >= 1:
                raise GraphError("护栏：每节点生命周期成功自动拆解 ≤1")
            mission_splits = cur.execute(
                "SELECT COUNT(*) AS c FROM split_proposals WHERE mission_id=?"
                " AND status='applied'", (mid,)).fetchone()["c"]
            if mission_splits >= max_splits_per_mission:
                raise GraphError(
                    f"护栏：每 mission 成功自动拆解 ≥"
                    f"{max_splits_per_mission}（当前 {mission_splits}）")
            parent_depth = self._node_depth(cur, node_id)
            if parent_depth + 1 > max_depth:
                raise GraphError(
                    f"护栏：树深超 {max_depth}（父深 {parent_depth}）")
            if node_cap is not None:
                n_nodes = cur.execute(
                    "SELECT COUNT(*) AS c FROM nodes WHERE mission_id=?",
                    (mid,)).fetchone()["c"]
                if n_nodes + len(children_spec) > node_cap:
                    raise GraphError(
                        f"护栏：node_cap {node_cap}（当前 {n_nodes}+新增"
                        f"{len(children_spec)}）")
            # --- 查重闸门（F6：批内规范化同名先行 + 与库内已有节点；
            # 复用优先；第一期不支持自动复用→明确拒绝） ---
            seen_titles: set[str] = set()
            for spec in children_spec:
                import re as _re_t
                norm = _re_t.sub(r"\s+", "",
                                 (spec.get("title") or "").lower())
                if norm and norm in seen_titles:
                    raise GraphError(
                        f"生长闸门⑥：提案内孩子同名：{spec.get('title')}"
                        "——批内查重拒绝（local_id 不同不豁免）")
                seen_titles.add(norm)
                dup = _find_similar(self.repo, mid,
                                    spec.get("title", ""),
                                    spec.get("statement", ""))
                if dup:
                    cands = ", ".join(
                        f"{d['id']}({d['title']}/{d['status']})"
                        for d in dup[:3])
                    raise GraphError(
                        f"查重命中已有节点：{cands}——复用优先于生长；"
                        "第一期自动复用未支持，提案拒绝（明确原因，不伪造"
                        "产物 FAIL）")
            # --- 外部依赖继承（F3/R3）：孩子获得父认领时批准的完整输入
            # 契约——快照 id + manifest 摘要 + 完整文件清单，持久到
            # dep_bindings；认领时逐项核对（同 id 换清单/清空 files/
            # 篡改 manifest 一律拒绝，不得按损坏清单播种零件）
            att_row0 = cur.execute(
                "SELECT input_binding_json FROM attempts WHERE id=?",
                (source_attempt_id,)).fetchone()
            _att_binding = (att_row0["input_binding_json"] if att_row0
                            else "[]")
            if source_binding_json is not None:
                # 小问题3（Codex 三轮）：可覆盖入口在事务内核对——传入
                # 绑定与源 attempt 认领绑定不一致=拒绝（绑定的唯一权威
                # =认领时刻账本，防伪造空绑定绕过）
                if (_json.loads(source_binding_json or "[]")
                        != _json.loads(_att_binding or "[]")):
                    raise GraphError(
                        "传入 source_binding 与源 attempt 认领绑定不一致："
                        "拒绝覆盖（以认领账本为准）")
            source_binding_json = _att_binding
            from .artifacts import manifest_hash_of as _mho
            external_deps: list[dict] = []
            for b in _json.loads((source_binding_json or "[]")):
                if b.get("role") == "dep" and b.get("source"):
                    rec = {
                        "source": b["source"],
                        "snapshot_id": b.get("snapshot_id"),
                        "manifest_hash": b.get("manifest_hash", ""),
                        "files": list(b.get("files") or []),
                        "artifact_version": b.get("artifact_version"),
                        "files_meta": []}
                    if rec["snapshot_id"] is not None:
                        _srow = cur.execute(
                            "SELECT manifest_hash, files_json FROM snapshots"
                            " WHERE id=?", (rec["snapshot_id"],)).fetchone()
                        if _srow is None:
                            raise GraphError(
                                f"父绑定快照不存在：{b['source']}"
                                f"(snap {rec['snapshot_id']})——拒绝应用")
                        # F2（Codex 三轮）：apply 时即做完整元信息固化——
                        # 快照行自洽（清单重算=存储 manifest）且与父认领
                        # 时刻的 manifest 一致；全量文件元信息进契约
                        _meta = _json.loads(_srow["files_json"] or "[]")
                        if _meta and _mho(_meta) != _srow["manifest_hash"]:
                            raise GraphError(
                                f"父绑定快照自洽性校验失败：{b['source']}"
                                "（清单重算≠存储 manifest）——拒绝应用")
                        if _srow["manifest_hash"] != rec["manifest_hash"]:
                            raise GraphError(
                                f"父绑定快照已相对认领时刻变化：{b['source']}"
                                f"（{rec['manifest_hash'][:12]}→"
                                f"{_srow['manifest_hash'][:12]}）——拒绝应用")
                        rec["files_meta"] = _meta
                    external_deps.append(rec)
            # --- 孩子落库（真实 ID 分配，不猜编号；局部依赖映射） ---
            # next_node_id 基于 COUNT(*)：必须分配一个即插入一个，否则
            # 两个孩子在插入前分配会拿到同一 ID（实测成环误报）。
            # 这里先取基准号、按 next_node_id 同一规则顺序分配——
            # BEGIN IMMEDIATE 写锁下事务内计数稳定，等价且可预映射 local_id。
            base = int(self.repo.next_node_id(cur)[1:])
            ids: list[str] = [f"n{base + i}"
                              for i in range(len(children_spec))]
            id_map: dict[str, str] = {
                spec.get("source_local_id", ""): nid
                for spec, nid in zip(children_spec, ids)}
            for i_spec, (spec, cid) in enumerate(zip(children_spec, ids)):
                dep_ids: list[str] = []
                for d in spec.get("deps", ()):
                    if d not in id_map:
                        raise GraphError(
                            f"局部依赖映射失败：{d}（须为提案内 local_id）")
                    dep_ids.append(id_map[d])
                if cid in dep_ids:
                    raise GraphError("孩子依赖自身（成环）")
                if node_id in dep_ids:
                    raise GraphError("孩子依赖父节点会死锁")
                ext_srcs = [e["source"] for e in external_deps]
                for d in list(dep_ids) + ext_srcs:
                    row = cur.execute(
                        "SELECT id, status FROM nodes WHERE id=? AND mission_id=?",
                        (d, mid)).fetchone()
                    if row is None:
                        raise GraphError(f"依赖节点不存在：{d}")
                    if row["status"] == "cancelled":
                        raise GraphError(f"生长闸门④：依赖 {d} 已取消")
                if len(dep_ids) + len(ext_srcs) > MAX_DEPS:
                    raise GraphError(
                        f"{spec.get('title')} 依赖超上限 {MAX_DEPS}")
                exam_files = tuple(spec.get("exam_files") or ())
                for rel in exam_files:
                    if not Path(rel).is_file():
                        raise GraphError(f"考卷包显式清单文件缺失：{rel}")
                if exam_files:
                    from .acceptance import verify_exam_files_cover as _vef
                    ok_cov, missing = _vef(spec.get("check_cmd"),
                                           spec.get("acceptance"),
                                           exam_files)
                    if not ok_cov:
                        raise GraphError(
                            f"考卷包显式清单未覆盖实际执行入口：{missing}")
                cur.execute(
                    "INSERT INTO nodes(id,mission_id,title,statement,parent_id,"
                    "kind,status,check_cmd,check_version,acceptance_json,"
                    "priority,declared_artifacts_json,exam_files_json,"
                    "created_at,updated_at)"
                    " VALUES(?,?,?,?,?,'task','pending',?,1,?,?,?,?,?,?)",
                    (cid, mid, spec.get("title", ""),
                     spec.get("statement", ""), node_id,
                     spec.get("check_cmd"),
                     _json.dumps(spec.get("acceptance"), ensure_ascii=False)
                     if spec.get("acceptance") else None,
                     int(spec.get("priority", 0)),
                     _json.dumps(list(spec.get("declared_artifacts", ())),
                                 ensure_ascii=False),
                     _json.dumps(list(exam_files), ensure_ascii=False),
                     ts, ts))
                for d in dep_ids:
                    cur.execute(
                        "INSERT INTO dependencies(node_id,depends_on,"
                        "created_at) VALUES(?,?,?)", (cid, d, ts))
                for e in external_deps:
                    # F3/R3：锁定父认领时刻快照 + 持久完整绑定契约——
                    # 孩子认领时按契约逐项核对（失效/缺失/篡改=认领拒绝，
                    # 人工处置；空清单不得解释成"合法无输入"）
                    cur.execute(
                        "INSERT INTO dependencies(node_id,depends_on,"
                        "expected_version,created_at) VALUES(?,?,?,?)",
                        (cid, e["source"], e["snapshot_id"], ts))
                    if e["snapshot_id"] is not None:
                        cur.execute(
                            "INSERT OR REPLACE INTO dep_bindings("
                            "node_id,depends_on,snapshot_id,manifest_hash,"
                            "files_json,artifact_version,files_meta_json)"
                            " VALUES(?,?,?,?,?,?,?)",
                            (cid, e["source"], e["snapshot_id"],
                             e["manifest_hash"],
                             _json.dumps(e["files"], ensure_ascii=False),
                             e["artifact_version"],
                             _json.dumps(e["files_meta"],
                                         ensure_ascii=False)))
                # R1：出题时刻冻结的考卷摘要持久到孩子——认领时重算比对，
                # 关闭"apply 验过→认领前漂移"窗口
                cur.execute(
                    "INSERT INTO node_exam_freeze(node_id,exam_digest,"
                    "proposal_id,frozen_at) VALUES(?,?,?,?)",
                    (cid, frozen_digests[i_spec], proposal_row_id, ts))
            # --- 父转 waiting_children（父考卷原样保留——不新增弱化检查）---
            cur.execute(
                "UPDATE nodes SET status='waiting_children', updated_at=?"
                " WHERE id=?", (ts, node_id))
            # --- 提案回执 + 成功计数（同事务；拒绝不计数） ---
            cur.execute(
                "UPDATE split_proposals SET status='applied',"
                " children_created_json=?, nodes_added=?, decided_at=?"
                " WHERE id=?",
                (_json.dumps(ids, ensure_ascii=False), len(ids), ts,
                 proposal_row_id))
            self.repo.append_event(
                cur, mid, "split_applied", node_id,
                proposal=proposal_row_id, children=ids,
                source_attempt=source_attempt_id,
                actual_nodes_added=len(ids),
                external_deps=external_deps)
        return ids

    @staticmethod
    def _group_members(cur, parent_id: str) -> dict[str, list]:
        """同父下的 OR 组：{group_id: [成员按 (group_order,id) 升序]}。"""
        groups: dict[str, list] = {}
        for r in cur.execute(
                "SELECT id, group_id, group_order FROM nodes WHERE parent_id=?"
                " AND group_id IS NOT NULL AND status!='cancelled'"
                " ORDER BY group_order, id", (parent_id,)).fetchall():
            groups.setdefault(r["group_id"], []).append(r["id"])
        return groups

    @staticmethod
    def _group_winner(cur, parent_id: str, group_id: str):
        """组赢家（原子：accepted 的成员即赢家；由提交事务保证唯一）。"""
        row = cur.execute(
            "SELECT id FROM nodes WHERE parent_id=? AND group_id=?"
            " AND status='accepted' LIMIT 1", (parent_id, group_id)).fetchone()
        return row["id"] if row else None

    @staticmethod
    def _fail_count(cur, nid: str) -> int:
        return cur.execute(
            "SELECT COUNT(*) AS c FROM attempts WHERE node_id=?"
            " AND status='failed'", (nid,)).fetchone()["c"]

    def _member_dispatchable(self, cur, node) -> bool:
        """激活规则：成员 i 可派发 ⇔ 排在它前面的成员都已"耗尽"
        （各自产物 FAIL 数 ≥ switch_after 且未 accepted）。ERROR 不算路线失败
        （默认走维修）；成员 accepted 即组已有赢家（不该再到这里）。"""
        if node.group_id is None:
            return True
        switch_after = max(int(node.switch_after or 1), 1)
        for r in cur.execute(
                "SELECT id, status FROM nodes WHERE parent_id=? AND group_id=?"
                " AND status!='cancelled' AND (group_order < ? OR"
                " (group_order = ? AND id < ?))",
                (node.parent_id, node.group_id, node.group_order,
                 node.group_order, node.id)).fetchall():
            if r["status"] == "accepted":
                return False  # 已有赢家在前面——本支不该再派
            if self._fail_count(cur, r["id"]) < switch_after:
                return False  # 前支还有余量，未到切换阈值
        return True

    def _integration_satisfied(self, cur, node_id: str) -> bool:
        """整合解锁判定（AND/OR）：普通孩子（非 cancelled）须全 accepted；
        每个 OR 组须存在赢家。非赢家成员 cancelled(lost_race) 不算缺口。"""
        kids = cur.execute(
            "SELECT id, group_id, status FROM nodes WHERE parent_id=?"
            " AND status!='cancelled'", (node_id,)).fetchall()
        if not kids:
            return False
        groups = self._group_members(cur, node_id)
        won: set[str] = set()
        for gid in groups:
            w = self._group_winner(cur, node_id, gid)
            if w is None:
                return False
            won.add(gid)
        for k in kids:
            if k["group_id"] is not None:
                continue  # 组孩子按组判定
            if k["status"] != "accepted":
                return False
        return True

    def _collect_group_win(self, cur, mid: str, node_id: str):
        """赢家原子选定：node accepted 且属某组 → 同事务内其余成员取消
        （在途尝试取消、节点 cancelled[lost_race]、工件与证据历史保留）。"""
        node = self._node(cur, mid, node_id)
        if node.group_id is None:
            return
        winner = self._group_winner(cur, node.parent_id, node.group_id)
        if winner != node_id:
            return  # 后到者：前面的赢家已定（理论上已进不了提交）
        ts = now()
        for r in cur.execute(
                "SELECT id FROM nodes WHERE parent_id=? AND group_id=?"
                " AND id!=? AND status NOT IN ('cancelled','accepted')",
                (node.parent_id, node.group_id, node_id)).fetchall():
            run = cur.execute(
                "SELECT id FROM attempts WHERE node_id=? AND status='running'",
                (r["id"],)).fetchone()
            if run:
                cur.execute(
                    "UPDATE attempts SET status='cancelled', category='cancel',"
                    " ended_at=? WHERE id=?", (ts, run["id"]))
                self.repo.append_event(cur, mid, "attempt_cancelled", r["id"],
                                       attempt=run["id"], reason="lost_race")
            cur.execute("UPDATE nodes SET status='cancelled', updated_at=?"
                        " WHERE id=?", (ts, r["id"]))
        self.repo.append_event(cur, mid, "group_won", node_id,
                               group=node.group_id, parent=node.parent_id)

    def _resolve_dep_effective(self, cur, dep_id: str) -> str:
        """依赖解析：依赖 OR 组成员 → 组赢家；无赢家则返回自身（未满足态）。"""
        row = cur.execute("SELECT parent_id, group_id FROM nodes WHERE id=?",
                          (dep_id,)).fetchone()
        if row is None or row["group_id"] is None:
            return dep_id
        winner = self._group_winner(cur, row["parent_id"], row["group_id"])
        return winner or dep_id

    # ---- 尝试生命周期 ----

    # ---- v3.2：输入绑定与认领指纹（协议02/03） ----

    @staticmethod
    def _binding_hash(bindings: list[dict]) -> str:
        import json as _json
        return hashlib.sha256(_json.dumps(
            bindings, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def _resolve_bindings(self, cur, mid: str, node_id: str,
                          integration: bool, dep_rows: dict) -> list[dict]:
        """认领事务内解析输入：叶子=执行依赖；整合=非 cancelled 孩子（v3.2）。
        每个来源取最新 valid 快照；accepted 且无工件 → 空绑定（无文件可播）；
        expected_version 指定快照号则精确绑定。缺有效快照 → 认领拒绝。"""
        import json as _json
        if integration:
            # v3.2 第3步：整合输入=普通孩子 + 各 OR 组赢家（败者不入输入）
            # R2：整合节点自身的执行 deps 同样绑定（不能只取孩子）
            sources: list[tuple[str, str, None]] = []
            groups = self._group_members(cur, node_id)
            for gid, members in groups.items():
                w = self._group_winner(cur, node_id, gid)
                if w is None:
                    raise GraphError(f"整合认领但组 {gid} 无赢家（状态机不一致）")
                sources.append((w, "child", None))
            for r in cur.execute(
                    "SELECT id FROM nodes WHERE parent_id=? AND group_id IS NULL"
                    " AND status!='cancelled'", (node_id,)).fetchall():
                sources.append((r["id"], "child", None))
            for dep in cur.execute(
                    "SELECT depends_on, expected_version FROM dependencies"
                    " WHERE node_id=?", (node_id,)).fetchall():
                eff = self._resolve_dep_effective(cur, dep["depends_on"])
                sources.append((eff, "dep", dep["expected_version"]))
        else:
            sources = [(d, "dep", dep_rows.get(d, {}).get("expected_version")
                        if isinstance(dep_rows.get(d), dict) else dep_rows.get(d))
                       for d in dep_rows]
        bindings: list[dict] = []
        for src, role, expected in sources:
            srow = cur.execute("SELECT status FROM nodes WHERE id=?", (src,)).fetchone()
            if srow is None:
                raise GraphError(f"输入来源不存在：{src}")
            if srow["status"] != "accepted":
                raise GraphError(f"输入来源未 accepted：{src}")
            if expected is not None:
                snap = cur.execute(
                    "SELECT * FROM snapshots WHERE node_id=? AND id=? AND valid=1",
                    (src, expected)).fetchone()
                if snap is None:
                    # R1：指定版本缺失=拒绝认领，禁止静默降级空绑定
                    raise GraphError(
                        f"依赖指定快照 {expected}（{src}）不存在或已失效："
                        f"拒绝认领——禁止降级为空绑定，请显式更新依赖声明")
            else:
                snap = cur.execute(
                    "SELECT * FROM snapshots WHERE node_id=? AND valid=1"
                    " ORDER BY id DESC LIMIT 1", (src,)).fetchone()
            if snap is None:
                bindings.append({"source": src, "snapshot_id": None,
                                 "manifest_hash": "", "files": [], "role": role})
                continue
            bindings.append({
                "source": src, "snapshot_id": snap["id"],
                "manifest_hash": snap["manifest_hash"],
                "files": _json.loads(snap["files_json"] or "[]")
                         and [f["rel_path"] for f in _json.loads(snap["files_json"])],
                "artifact_version": snap["artifact_version"], "role": role})
        return bindings

    @staticmethod
    def _exam_files_of(node) -> tuple[str, ...]:
        """T6：节点考卷包显式清单（空=扫描闭包默认）。
        兼容 Node dataclass（属性访问）与 sqlite3.Row（下标访问）——
        Row 无属性协议，getattr 失败须回退下标，否则显式清单静默丢成空。"""
        import json as _jf
        raw = getattr(node, "exam_files_json", None)
        if raw is None:
            try:
                raw = node["exam_files_json"]
            except Exception:
                return ()
        try:
            return tuple(_jf.loads(raw or "[]"))
        except Exception:
            return ()

    def start_attempt(self, mid: str, node_id: str, backend: str = "stub") -> str:
        ts = now()
        aid = f"a-{uuid.uuid4().hex[:8]}"
        with self.repo.tx() as cur:
            node = self._node(cur, mid, node_id)
            if node.status in ("in_work", "submitted"):
                raise GraphError(f"节点已有在途尝试（{node.status}）")
            if node.status in ("accepted", "cancelled", "blocked"):
                raise GraphError(f"节点状态 {node.status} 不可开始尝试")
            kid_count = cur.execute(
                "SELECT COUNT(*) AS c FROM nodes WHERE parent_id=?",
                (node_id,)).fetchone()["c"]
            integration = kid_count > 0  # 有孩子一律走整合（含 failed 重试）
            if integration and not self._integration_satisfied(cur, node_id):
                raise GraphError("整合尝试未解锁：普通孩子未全 accepted 或"
                                 "存在未定胜负的 OR 组")
            if node.group_id is not None and not self._member_dispatchable(cur, node):
                raise GraphError("OR 组激活规则：前支未达切换阈值（FAIL 数不足）"
                                 "或组已有赢家")
            dep_rows: dict = {}
            for dep in cur.execute(
                    "SELECT depends_on, expected_version FROM dependencies"
                    " WHERE node_id=?", (node_id,)).fetchall():
                eff = self._resolve_dep_effective(cur, dep["depends_on"])
                drow = cur.execute("SELECT status FROM nodes WHERE id=?",
                                   (eff,)).fetchone()
                if drow is None or drow["status"] != "accepted":
                    raise GraphError(f"依赖未满足：{dep['depends_on']}"
                                     f"（有效源 {eff}）未 accepted")
                dep_rows[eff] = dep["expected_version"]
                # R3：持久绑定契约核对（apply 时落 dep_bindings）——快照
                # id 相同但 manifest/文件清单被篡改/损坏（如清空 files）
                # = 认领拒绝，不按损坏清单播种零件；无契约行（人工节点/
                # 旧库）保持原语义
                if dep["expected_version"] is not None:
                    import json as _jc
                    ctr = cur.execute(
                        "SELECT manifest_hash, files_json, files_meta_json"
                        " FROM dep_bindings WHERE node_id=? AND depends_on=?",
                        (node_id, dep["depends_on"])).fetchone()
                    if ctr is not None:
                        # F2（Codex 三轮）：认领时按冻结契约做**完整重算**
                        # ——规范化 manifest_hash_of 对当前快照清单重算并
                        # 与契约比对（同 ID、同文件名的元数据/文件联合
                        # 改写必改重算值），逐文件 rel/sha/size 元信息与
                        # 契约比对（manifest 字段被同步改写也拦得住）
                        srow = cur.execute(
                            "SELECT manifest_hash, files_json FROM snapshots"
                            " WHERE id=?", (dep["expected_version"],)).fetchone()
                        from .artifacts import manifest_hash_of as _mho_c
                        _EMPTY_HASH = _mho_c([])

                        # 解析快照文件清单与绑定契约清单（按集合与协议语义，不凭裸字符串）
                        meta = None
                        if srow and srow["files_json"] is not None:
                            try:
                                p_s = _jc.loads(srow["files_json"])
                                if isinstance(p_s, list):
                                    meta = p_s
                            except Exception:
                                meta = None

                        ctr_files = None
                        if ctr and ctr["files_json"] is not None:
                            try:
                                p_c = _jc.loads(ctr["files_json"])
                                if isinstance(p_c, list):
                                    ctr_files = p_c
                            except Exception:
                                ctr_files = None

                        try:
                            ctr_meta = _jc.loads(ctr["files_meta_json"] or "[]")
                            if not isinstance(ctr_meta, list):
                                ctr_meta = None
                        except Exception:
                            ctr_meta = None

                        _file_sig = lambda ms: sorted(
                            (m.get("rel_path"), m.get("sha256"), m.get("size"))
                            for m in ms) if ms is not None else []

                        # 契约哈希与空集合语义核对：
                        # 1. 快照不存在
                        # 2. 快照哈希与绑定哈希不符
                        # 3. 契约或快照文件解析为 None（非法 JSON 或非列表）
                        # 4. 非空产物（manifest_hash != _EMPTY_HASH）但契约或快照文件列表为空（缺件坏库）
                        # 5. 重算快照清单哈希与绑定哈希不符（包含空列表 _mho_c([]) != non_empty_hash）
                        # 6. 详细元数据存在时签名不一致
                        ctr_hash = ctr["manifest_hash"] or ""
                        is_non_empty_contract = bool(ctr_hash and ctr_hash != _EMPTY_HASH)

                        has_defect = (
                            srow is None
                            or srow["manifest_hash"] != ctr_hash
                            or meta is None
                            or ctr_files is None
                            or (is_non_empty_contract and (len(meta) == 0 or len(ctr_files) == 0))
                            or _mho_c(meta) != ctr_hash
                            or (ctr_meta is not None and len(ctr_meta) > 0 and _file_sig(meta) != _file_sig(ctr_meta))
                        )
                        if has_defect:
                            raise GraphError(
                                f"依赖 {dep['depends_on']} 快照 "
                                f"{dep['expected_version']} 与父认领绑定契约"
                                "不符（缺失/篡改/清单损坏/空清单缺件，完整 manifest "
                                "重算不匹配）：拒绝认领——人工处置，不得"
                                "按损坏清单播种")
            # v3.2 协议02：认领即冻结输入绑定；协议03：写认领指纹
            # A3 派发硬闸门（2026-10-04 查看轮补齐）：真实执行后端
            # （claude-cli/host 等）缺五要素简报 → 拒绝认领；测试桩后端
            # 降为告警事件（零模型桩不代表真实派发，保既有回归不受扰）
            from .briefing import STUB_BACKENDS as _STUBS
            from .briefing import check_briefing as _cb
            gate = _cb(node.statement or "")
            if not gate["ok"]:
                if backend not in _STUBS:
                    raise GraphError(
                        f"简报五要素缺段：{gate['missing']}"
                        "（派发硬闸门——【输入】【交付清单】【接口签名】"
                        "【样例】【自测入口】齐全才放行真实执行器）")
                self.repo.append_event(cur, mid, "brief_gate_warning",
                                       node_id, missing=gate["missing"])
            bindings = self._resolve_bindings(cur, mid, node_id, integration, dep_rows)
            import json as _jsonfp
            from .acceptance import compute_check_digest as _ccd
            fp = {
                "goal_version": self._goal_version(cur, mid),
                "check_version": node.check_version,
                "node_revision": node.revision,
                "input_binding_hash": self._binding_hash(bindings),
                # R1：考卷内容锁定——命令文本+引用脚本内容摘要入指纹
                "check_digest": _ccd(
                    check_cmd=node.check_cmd,
                    acceptance=_jsonfp.loads(node.acceptance_json)
                    if node.acceptance_json else None,
                    exam_files=self._exam_files_of(node)),
            }
            # R1：出题冻结摘要核对（apply 时落 node_exam_freeze）——
            # 关闭"apply 验过→孩子认领前考卷漂移"窗口：认领的必须就是
            # 出题体检过的那份内容
            frz = cur.execute(
                "SELECT exam_digest FROM node_exam_freeze WHERE node_id=?",
                (node_id,)).fetchone()
            if frz is not None and frz["exam_digest"] != fp["check_digest"]:
                raise GraphError(
                    "考卷内容与出题冻结摘要不一致（认领前漂移）：拒绝认领"
                    "——人工处置，不得以漂移后考卷判卷")
            import json as _json2
            workdir = Path(self.repo.db_path).parent / "work" / node_id / aid
            workdir.mkdir(parents=True, exist_ok=True)
            cur.execute(
                "INSERT INTO attempts(id,node_id,mission_id,backend,status,workdir,"
                "resources_json,input_binding_json,claim_fingerprint_json,started_at)"
                " VALUES(?,?,?,?,'running',?,?,?,?,?)",
                (aid, node_id, mid, backend, str(workdir),
                 f'{{"integration": {"true" if integration else "false"},'
                 f' "goal_version": {fp["goal_version"]}}}',
                 _json2.dumps(bindings, ensure_ascii=False),
                 _json2.dumps(fp, ensure_ascii=False), ts))
            cur.execute("UPDATE nodes SET status='in_work', updated_at=? WHERE id=?",
                        (ts, node_id))
            self.repo.append_event(cur, mid, "attempt_started", node_id,
                                   attempt=aid, integration=integration, backend=backend)
        return aid

    def _replay_fresh(self, mid: str, node_id: str, cand: Path,
                      bindings: list[dict]) -> tuple[str, str]:
        """v3.2 第4步·干净重放（协议第4步）：根验收二次关卡——全新目录、
        只带绑定输入快照 + 冻结候选的申报产物，重跑同一考卷。
        重放不过（fail/error）→ 不得宣告 machine 闭合（调用方改判）。"""
        import json as _json
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory(prefix="replay_") as td:
            fresh = Path(td)
            from .artifacts import manifest_hash_of as _mho_r
            for b in bindings:
                sid = b.get("snapshot_id")
                if not sid:
                    continue
                snap = self.repo.snapshot_by_id(sid)
                if snap is None or not snap.valid:
                    return "error", f"重放输入快照失效：{b['source']}"
                # N3（Codex 四轮）：重放与播种同规则——快照清单规范化
                # 重算=认领账本 manifest，失配拒绝（空清单≠合法）
                try:
                    meta_r = _json.loads(snap.files_json or "[]")
                except Exception:
                    meta_r = None
                recomputed = _mho_r(meta_r) if meta_r else ""
                if (b.get("manifest_hash") or "") and \
                        recomputed != b["manifest_hash"]:
                    return "error", (f"重放核验失败：{b['source']} 快照与"
                                     "认领账本 manifest 不符")
                # B2（Codex 五轮）：重放同规则——来源/版本身份核对
                if str(snap.node_id) != str(b.get("source")):
                    return "error", (f"重放身份核验失败：{b['source']} "
                                     f"快照实际来源 {snap.node_id}")
                if (b.get("artifact_version") is not None
                        and snap.artifact_version is not None
                        and int(snap.artifact_version)
                        != int(b["artifact_version"])):
                    return "error", (f"重放身份核验失败：{b['source']} "
                                     "快照版本与账本不符")
                try:
                    self.store.seed_from_snapshot(snap, fresh)
                except ValueError as e:
                    return "error", f"重放播种失败：{e}"
            node = self.repo.get_node(node_id)
            for rel in _json.loads(node.declared_artifacts_json or "[]"):
                src = cand / rel
                if not src.is_file():
                    return "error", f"重放缺申报产物：{rel}"
                dest = fresh / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
            acc_items = (_json.loads(node.acceptance_json)
                         if node.acceptance_json else None)
            if acc_items:
                from .acceptance import compile_all as _ca, run_acceptance as _ra
                return _ra(_ca(acc_items), fresh)
            if self.checker3 is not None:
                return self.checker3(node.check_cmd, fresh)
            ok, ev = self.checker(node.check_cmd, fresh)
            return ("pass" if ok else "fail"), ev

    def _lineage_map(self, attempt) -> tuple[dict, dict]:
        """T5 per-file lineage 索引（认领绑定 → 来源）。

        返回 (by_path, by_sha)：
        - by_path：inputs/<src>/… 前缀 → (src, version)——申报转交播种件；
        - by_sha：上游快照件内容哈希 → (src, version, rel_path)——合并件
          与上游逐字节一致时推断 verbatim 来源（内容级血缘）。
        来源随 files 清单进 manifest 摘要——血缘变化即快照身份变化。
        """
        by_path: dict[tuple, tuple] = {}
        by_sha: dict[str, tuple] = {}
        import json as _json
        for b in _json.loads(attempt.input_binding_json or "[]"):
            sid = b.get("snapshot_id")
            if not sid or not b.get("source"):
                continue
            src, ver = b["source"], b.get("artifact_version") or 0
            by_path[src] = (src, ver)
            snap = self.repo.snapshot_by_id(sid)
            if snap is None:
                continue
            for f in _json.loads(snap.files_json or "[]"):
                by_sha.setdefault(f["sha256"], (src, ver, f["rel_path"]))
        return by_path, by_sha

    def _lineage_source(self, attempt, rel_path: str, sha: str) -> dict:
        """T5' 原样转交三条件（Codex t-review：inputs/<node>/ 前缀≠上游、
        改写件/凭空文件不得冒充原件）：upstream 来源必须同时满足
        ①来源属本次绑定快照；②该文件在上游快照 manifest 内；
        ③申报内容 SHA256 与来源文件一致。
        - 路径+哈希双命中=原样转交（强证据，basis="path+sha"）；
        - 仅内容命中=verbatim 推断（basis="sha"），多来源不同指→歧义；
        - 无依据→{"origin":"unknown"}——不自动断言 self（自产须有依据：
          文件不在任何上游且非转交，由调用方在展示层标注本节点产物）。"""
        import json as _json
        strong: list[tuple[str, int, str]] = []
        weak: list[tuple[str, int, str]] = []
        rel_n = rel_path.replace("\\", "/")
        for b in _json.loads(attempt.input_binding_json or "[]"):
            sid = b.get("snapshot_id")
            if not sid or not b.get("source"):
                continue
            snap = self.repo.snapshot_by_id(sid)
            if snap is None:
                continue
            src, ver = b["source"], b.get("artifact_version") or 0
            for f in _json.loads(snap.files_json or "[]"):
                up_rel = f["rel_path"].replace("\\", "/")
                same_sha = f["sha256"] == sha
                path_hit = (rel_n == f"inputs/{src}/{up_rel}")
                if path_hit and same_sha:
                    strong.append((src, ver, up_rel))
                elif same_sha:
                    weak.append((src, ver, up_rel))
        for pool, basis in ((strong, "path+sha"), (weak, "sha")):
            uniq = sorted(set(pool))
            if len(uniq) == 1:
                src, ver, up_rel = uniq[0]
                return {"node": src, "version": ver, "file": up_rel,
                        "basis": basis}
            if len(uniq) > 1:
                return {"origin": "ambiguous",
                        "candidates": [{"node": c[0], "version": c[1],
                                        "file": c[2]} for c in uniq]}
        return {"origin": "unknown"}

    def submit_attempt(self, mid: str, node_id: str, attempt_id: str,
                       artifact_paths: list[str] = ()) -> dict:
        """两阶段接收（协议03）：
        P0 预检指纹 → P1 冻结候选 → P2 候选上检查（逐条三分聚合优先）
        → P3 通过才备文件（不写库）→ P4 单事务内核验提交权/版本/绑定后正式接收。
        重复提交幂等返回既有结果；检查期间取消/改目标/维修/失效 → attempt 置
        rejected（或保持既有终态），绝不覆写当前状态。
        """
        import json as _json
        import shutil
        from .acceptance import (compile_all as _compile_items,
                                 compute_check_digest, env_note,
                                 run_acceptance as _run_items)
        from .artifacts import manifest_hash_of
        from .models import Snapshot
        with self.repo._lock:  # R2+：P0 初读串行化（跨线程共享连接的读竞态）
            attempt = self.repo.get_attempt(attempt_id)
            node = self.repo.get_node(node_id)
            mission = self.repo.get_mission(mid)
        if attempt.node_id != node_id or attempt.mission_id != mid:
            raise GraphError("尝试与节点/任务不匹配")
        # 幂等：终态尝试重复提交 → 返回既有权威结果，不再产生副作用
        if attempt.status != "running":
            prior = self.repo._conn.execute(
                "SELECT verdict, evidence FROM assessments WHERE attempt_id=?"
                " AND authoritative=1 ORDER BY id DESC LIMIT 1",
                (attempt_id,)).fetchone()
            if prior is not None:
                return {"verdict": prior["verdict"], "evidence": prior["evidence"],
                        "artifacts": [], "replayed": True}
            raise GraphError(f"尝试状态 {attempt.status} 不可提交")
        fp = _json.loads(attempt.claim_fingerprint_json or "{}")
        # P0 预检（早失败；权威核验在 P4 事务内）
        if fp.get("goal_version") != mission.goal_version:
            raise GraphError("目标版本已变更：本次尝试过期，验收作废")
        if fp.get("check_version") != node.check_version:
            raise GraphError("考卷已维修（check_version 变更）：本次尝试过期")
        if fp.get("node_revision") != node.revision:
            raise GraphError("节点修订已变更：本次尝试过期")
        # R1：考卷内容锁定——命令文本+脚本内容摘要都要一致（只锁版本号不够）
        acc0 = (_json.loads(node.acceptance_json)
                if node.acceptance_json else None)
        if fp.get("check_digest") and fp["check_digest"] != compute_check_digest(
                check_cmd=node.check_cmd, acceptance=acc0,
                exam_files=self._exam_files_of(node)):
            raise GraphError("考卷内容已变更（脚本内容与认领时不一致）："
                             "本次尝试过期，请重新认领")
        res = _json.loads(attempt.resources_json or "{}")
        if node.status == "waiting_children" and not res.get("integration"):
            raise GraphError("节点已转入分解：原尝试已释放，提交须走整合尝试")
        # P1 冻结候选：检查跑在候选副本上——检查的=发布的
        # R2：候选目录每次提交唯一（同 attempt 并发重复提交不共享目录）
        workdir = Path(attempt.workdir)
        cand = workdir.parent / f"cand_{attempt_id}_{uuid.uuid4().hex[:8]}"
        staging: Path | None = None   # P3 赋值；早抛时 finally 回收为 no-op
        shutil.rmtree(cand, ignore_errors=True)
        from .assessment import IGNORED_DIRS as _IGN
        shutil.copytree(workdir, cand,
                        ignore=shutil.ignore_patterns(*_IGN))
        # A2' 验收集合=发布集合（Codex 63bf8d2 复核 P1 约束）：
        # ①节点定义的必需工件必须全部在提交清单内（提交清单不得替换
        #   节点定义——此前 or 语义允许缺必需项发布）；
        # ②候选目录冻结 _declared.json=实际发布清单，考卷按该集合验收
        #   （候选全目录可见 ≠ 发布集合，检查必须围绕发布集合）。
        node_required = list(_json.loads(node.declared_artifacts_json or "[]"))
        missing = [r for r in node_required if r not in set(artifact_paths)]
        if missing:
            raise GraphError(f"申报清单缺节点必需工件：{missing}"
                             "（提交清单不得少于节点定义清单）")
        declared = list(artifact_paths) or node_required
        try:
            for rel in declared:
                if not (cand / rel).is_file():
                    raise GraphError(f"申报工件不存在或越界：{rel}")
            (cand / "_declared.json").write_text(
                _json.dumps(sorted(declared), ensure_ascii=False),
                encoding="utf-8")
            # P2 检查（候选目录上；acceptance 清单→逐条三分聚合，ERROR 优先）
            from .assessment import verify_context as _verify_ctx
            acc_items = (_json.loads(node.acceptance_json)
                         if node.acceptance_json else None)
            with _verify_ctx(mission_id=mid, node_id=node_id, attempt_id=attempt_id):
                if acc_items:
                    verdict, evidence = _run_items(_compile_items(acc_items), cand)
                    method = "items"
                elif self.checker3 is not None:
                    verdict, evidence = self.checker3(node.check_cmd, cand)
                    method = "cmd3"
                else:
                    ok, evidence = self.checker(node.check_cmd, cand)
                    verdict, evidence = "pass" if ok else "fail", evidence
                    method = "cmd"
                bindings = _json.loads(attempt.input_binding_json or "[]")
                # v3.2 第4步·干净重放：根节点（整体目标）机器闭合前置——
                # 全新目录只带绑定快照+冻结产物重跑考卷；不过不闭合。
                if verdict == "pass" and node.parent_id is None:
                    rv, r_ev = self._replay_fresh(mid, node_id, cand, bindings)
                    if rv != "pass":
                        verdict = rv  # fail 退回产物不合格；error 走维修
                        evidence = (f"干净重放未过（{rv}）：{r_ev[:300]}；"
                                    f"原检：{evidence[:150]}")
                        method += f"+replay_{rv}"
                    else:
                        evidence += "；干净重放通过（协议第4步）"
            # P3 发布准备（R1：独占 staging，不占版本不碰 vN；正式接收事务内 rename）
            staging: Path | None = None
            prepared: list[dict] = []
            if verdict == "pass" and declared:
                try:
                    staging, prepared = self.store.stage_prepared(
                        mid, node_id, attempt_id, cand, declared)
                except ValueError as e:
                    staging = None
                    verdict, evidence = "error", f"发布准备失败：{e}"
                    method += "+publish_error"
            files_meta = [{"rel_path": p["rel_path"], "sha256": p["sha256"],
                           "size": p["size"],
                           "source": self._lineage_source(attempt,
                                                          p["rel_path"],
                                                          p["sha256"])}
                          for p in prepared]
            mhash = manifest_hash_of(files_meta) if prepared else ""
            digest = compute_check_digest(check_cmd=node.check_cmd,
                                          acceptance=acc_items,
                                          exam_files=self._exam_files_of(node))
            envn = env_note()
            bindings = _json.loads(attempt.input_binding_json or "[]")
            ts = now()
            rejection: str | None = None
            with self.repo.tx() as cur:
                # P4 权威核验（事务内重读——P2 期间的一切变化在此拦截）
                att2 = cur.execute("SELECT status FROM attempts WHERE id=?",
                                   (attempt_id,)).fetchone()
                if att2 is None:
                    raise GraphError("尝试不存在")
                if att2["status"] != "running":
                    rejection = f"提交权已失效（attempt={att2['status']}）"
                if rejection is None:
                    gv = cur.execute("SELECT goal_version FROM missions WHERE id=?",
                                     (mid,)).fetchone()["goal_version"]
                    if gv != fp.get("goal_version"):
                        rejection = "目标版本已变更（检查期间）"
                if rejection is None:
                    nd2 = cur.execute(
                        "SELECT check_version, revision FROM nodes WHERE id=?",
                        (node_id,)).fetchone()
                    if nd2["check_version"] != fp.get("check_version"):
                        rejection = "考卷已维修（检查期间）"
                    elif nd2["revision"] != fp.get("node_revision"):
                        rejection = "节点修订已变更（检查期间）"
                if rejection is None and fp.get("check_digest"):
                    nd3row = cur.execute(
                        "SELECT check_cmd, acceptance_json, exam_files_json"
                        " FROM nodes WHERE id=?", (node_id,)).fetchone()
                    acc_now = (_json.loads(nd3row["acceptance_json"])
                               if nd3row["acceptance_json"] else None)
                    ef_now = self._exam_files_of(nd3row)
                    if compute_check_digest(check_cmd=nd3row["check_cmd"],
                                            acceptance=acc_now,
                                            exam_files=ef_now) != \
                            fp["check_digest"]:
                        rejection = "考卷内容已变更（检查期间，脚本被改）"
                if rejection is None:
                    for b in bindings:
                        if b.get("snapshot_id") is None:
                            continue
                        srow = cur.execute(
                            "SELECT valid FROM snapshots WHERE id=?",
                            (b["snapshot_id"],)).fetchone()
                        if srow is None or srow["valid"] != 1:
                            rejection = f"输入快照已失效：{b['source']}"
                            break
                if rejection is not None:
                    # 不覆写当前真相：attempt 已非 running 则保持其终态
                    if att2["status"] == "running":
                        cur.execute(
                            "UPDATE attempts SET status='rejected', category='reject',"
                            " ended_at=? WHERE id=?", (ts, attempt_id))
                    nd3 = cur.execute("SELECT status FROM nodes WHERE id=?",
                                      (node_id,)).fetchone()
                    if nd3 and nd3["status"] == "in_work":
                        # 执行位已死（拒收），节点回 pending 可重派
                        cur.execute(
                            "UPDATE nodes SET status='pending', updated_at=?"
                            " WHERE id=?", (ts, node_id))
                    self.repo.append_event(cur, mid, "submit_rejected", node_id,
                                           attempt=attempt_id, reason=rejection)
                else:
                    # 正式接收：快照+工件+证据+状态迁移，同一事务
                    self.repo.append_event(cur, mid, "submitted", node_id,
                                           attempt=attempt_id)
                    snapshot_id = None
                    art_version = 0
                    if verdict == "pass" and prepared and staging is not None:
                        # R1：事务内取版本并原子 rename（持写锁，撞车不可能）
                        version = self.store.commit_staged(
                            cur, node_id, staging, attempt_id)
                        snapshot_id = self.repo.insert_snapshot(
                            Snapshot(id=0, mission_id=mid, node_id=node_id,
                                     attempt_id=attempt_id,
                                     artifact_version=version,
                                     node_revision=fp.get("node_revision", 1),
                                     goal_version=fp.get("goal_version", 1),
                                     manifest_hash=mhash,
                                     files_json=_json.dumps(
                                         files_meta, ensure_ascii=False),
                                     valid=1, created_at=ts), cur)
                        for p in prepared:
                            cur.execute(
                                "INSERT INTO artifacts(attempt_id,node_id,mission_id,"
                                "rel_path,content_hash,version,snapshot_id,created_at)"
                                " VALUES(?,?,?,?,?,?,?,?)",
                                (attempt_id, node_id, mid, p["rel_path"],
                                 p["sha256"], version, snapshot_id, ts))
                        art_version = version
                    else:
                        arts = cur.execute(
                            "SELECT COALESCE(MAX(version),0) AS v FROM artifacts"
                            " WHERE node_id=?", (node_id,)).fetchone()["v"]
                        art_version = arts
                    self.repo.insert_assessment(Assessment(
                        id=0, node_id=node_id, attempt_id=attempt_id,
                        artifact_version=art_version,
                        goal_version=fp.get("goal_version", mission.goal_version),
                        check_cmd=node.check_cmd or "",
                        check_version=fp.get("check_version", node.check_version),
                        method=method, evidence=evidence, verdict=verdict,
                        authoritative=1, snapshot_id=snapshot_id,
                        check_digest=digest,
                        input_binding_hash=fp.get("input_binding_hash", ""),
                        env_note=envn, created_at=ts), cur)
                    if verdict == "pass":
                        cur.execute(
                            "UPDATE attempts SET status='accepted', category='accept',"
                            " ended_at=? WHERE id=?", (ts, attempt_id))
                        cur.execute(
                            "UPDATE nodes SET status='accepted',"
                            " accepted_artifact_version=?, updated_at=? WHERE id=?",
                            (art_version, ts, node_id))
                        self.repo.append_event(cur, mid, "done", node_id,
                                               attempt=attempt_id,
                                               evidence=evidence[:200])
                        self._collect_group_win(cur, mid, node_id)  # OR 组赢家原子选定
                        self._propagate(cur, mid, node_id)
                    elif verdict == "fail":
                        cur.execute(
                            "UPDATE attempts SET status='failed', category='fail',"
                            " ended_at=? WHERE id=?", (ts, attempt_id))
                        # D4 探针（Codex adcc5d5 行为断言）暴露：无条件覆盖会把
                        # pause 置的 blocked 冲成 failed→重试立即重派，暂停失效。
                        # 只在仍 in_work 时转 failed；blocked 保持（人工控制优先）
                        cur.execute(
                            "UPDATE nodes SET status='failed', updated_at=?"
                            " WHERE id=? AND status='in_work'",
                            (ts, node_id))
                        self.repo.append_event(cur, mid, "failed", node_id,
                                               attempt=attempt_id,
                                               evidence=evidence[:200])
                    else:  # error：考卷/环境故障——不追究执行者
                        cur.execute(
                            "UPDATE attempts SET status='error', category='error',"
                            " ended_at=? WHERE id=?", (ts, attempt_id))
                        cur.execute(
                            "UPDATE nodes SET status='blocked', updated_at=?"
                            " WHERE id=?", (ts, node_id))
                        self.repo.append_event(cur, mid, "check_error", node_id,
                                               attempt=attempt_id,
                                               evidence=evidence[:200],
                                               hint="考卷故障：请出题方 dag3_repair_check"
                                                    " 维修后重验")
            if rejection is not None:
                # R1：拒收只回收自己的独占 staging（永不触碰 vN 目录）
                self.store.discard_staged(staging)
                return {"verdict": "rejected", "evidence": rejection,
                        "artifacts": [], "rejected": True}
            return {"verdict": verdict, "evidence": evidence,
                    "artifacts": [(p["rel_path"], art_version) for p in prepared],
                    "snapshot_id": snapshot_id}
        finally:
            # staging 兜底回收：成功路径已被 rename 走（目录不存在=no-op）
            self.store.discard_staged(staging)
            shutil.rmtree(cand, ignore_errors=True)

    # ---- 阻塞 / 取消 / 目标版本 ----

    def block(self, mid: str, node_id: str, reason: str):
        with self.repo.tx() as cur:
            node = self._node(cur, mid, node_id)
            if node.status in ("accepted", "cancelled"):
                raise GraphError(f"终态 {node.status} 不可阻塞")
            cur.execute("UPDATE nodes SET status='blocked', updated_at=? WHERE id=?",
                        (now(), node_id))
            self.repo.append_event(cur, mid, "block", node_id, reason=reason)

    def unblock(self, mid: str, node_id: str):
        with self.repo.tx() as cur:
            node = self._node(cur, mid, node_id)
            if node.status != "blocked":
                raise GraphError(f"节点状态 {node.status} 不是 blocked")
            cur.execute("UPDATE nodes SET status='pending', updated_at=? WHERE id=?",
                        (now(), node_id))
            self.repo.append_event(cur, mid, "unblock", node_id)

    def cancel_node(self, mid: str, node_id: str, reason: str):
        with self.repo.tx() as cur:
            self._node(cur, mid, node_id)
            cur.execute("UPDATE nodes SET status='cancelled', updated_at=? WHERE id=?",
                        (now(), node_id))
            self.repo.append_event(cur, mid, "cancel", node_id, reason=reason)

    def change_goal(self, mid: str, reason: str) -> int:
        with self.repo.tx() as cur:
            cur.execute(
                "UPDATE missions SET goal_version=goal_version+1, updated_at=? WHERE id=?",
                (now(), mid))
            gv = self._goal_version(cur, mid)
            self.repo.append_event(cur, mid, "goal_changed", None, reason=reason,
                                   goal_version=gv)
        return gv

    # ---- v3.1：考卷维修 / 强度记账 / 优先级 / 自检 / 主观闭合 / 受影响面 / 审阅 ----

    def repair_check(self, mid: str, node_id: str,
                     new_check_cmd: Optional[str] = None,
                     acceptance: Optional[list[dict]] = None,
                     reason: str = "",
                     exam_files: Optional[list[str]] = None) -> int:
        """考卷维修协议：出题方专属（执行者无此入口的授权语义由 MCP 层保证）。

        新考卷过程序体检后落库：check_version+1、事件留痕、旧验收证据作废
        （invalid=1）、节点回 pending 等待重验。accepted/cancelled 终态不可修。
        T6'（Codex t-review）：exam_files 与新考卷/版本/证据失效同一事务更新——
        - None：继承现有清单；继承后新命令实际入口必须仍被覆盖，否则拒绝
          （维修换入口不换清单=新考卷漂移不可见，BAD 被接受的 P1 缺陷）；
        - 非空清单：更新为该清单（逐件存在+入口覆盖校验）；
        - 空列表：显式节点不得静默退回扫描模式，直接拒绝。
        """
        node = self.repo.get_node(node_id)
        if node.status in ("accepted", "cancelled"):
            raise GraphError(f"终态 {node.status} 不可维修考卷")
        if new_check_cmd is None and acceptance is None:
            raise GraphError("维修须给新考卷（check_cmd 或 acceptance）")
        from .acceptance import (compile_all as _compile_all,
                                 preflight as _preflight,
                                 verify_exam_files_cover as _vef)
        # 先确定最终形态（命令/acceptance/显式清单），再体检——N1（Codex
        # 四轮）：体检前算摘要①，体检后算摘要②，两者不一致=体检验证的
        # 不是将发布的内容（"体检过 STRICT 后换 GREEN"窗口）→拒绝
        if acceptance is not None:
            cmds = _compile_all(acceptance)
            new_cmd = " && ".join(cmds)
        else:
            new_cmd = new_check_cmd
        import json as _json
        existing = list(_json.loads(node.exam_files_json or "[]"))
        if exam_files is None:
            final_files = existing          # 继承
        elif not exam_files:
            if existing:
                raise GraphError(
                    "显式考卷节点不得以空清单静默退回扫描模式——维修切换模式"
                    "须显式声明新清单")
            final_files = []
        else:
            final_files = list(exam_files)
            for rel in final_files:
                if not Path(rel).is_file():
                    raise GraphError(f"考卷包显式清单文件缺失：{rel}")
        if final_files:
            ok_cov, missing = _vef(new_cmd, acceptance, final_files)
            if not ok_cov:
                raise GraphError(
                    f"维修被拒：新考卷实际入口不在显式清单内：{missing}——"
                    "请同时声明覆盖新入口的 exam_files（命令、清单、版本须"
                    "一致更新）")
        from .acceptance import compute_check_digest as _ccd_r
        digest1 = _ccd_r(check_cmd=new_cmd, acceptance=acceptance,
                         exam_files=tuple(final_files))
        if acceptance is not None:
            pf_ok, pf_why, _ = _preflight(acceptance=acceptance)
        else:
            pf_ok, pf_why, _ = _preflight(check_cmd=new_cmd)
        if not pf_ok:
            raise GraphError(f"新考卷体检未过：{pf_why}")
        digest2 = _ccd_r(check_cmd=new_cmd, acceptance=acceptance,
                         exam_files=tuple(final_files))
        if digest2 != digest1:
            raise GraphError(
                "新考卷在体检期间漂移（前后摘要不一致）：拒绝维修——"
                "体检未验证将发布的内容")
        ts = now()
        with self.repo.tx() as cur:
            # N2（Codex 四轮）：权威终态核对在**本事务内**——体检期间
            # 原提交可能已闭合节点/任务：节点 accepted/任务 done/cancelled
            # → 维修拒绝（不改卷、不作废既有机器证据；改已完成规格须走
            # 正式 revise/reopen 协议）
            _row = cur.execute(
                "SELECT status FROM nodes WHERE id=? AND mission_id=?",
                (node_id, mid)).fetchone()
            if _row is None:
                raise GraphError(f"节点不存在：{node_id}")
            if _row["status"] in ("accepted", "cancelled"):
                raise GraphError(
                    f"维修被拒：节点已进入终态 {_row['status']}"
                    "（体检期间闭合）——不修改已完成规格，"
                    "变更须走 revise/reopen 协议")
            _mrow = cur.execute(
                "SELECT status FROM missions WHERE id=?", (mid,)).fetchone()
            if _mrow is None or _mrow["status"] in ("done", "cancelled"):
                raise GraphError(
                    "维修被拒：任务已闭合/取消（体检期间）——不作废唯一"
                    "机器证据，变更须走 revise/reopen 协议")
            # N1：发布前最后一核（事务内重算③——①=②≠③同样拒绝）
            digest3 = _ccd_r(check_cmd=new_cmd, acceptance=acceptance,
                             exam_files=tuple(final_files))
            if digest3 != digest2:
                raise GraphError(
                    "新考卷在维修发布前漂移（体检后摘要变化）：拒绝——"
                    "无半更新，旧版本与冻结记录保持一致")
            old_v = cur.execute("SELECT check_version FROM nodes WHERE id=?",
                                (node_id,)).fetchone()["check_version"]
            cur.execute(
                "UPDATE nodes SET check_cmd=?, check_version=check_version+1,"
                " acceptance_json=?, exam_files_json=?, updated_at=? WHERE id=?",
                (new_cmd, _json.dumps(acceptance, ensure_ascii=False)
                 if acceptance else None,
                 _json.dumps(final_files, ensure_ascii=False), ts, node_id))
            new_v = old_v + 1
            cur.execute(
                "UPDATE assessments SET invalid=1 WHERE node_id=? AND invalid=0",
                (node_id,))
            if _row["status"] in ("blocked", "failed"):
                cur.execute("UPDATE nodes SET status='pending', updated_at=?"
                            " WHERE id=?", (ts, node_id))
            # F1（Codex 三轮）+N1（四轮）：冻结摘要=本事务内重算的
            # digest3（与落库内容一致——digest3 即按最终命令/清单算出）
            # **同事务** UPSERT（不删行绕过、不留旧摘要堵死认领）；旧
            # 摘要进事件保留审计链。
            _old_frz = cur.execute(
                "SELECT exam_digest FROM node_exam_freeze WHERE node_id=?",
                (node_id,)).fetchone()
            cur.execute(
                "INSERT INTO node_exam_freeze(node_id,exam_digest,proposal_id,"
                "frozen_at) VALUES(?,?,NULL,?)"
                " ON CONFLICT(node_id) DO UPDATE SET"
                " exam_digest=excluded.exam_digest,"
                " frozen_at=excluded.frozen_at",
                (node_id, digest3, ts))
            self.repo.append_event(
                cur, mid, "exam_freeze_updated", node_id,
                from_version=old_v, to_version=new_v,
                old_digest=(_old_frz["exam_digest"] if _old_frz else ""),
                new_digest=digest3, reason="authorized_repair")
            self.repo.append_event(cur, mid, "check_repaired", node_id,
                                   from_version=old_v, to_version=new_v,
                                   reason=reason[:300],
                                   exam_files_updated=(exam_files is not None))
        return new_v

    def declare_strength(self, mid: str, node_id: str, strength: str):
        """强度记账：交付时填"实际做到什么/没做到什么"，防名实不符。"""
        if not (strength or "").strip():
            raise GraphError("强度声明不可为空")
        with self.repo.tx() as cur:
            node = self._node(cur, mid, node_id)
            if node.status == "cancelled":
                raise GraphError("cancelled 节点不记账强度")
            cur.execute("UPDATE nodes SET strength_actual=?, updated_at=? WHERE id=?",
                        (strength, now(), node_id))
            self.repo.append_event(cur, mid, "strength_declared", node_id,
                                   strength=strength[:300])

    def set_priority(self, mid: str, node_id: str, priority: int):
        """调度优先级（Anthropic 原版：人发"优先完成 Mazur"式指令的落地通道）。"""
        with self.repo.tx() as cur:
            self._node(cur, mid, node_id)
            cur.execute("UPDATE nodes SET priority=?, updated_at=? WHERE id=?",
                        (int(priority), now(), node_id))
            self.repo.append_event(cur, mid, "priority_set", node_id,
                                   priority=int(priority))

    def self_check(self, mid: str, node_id: str,
                   workdir: Optional[Path] = None) -> dict:
        """执行者自检（反馈角色，零权威）：跑同一考卷，落账 authoritative=0，
        不改变节点任何状态——只有平台 submit 的验收才算数。"""
        node = self.repo.get_node(node_id)
        if workdir is None:
            base = Path(self.repo.db_path).parent / "work" / node_id
            workdir = base if base.is_dir() else Path(self.repo.db_path).parent
        if self.checker3 is not None:
            verdict, evidence = self.checker3(node.check_cmd, workdir)
        else:
            ok, evidence = self.checker(node.check_cmd, workdir)
            verdict = "pass" if ok else "fail"
        ts = now()
        with self.repo.tx() as cur:
            self._node(cur, mid, node_id)
            cur.execute(
                "INSERT INTO assessments(node_id,attempt_id,artifact_version,"
                "goal_version,check_cmd,check_version,method,evidence,verdict,"
                "authoritative,created_at) VALUES(?,?,?,?,?,?,?,?,?,0,?)",
                (node_id, "self", node.accepted_artifact_version or 0,
                 self.repo.get_mission(mid).goal_version,
                 node.check_cmd or "", node.check_version, "selfcheck",
                 evidence, verdict, ts))
            self.repo.append_event(cur, mid, "self_check", node_id,
                                   verdict=verdict, authoritative=0)
        return {"verdict": verdict, "evidence": evidence, "authoritative": 0}

    def close_mission_subjective(self, mid: str, note: str = "",
                                 basis: str = "",
                                 snapshot_id: Optional[int] = None) -> str:
        """显式降级：主观层闭合。closure_basis=subjective，永不冒充机器闭合。
        v3.2：签收依据（basis）必填、绑定具体快照——主观闭合是显式签收，不是补丁。"""
        if not (basis or "").strip():
            raise GraphError("主观闭合必须记录签收依据（basis）")
        with self.repo.tx() as cur:
            self._mission_active(cur, mid)
            cur.execute(
                "UPDATE missions SET status='done', closure_basis='subjective',"
                " updated_at=? WHERE id=?", (now(), mid))
            self.repo.append_event(cur, mid, "mission_done_subjective", None,
                                   note=note[:300], basis=basis[:500],
                                   snapshot_id=snapshot_id)
        return "subjective"

    # ---- v3.2 第2步：修订 / 级联失效 / 重开协议 ----

    def _invalidate_node_evidence(self, cur, mid: str, nid: str,
                                  reason: str, ts: str):
        """作废节点全部快照（valid=0）与机器证据（invalid=1），事件留痕。
        历史 accepted 文件与证据行保留（可审计），仅失效。"""
        cur.execute("UPDATE snapshots SET valid=0 WHERE node_id=? AND valid=1",
                    (nid,))
        cur.execute("UPDATE assessments SET invalid=1 WHERE node_id=?"
                    " AND invalid=0", (nid,))
        self.repo.append_event(cur, mid, "evidence_invalidated", nid,
                               reason=reason[:200])

    def affected_closure(self, mid: str, node_id: str) -> list[str]:
        """修订级联闭包（R2 修复：迭代至稳定）。从 node_id 出发反复扩展：
        ① 直接依赖者（反向 depends_on）；② OR 组成员的依赖者（下游 deps 写在
        成员上、实际绑定赢家——修订任一相关成员都要沿真实绑定传播）；
        ③ 每个受影响节点自己的全部整合祖先（沿 parent 链，跨父场景）。
        祖先入集后其反向依赖继续传播，直到闭包稳定。"""
        out: set[str] = set()
        stack = [node_id]
        conn = self.repo._conn
        while stack:
            x = stack.pop()
            if x in out:
                continue
            out.add(x)
            row = conn.execute(
                "SELECT parent_id, group_id FROM nodes WHERE id=?", (x,)).fetchone()
            if row is None:
                continue
            # ① 反向依赖
            stack += [r["node_id"] for r in conn.execute(
                "SELECT node_id FROM dependencies WHERE depends_on=?",
                (x,)).fetchall()]
            # ② OR 组展开：同组成员的依赖者
            if row["group_id"] and row["parent_id"]:
                for m in conn.execute(
                        "SELECT id FROM nodes WHERE parent_id=? AND group_id=?",
                        (row["parent_id"], row["group_id"])).fetchall():
                    stack += [r["node_id"] for r in conn.execute(
                        "SELECT node_id FROM dependencies WHERE depends_on=?",
                        (m["id"],)).fetchall()]
            # ③ 自身整合祖先链（祖先继续参与①②传播）
            p = row["parent_id"]
            hops = 0
            while p and hops < 64:
                stack.append(p)
                prow = conn.execute(
                    "SELECT parent_id FROM nodes WHERE id=?", (p,)).fetchone()
                p = prow["parent_id"] if prow else None
                hops += 1
        out.discard(node_id)
        return sorted(out)

    def revise_node(self, mid: str, node_id: str, reason: str) -> int:
        """产物修订（出题方/人显式发起）：revision+1 → 本节点快照与证据作废 →
        级联失效（反向执行依赖 + 全部整合祖先）→ 本节点回 pending 重做；
        在途尝试取消；mission 已 done 自动重开，链条重建后根重新闭合。"""
        if not (reason or "").strip():
            raise GraphError("修订必须给原因")
        ts = now()
        with self.repo.tx() as cur:
            node = self._node(cur, mid, node_id)
            if node.status == "cancelled":
                raise GraphError("cancelled 节点不可修订")
            new_rev = node.revision + 1
            # 目标节点自身：有孩子→waiting_children（拆分后节点重做=
            # 整合重跑）。无条件 pending 会造成"有孩子的 pending 死水"：
            # frontier 排除有孩子节点、integration_ready 只认
            # waiting_children → 整图卡死（s1-up-1h 实证后最小修复，
            # 与下方级联分支的 has_kids 语义对齐）
            has_kids_self = cur.execute(
                "SELECT COUNT(*) AS c FROM nodes WHERE parent_id=?",
                (node_id,)).fetchone()["c"] > 0
            cur.execute("UPDATE nodes SET revision=?, status=?,"
                        " accepted_artifact_version=NULL, updated_at=?"
                        " WHERE id=?",
                        (new_rev,
                         "waiting_children" if has_kids_self else "pending",
                         ts, node_id))
            # 本节点自身在途一并取消（节点已回 pending，遗留 running 不一致）
            own_run = cur.execute(
                "SELECT id FROM attempts WHERE node_id=? AND status='running'",
                (node_id,)).fetchone()
            if own_run:
                cur.execute(
                    "UPDATE attempts SET status='cancelled', category='cancel',"
                    " ended_at=? WHERE id=?", (ts, own_run["id"]))
                self.repo.append_event(cur, mid, "attempt_cancelled", node_id,
                                       attempt=own_run["id"],
                                       reason="node_revised")
            self._invalidate_node_evidence(cur, mid, node_id,
                                           f"revised:{node_id}", ts)
            for nid in self.affected_closure(mid, node_id):
                nrow = cur.execute("SELECT status FROM nodes WHERE id=?",
                                   (nid,)).fetchone()
                if nrow is None or nrow["status"] == "cancelled":
                    continue
                self._invalidate_node_evidence(cur, mid, nid,
                                               f"cascade:{node_id}", ts)
                run = cur.execute(
                    "SELECT id FROM attempts WHERE node_id=? AND status='running'",
                    (nid,)).fetchone()
                if run:
                    cur.execute(
                        "UPDATE attempts SET status='cancelled', category='cancel',"
                        " ended_at=? WHERE id=?", (ts, run["id"]))
                    self.repo.append_event(cur, mid, "attempt_cancelled", nid,
                                           attempt=run["id"],
                                           reason="revision_cascade")
                has_kids = cur.execute(
                    "SELECT COUNT(*) AS c FROM nodes WHERE parent_id=?",
                    (nid,)).fetchone()["c"] > 0
                cur.execute(
                    "UPDATE nodes SET status=?, accepted_artifact_version=NULL,"
                    " updated_at=? WHERE id=?",
                    ("waiting_children" if has_kids else "pending", ts, nid))
            mrow = cur.execute("SELECT status FROM missions WHERE id=?",
                               (mid,)).fetchone()
            if mrow["status"] == "done":
                cur.execute("UPDATE missions SET status='active', updated_at=?"
                            " WHERE id=?", (ts, mid))
                self.repo.append_event(cur, mid, "mission_reopened", None,
                                       reason=reason[:200])
            self.repo.append_event(cur, mid, "node_revised", node_id,
                                   revision=new_rev, reason=reason[:300])
        return new_rev

    def reopen_mission(self, mid: str, reason: str):
        """已 done 任务显式重开：根回等待态（有孩子=waiting_children），事件留痕。"""
        if not (reason or "").strip():
            raise GraphError("重开必须给原因")
        ts = now()
        with self.repo.tx() as cur:
            row = cur.execute("SELECT status FROM missions WHERE id=?",
                              (mid,)).fetchone()
            if row is None:
                raise GraphError(f"任务不存在：{mid}")
            if row["status"] != "done":
                raise GraphError(f"任务状态 {row['status']} 无须重开")
            root = cur.execute(
                "SELECT id, status FROM nodes WHERE mission_id=? AND parent_id"
                " IS NULL", (mid,)).fetchone()
            if root and root["status"] == "accepted":
                has_kids = cur.execute(
                    "SELECT COUNT(*) AS c FROM nodes WHERE parent_id=?",
                    (root["id"],)).fetchone()["c"] > 0
                cur.execute(
                    "UPDATE nodes SET status=?, accepted_artifact_version=NULL,"
                    " updated_at=? WHERE id=?",
                    ("waiting_children" if has_kids else "pending", ts, root["id"]))
            cur.execute("UPDATE missions SET status='active', updated_at=?"
                        " WHERE id=?", (ts, mid))
            self.repo.append_event(cur, mid, "mission_reopened", None,
                                   reason=reason[:300])

    def affected_subgraph(self, mid: str, node_id: str) -> list[str]:
        """受影响面（增量重验的依据）：依赖闭包（谁传递依赖我）+ 归属闭包（我的孩子）。"""
        out: set[str] = set()
        stack = [node_id]
        while stack:
            cur_id = stack.pop()
            if cur_id in out:
                continue
            out.add(cur_id)
            rows = self.repo._conn.execute(
                "SELECT node_id FROM dependencies WHERE depends_on=?",
                (cur_id,)).fetchall()
            stack += [r["node_id"] for r in rows]
            kids = self.repo._conn.execute(
                "SELECT id FROM nodes WHERE parent_id=?", (cur_id,)).fetchall()
            stack += [r["id"] for r in kids]
        out.discard(node_id)
        return sorted(out)

    def attach_review(self, mid: str, node_id: str, verdict: str,
                      note: str = "", blind: bool = True,
                      snapshot_id: Optional[int] = None,
                      basis: str = "") -> int:
        """主观层审阅证据（盲读）：独立会话产出，append-only，永不与机器验收合并。
        v3.2：签收可绑定具体快照（snapshot_id）并记录签收依据（basis）。"""
        if verdict not in ("accept", "concern", "reject"):
            raise GraphError("审阅 verdict 须为 accept|concern|reject")
        ts = now()
        with self.repo.tx() as cur:
            self._node(cur, mid, node_id)
            if snapshot_id is not None:
                srow = cur.execute("SELECT node_id FROM snapshots WHERE id=?",
                                   (snapshot_id,)).fetchone()
                if srow is None or srow["node_id"] != node_id:
                    raise GraphError(f"签收快照 {snapshot_id} 不属于节点 {node_id}")
            cur.execute(
                "INSERT INTO reviews(node_id,verdict,note,blind,snapshot_id,basis,"
                "created_at) VALUES(?,?,?,?,?,?,?)",
                (node_id, verdict, note[:2000], 1 if blind else 0,
                 snapshot_id, basis[:500], ts))
            rid = cur.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
            self.repo.append_event(cur, mid, "review_attached", node_id,
                                   verdict=verdict, blind=1 if blind else 0,
                                   snapshot_id=snapshot_id)
        return int(rid)

    # ---- 查询 ----

    def frontier(self, mid: str) -> list[str]:
        """可立即开始尝试的叶子节点：pending、无孩子、依赖全 accepted；
        v3.2：OR 组成员须同时满足激活规则（前支未耗尽不派）。"""
        nodes = self.repo.list_nodes(mid)
        parents = {n.parent_id for n in nodes if n.parent_id}
        out = []
        with self.repo.tx() as cur:
            for n in nodes:
                if n.status != "pending" or n.id in parents:
                    continue
                if n.group_id is not None and not self._member_dispatchable(cur, n):
                    continue
                if all(self.repo.get_node(d.depends_on).status == "accepted"
                       for d in self.repo.deps_of(n.id)):
                    out.append(n.id)
        return out

    def integration_ready(self, mid: str) -> list[str]:
        out = []
        for n in self.repo.list_nodes(mid):
            if n.status != "waiting_children":
                continue
            if self.repo._conn.execute(
                    "SELECT COUNT(*) AS c FROM nodes WHERE parent_id=?",
                    (n.id,)).fetchone()["c"] == 0:
                continue
            with self.repo.tx() as cur:
                if self._integration_satisfied(cur, n.id):
                    out.append(n.id)
        return out

    def children_all_accepted(self, node_id: str) -> bool:
        """v3.2 组感知：普通孩子全 accepted 且每个 OR 组有赢家。"""
        with self.repo.tx() as cur:
            return self._integration_satisfied(cur, node_id)

    def children_of(self, node_id: str) -> list[Node]:
        mid = self.repo.get_node(node_id).mission_id
        return [k for k in self.repo.list_nodes(mid) if k.parent_id == node_id]

    # ---- 内部 ----

    def _children_stat(self, cur, node_id: str) -> dict:
        """孩子状态统计（排除 cancelled=移出范围）。"""
        rows = cur.execute(
            "SELECT status, COUNT(*) AS c FROM nodes WHERE parent_id=?"
            " AND status!='cancelled' GROUP BY status", (node_id,)).fetchall()
        return {r["status"]: r["c"] for r in rows}

    def _node(self, cur, mid: str, node_id: str) -> Node:
        row = cur.execute("SELECT * FROM nodes WHERE id=? AND mission_id=?",
                          (node_id, mid)).fetchone()
        if row is None:
            raise GraphError(f"节点不存在：{node_id}")
        return Node(**dict(row))

    def _mission_active(self, cur, mid: str) -> Mission:
        row = cur.execute("SELECT * FROM missions WHERE id=?", (mid,)).fetchone()
        if row is None:
            raise GraphError(f"任务不存在：{mid}")
        m = Mission(**dict(row))
        if m.status != "active":
            raise GraphError(f"任务状态 {m.status}，拒绝变更")
        return m

    def _goal_version(self, cur, mid: str) -> int:
        row = cur.execute("SELECT goal_version FROM missions WHERE id=?", (mid,)).fetchone()
        return row["goal_version"]

    def _propagate(self, cur, mid: str, node_id: str):
        """accepted 后向上传播：孩子侧满足（组感知）→ 父发 integration_ready
        事件；根 → 任务 done。"""
        node = self._node(cur, mid, node_id)
        if node.parent_id:
            parent = self._node(cur, mid, node.parent_id)
            if parent.status == "waiting_children":
                if self._integration_satisfied(cur, parent.id):
                    self.repo.append_event(cur, mid, "integration_ready", parent.id)
        else:
            cur.execute("UPDATE missions SET status='done', updated_at=? WHERE id=?",
                        (now(), mid))
            self.repo.append_event(cur, mid, "mission_done", node.id)
