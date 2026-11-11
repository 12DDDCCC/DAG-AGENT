"""store.py — 任务DAG状态机。

设计要点（对齐Prove2Me源码验证过的机制）：
- 陈述与实现分离：节点在图上是"陈述+checks"，完成=checks通过（kernel式一票否决）。
- 冲突容忍：节点认领无锁，提交不可变（结果一旦done不再改，只有事件日志向前追加）。
- DAG只生长：decompose给父节点加children并把父节点转为aggregate（依赖闭合自动done），
  不回改历史节点。
- 依赖闭合自动状态：aggregate父节点在全部deps done后自动done——假完成在结构上不可能。

mission目录结构：
    <home>/<mid>/
      mission.json   状态（原子写：tmp+replace）
      events.jsonl   事件日志（append-only）
      workspace/     节点工作产物目录
      dag.md         Obsidian看板（每次状态变更重渲染）
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

DEFAULT_HOME = Path.home() / ".dag-agent" / "v1" / "missions"

# 进程内串行化"load→改→save"的写回窗口：让rev冲突可靠发生（而非丢更新），
# 跨进程仍由rev乐观锁保护。
_SAVE_LOCK = threading.Lock()

_MID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_WINDOWS_RESERVED = ({"CON", "PRN", "AUX", "NUL"}
                     | {f"COM{i}" for i in range(1, 10)}
                     | {f"LPT{i}" for i in range(1, 10)})


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def default_home() -> Path:
    env = os.environ.get("DAG_AGENT_HOME")
    return Path(env) if env else DEFAULT_HOME


def _read_json_retry(p: Path, attempts: int = 10, delay: float = 0.02) -> dict:
    """读JSON并容忍Windows下'读打开 vs 原子替换'互斥的瞬时OSError。"""
    last: OSError | None = None
    for i in range(attempts):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise
        except OSError as exc:
            last = exc
            time.sleep(delay * (i + 1))
    raise last  # type: ignore[misc]


class DagError(Exception):
    """业务拒绝（可预期的错误），message即"错误→动作"提示。"""


class MissionStore:
    def __init__(self, home: Path | None = None):
        self.home = Path(home) if home else default_home()

    # ---------- 路径 ----------

    def mission_dir(self, mid: str) -> Path:
        if (not mid or not _MID_RE.fullmatch(mid)
                or mid.upper() in _WINDOWS_RESERVED):
            raise DagError(f"非法任务ID '{mid}'：只允许字母数字-下划线"
                           "（1-64位，避开Windows保留名如CON/NUL）")
        return self.home / mid

    def _state_path(self, mid: str) -> Path:
        return self.mission_dir(mid) / "mission.json"

    def _events_path(self, mid: str) -> Path:
        return self.mission_dir(mid) / "events.jsonl"

    def workspace_dir(self, mid: str) -> Path:
        return self.mission_dir(mid) / "workspace"

    def board_path(self, mid: str) -> Path:
        return self.mission_dir(mid) / "dag.md"

    # ---------- 读写 ----------

    def load(self, mid: str) -> dict:
        p = self._state_path(mid)
        if not p.exists():
            raise DagError(f"任务 '{mid}' 不存在。动作：dag_create_mission 先创建")
        return _read_json_retry(p)

    def _fs_lock(self, mid: str) -> Path:
        """跨进程文件锁：O_EXCL创建型。永不超时抛错——等待+陈旧锁(>10s)抢占
        保证有界前进（持有方只在ms级save期间持锁），错误面只剩rev冲突。"""
        lockp = self._state_path(mid).with_suffix(".json.lock")
        while True:
            try:
                fd = os.open(str(lockp), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return lockp
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(lockp) > 10:
                        os.unlink(lockp)  # 陈旧锁（持有进程崩溃残留）抢占
                        continue
                except OSError:
                    continue
                time.sleep(0.01)
            except OSError as exc:
                raise DagError(f"文件锁创建失败：{exc}")

    @staticmethod
    def _fs_unlock(lockp: Path) -> None:
        try:
            os.unlink(lockp)
        except OSError:
            pass

    def _save(self, mid: str, state: dict) -> None:
        p = self._state_path(mid)
        lockp = self._fs_lock(mid)
        try:
            with _SAVE_LOCK:
                if p.exists():
                    cur = json.loads(p.read_text(encoding="utf-8"))
                    if cur.get("rev") != state.get("rev"):
                        raise DagError(
                            "并发冲突：任务状态已被其他会话更新（乐观锁rev不一致，"
                            "本次修改未写入，已验证的结果不会丢失）。"
                            "动作：重新 load/frontier 获取最新状态后重试当前操作")
                state["rev"] = state.get("rev", 0) + 1
                tmp = p.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                               encoding="utf-8")
                for i in range(20):  # 目标被并发读打开时Windows拒绝replace，重试~1s
                    try:
                        tmp.replace(p)
                        break
                    except PermissionError:
                        time.sleep(0.01 * (i + 1))
                else:
                    tmp.replace(p)
        finally:
            self._fs_unlock(lockp)

    def _log(self, mid: str, etype: str, **detail) -> None:
        rec = {"at": now_iso(), "type": etype, **detail}
        with self._events_path(mid).open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def read_events(self, mid: str, tail: int = 20) -> list[dict]:
        p = self._events_path(mid)
        if not p.exists() or tail <= 0:
            return []
        lines = p.read_text(encoding="utf-8").strip().splitlines()
        out = []
        for x in lines[-tail:]:
            try:
                out.append(json.loads(x))
            except json.JSONDecodeError:
                continue  # 并发追加中的半行，跳过
        return out

    # ---------- 创建与生长 ----------

    def create_mission(self, mid: str, name: str, brief: str,
                       root_title: str, root_statement: str,
                       root_checks: list[dict] | None = None) -> dict:
        d = self.mission_dir(mid)
        if self._state_path(mid).exists():
            raise DagError(f"任务 '{mid}' 已存在。动作：换一个ID，或 dag_status 查看")
        (d / "workspace").mkdir(parents=True, exist_ok=True)
        state = {
            "id": mid, "name": name, "brief": brief, "rev": 0,
            "created_at": now_iso(), "root": "n0", "next": 1,
            "nodes": {
                "n0": self._new_node(root_title, root_statement,
                                     deps=[], checks=root_checks, kind="task"),
            },
        }
        self._save(mid, state)
        self._log(mid, "create", name=name, brief=brief)
        self.render(mid)
        return self.status(mid)

    @staticmethod
    def _new_node(title: str, statement: str, deps: list[str],
                  checks: list[dict] | None, kind: str,
                  parent: str | None = None) -> dict:
        if kind not in ("task", "aggregate"):
            raise DagError(f"非法节点类型 '{kind}'：只允许 task/aggregate")
        for c in checks or []:
            cmd = c.get("command")
            if not (isinstance(cmd, list) and cmd and cmd[0] in ("python", "pytest")):
                raise DagError(
                    f"检查 '{c.get('name')}' 的command必须是python/pytest文件入口的argv数组，"
                    "如 [\"python\", \"check_x.py\"]（零写入纯断言）")
        return {
            "title": title, "statement": statement, "deps": list(deps),
            "checks": [dict(c) for c in (checks or [])], "kind": kind,
            "parent": parent,
            "status": "open", "blocked_reason": None, "auto_resolved": False,
            "result": None, "created_at": now_iso(),
        }

    def add_node(self, mid: str, title: str, statement: str,
                 deps: list[str], checks: list[dict] | None = None,
                 kind: str = "task") -> dict:
        state = self.load(mid)
        nid = f"n{state['next']}"
        for dep in deps:
            if dep not in state["nodes"]:
                raise DagError(
                    f"依赖 '{dep}' 不存在。动作：先 add_node 创建它"
                    "（unknown-依赖：先建依赖再建引用者）")
        state["nodes"][nid] = self._new_node(title, statement, deps, checks, kind)
        state["next"] += 1
        self._save(mid, state)
        self._log(mid, "add_node", node=nid, title=title, deps=deps, kind=kind)
        self.render(mid)
        return {"node": nid, "status": self.status(mid)}

    def decompose(self, mid: str, parent_id: str,
                  children: list[dict]) -> dict:
        """把父节点分解为若干children并转aggregate（Prove2Me sketch语义）：
        child导入的是父节点的"陈述"而非其完成——child的deps为空（无执行依赖），
        parent字段记录归属；children全部done后父节点自动闭合。这样避免
        "child等parent完成、parent又等child闭合"的死锁。"""
        state = self.load(mid)
        if parent_id not in state["nodes"]:
            raise DagError(f"父节点 '{parent_id}' 不存在。动作：dag_status 查看节点表")
        parent = state["nodes"][parent_id]
        if parent["status"] != "open":
            raise DagError(f"父节点 '{parent_id}' 状态为 {parent['status']}，只能分解open节点")
        if parent.get("auto_resolved"):
            raise DagError(f"父节点 '{parent_id}' 是自动闭合节点，不可再分解")
        if not children:
            raise DagError("children为空：分解至少要给出一个子节点")
        new_ids = []
        for ch in children:
            nid = f"n{state['next']}"
            state["nodes"][nid] = self._new_node(
                ch["title"], ch["statement"], deps=[],
                checks=ch.get("checks"), kind=ch.get("kind", "task"),
                parent=parent_id)
            state["next"] += 1
            new_ids.append(nid)
        parent["kind"] = "aggregate"   # 依赖闭合自动done（自身checks仍会在闭合时运行）
        self._save(mid, state)
        self._log(mid, "decompose", parent=parent_id, children=new_ids)
        from dag_agent.verify import CheckRunner
        self._auto_resolve(mid, state, parent_id,
                           CheckRunner(self.mission_dir(mid)))
        self.render(mid)
        return {"children": new_ids, "status": self.status(mid)}

    # ---------- 状态查询 ----------

    def frontier(self, mid: str) -> list[dict]:
        """前沿：可立即认领的节点——task、open、全部上游deps done、未blocked。
        aggregate（分解型）不可手工提交，永不入前沿。"""
        state = self.load(mid)
        out = []
        for nid, n in state["nodes"].items():
            if n["status"] != "open" or n["kind"] == "aggregate":
                continue
            if not all(state["nodes"][d]["status"] == "done" for d in n["deps"]):
                continue
            out.append(self._brief_node(nid, n))
        return sorted(out, key=lambda x: x["id"])

    @staticmethod
    def _brief_node(nid: str, n: dict) -> dict:
        return {"id": nid, "title": n["title"], "statement": n["statement"],
                "kind": n["kind"], "checks": [c["name"] for c in n["checks"]],
                "deps": n["deps"], "attempts": (n["result"] or {}).get("attempts", 0)
                if isinstance(n["result"], dict) else 0}

    def status(self, mid: str) -> dict:
        state = self.load(mid)
        nodes = state["nodes"]
        cnt = {"open": 0, "done": 0, "blocked": 0}
        for n in nodes.values():
            cnt[n["status"]] += 1
        root_done = nodes[state["root"]]["status"] == "done"
        return {
            "id": mid, "name": state["name"], "brief": state["brief"],
            "root": state["root"], "root_done": root_done,
            "mission_done": root_done,
            "counts": cnt, "total": len(nodes),
            "frontier": self.frontier(mid),
            "board": str(self.board_path(mid)),
        }

    # ---------- 提交与阻塞 ----------

    def submit(self, mid: str, node_id: str, summary: str,
               checker: "object | None" = None) -> dict:
        """提交节点结果：运行节点checks，PASS则done并级联闭合，FAIL则保持open。
        checker可注入（测试用）；默认用verify.CheckRunner（零写入守卫）。"""
        state = self.load(mid)
        if node_id not in state["nodes"]:
            raise DagError(f"节点 '{node_id}' 不存在。动作：dag_status 查看节点表")
        n = state["nodes"][node_id]
        if n["status"] == "done":
            raise DagError(f"节点 '{node_id}' 已done（提交不可变）。动作：做前沿的下一个节点")
        if n["status"] == "blocked":
            raise DagError(f"节点 '{node_id}' 被阻塞：{n['blocked_reason']}。"
                           "动作：先 dag_unblock 或交由人类决策")
        if n["kind"] == "aggregate":
            raise DagError(f"节点 '{node_id}' 是aggregate（分解型），不可手工提交——"
                           "它的完成由子任务全部done后自动闭合。动作：做它的子任务")
        undone = [d for d in n["deps"] if state["nodes"][d]["status"] != "done"]
        if undone:
            raise DagError(f"节点 '{node_id}' 依赖未完成：{undone}。"
                           "动作：先做前沿里的依赖节点")

        if checker is None:
            from dag_agent.verify import CheckRunner
            checker = CheckRunner(self.mission_dir(mid))
        verdict = checker.run(n["checks"])

        if verdict["ok"]:
            n["status"] = "done"
            n["result"] = {"summary": summary, "evidence": verdict["evidence"],
                           "at": now_iso()}
            self._save(mid, state)
            self._log(mid, "done", node=node_id, summary=summary,
                      evidence=verdict["evidence"][:300])
            closed = self._auto_resolve(mid, state, node_id, checker)
            self.render(mid)
            return {"node": node_id, "verdict": "PASS",
                    "evidence": verdict["evidence"], "auto_closed": closed,
                    "status": self.status(mid)}
        else:
            n["result"] = {"summary": summary, "attempts": n["result"]["attempts"] + 1
                           if isinstance(n["result"], dict) else 1,
                           "last_fail": verdict["evidence"], "at": now_iso()}
            self._save(mid, state)
            self._log(mid, "fail", node=node_id, summary=summary,
                      evidence=verdict["evidence"][:300])
            self.render(mid)
            return {"node": node_id, "verdict": "FAIL",
                    "evidence": verdict["evidence"],
                    "hint": verdict.get("hint", ""),
                    "next": "按evidence修复后重新 dag_submit；不要改检查脚本本身",
                    "status": self.status(mid)}

    def _auto_resolve(self, mid: str, state: dict, done_id: str,
                      checker) -> list[str]:
        """级联闭合：aggregate节点的完成=其全部children done（Prove2Me语义：
        父证明由子引理导入构成，子全证则父自动Resolved）。无children的aggregate
        永不闭合——根节点必须先分解才能交付。闭合时若配有checks则运行（组合验收）。"""
        closed, dirty = [], False
        changed = True
        while changed:
            changed = False
            for nid, n in state["nodes"].items():
                if n["status"] != "open" or n["kind"] != "aggregate":
                    continue
                children = [m for m in state["nodes"].values()
                            if m.get("parent") == nid]
                if not children or not all(m["status"] == "done"
                                           for m in children):
                    continue
                if n["checks"]:
                    v = checker.run(n["checks"])
                    if not v["ok"]:
                        n["result"] = {"attempts": (n["result"]["attempts"] + 1)
                                       if isinstance(n["result"], dict) else 1,
                                       "last_fail": v["evidence"], "at": now_iso()}
                        self._log(mid, "auto_fail", node=nid, evidence=v["evidence"][:300])
                        dirty = True
                        continue
                n["status"] = "done"
                n["auto_resolved"] = True
                n["result"] = {"summary": "自动闭合：全部子任务已完成"
                               + ("，组合检查通过" if n["checks"] else ""),
                               "evidence": "auto-resolve", "at": now_iso()}
                self._log(mid, "auto_done", node=nid)
                closed.append(nid)
                changed = True
        if closed or dirty:
            self._save(mid, state)
        return closed

    def set_readback(self, mid: str, node_id: str, text: str,
                     model: str = "") -> dict:
        """盲读回审计（Prove2Me mission_auditor语义）：由未接触任务意图的独立
        agent只看交付物/陈述盲写的自然语言证词，人类对照原始意图找差距。
        open节点可附（覆盖式更新留历史于事件日志）；done后冻结不可改。"""
        state = self.load(mid)
        if node_id not in state["nodes"]:
            raise DagError(f"节点 '{node_id}' 不存在")
        n = state["nodes"][node_id]
        if n["status"] == "done":
            raise DagError(f"节点 '{node_id}' 已done，读回证词已冻结（提交不可变）。"
                           "动作：复盘说明走事件日志或新开审计节点")
        if not (text or "").strip():
            raise DagError("证词为空：盲读回必须写'它字面上做了/断言了什么'")
        n.setdefault("readbacks", []).append(
            {"text": text.strip()[:4000], "model": model, "at": now_iso()})
        self._save(mid, state)
        self._log(mid, "readback", node=node_id, model=model,
                  chars=len(text.strip()))
        self.render(mid)
        return {"node": node_id, "readbacks": len(n["readbacks"]),
                "hint": "证词已附。人类对照原始意图找差距——差距即不忠实点"}

    def audit_prepare(self, mid: str, node_id: str) -> dict:
        """盲读回自动派发的准备步骤：生成给独立审计子agent的自包含提示。
        结构性保证盲性——prompt只含节点陈述+工作产物路径+审计员规则（AUDITOR.md），
        绝不含brief/意图。宿主把prompt原样交给一个全新上下文的子agent，
        收回证词后用 set_readback 附着。"""
        state = self.load(mid)
        if node_id not in state["nodes"]:
            raise DagError(f"节点 '{node_id}' 不存在")
        n = state["nodes"][node_id]
        template = (Path(__file__).resolve().parent.parent / "AUDITOR.md")
        rules = (template.read_text(encoding="utf-8")
                 if template.exists() else "（AUDITOR.md缺失：按译物不译意原则盲写）")
        ws = self.workspace_dir(mid)
        deliverables = sorted(
            p.relative_to(self.mission_dir(mid)).as_posix()
            for p in ws.rglob("*") if p.is_file())
        prompt = (
            "你是独立盲读回审计员。依据以下审计规则，对目标节点写出读回证词。\n\n"
            f"{rules}\n\n## 待审对象\n\n"
            f"节点陈述（字面原文）：{n['statement']}\n"
            f"工作产物文件（相对任务目录）：{deliverables or '（无，审计陈述本体）'}\n"
            f"产物目录：{ws}\n\n"
            "只依据上述物证写证词，不猜测任何未写明的意图。")
        return {"node": node_id, "prompt": prompt,
                "deliverables": deliverables,
                "note": "把prompt原样交给全新上下文的子agent；不要附加brief/意图"}

    def retry(self, mid: str, node_id: str | None = None) -> dict:
        """重试闭合：aggregate的children全done但组合检查失败（auto_fail）后，
        修复产物再调用本入口重新运行闭合检查。node_id缺省=根节点。"""
        state = self.load(mid)
        target = node_id or state["root"]
        if target not in state["nodes"]:
            raise DagError(f"节点 '{target}' 不存在")
        n = state["nodes"][target]
        if n["kind"] != "aggregate" or n["status"] != "open":
            raise DagError(f"节点 '{target}' 不是open的aggregate节点，无需重试闭合")
        from dag_agent.verify import CheckRunner
        closed = self._auto_resolve(mid, state, target,
                                    CheckRunner(self.mission_dir(mid)))
        self.render(mid)
        return {"retried": target, "auto_closed": closed,
                "status": self.status(mid)}

    def block(self, mid: str, node_id: str, reason: str) -> dict:
        state = self.load(mid)
        if node_id not in state["nodes"]:
            raise DagError(f"节点 '{node_id}' 不存在")
        n = state["nodes"][node_id]
        if n["status"] == "done":
            raise DagError(f"节点 '{node_id}' 已done，无需阻塞")
        n["status"] = "blocked"
        n["blocked_reason"] = reason
        self._save(mid, state)
        self._log(mid, "block", node=node_id, reason=reason)
        self.render(mid)
        return {"node": node_id, "status": "blocked",
                "hint": "阻塞节点已移出前沿；这是人类决策点（等外部/换路径/降级）"}

    def unblock(self, mid: str, node_id: str) -> dict:
        state = self.load(mid)
        if node_id not in state["nodes"]:
            raise DagError(f"节点 '{node_id}' 不存在")
        n = state["nodes"][node_id]
        if n["status"] != "blocked":
            raise DagError(f"节点 '{node_id}' 状态为 {n['status']}，无需解锁")
        n["status"] = "open"
        n["blocked_reason"] = None
        self._save(mid, state)
        self._log(mid, "unblock", node=node_id)
        from dag_agent.verify import CheckRunner
        closed = self._auto_resolve(mid, state, node_id,
                                    CheckRunner(self.mission_dir(mid)))
        self.render(mid)
        return {"node": node_id, "status": "open", "auto_closed": closed}

    # ---------- 渲染 ----------

    def render(self, mid: str) -> str:
        from dag_agent.render import render_board, render_index
        state = self.load(mid)
        render_board(self, mid, state)
        render_index(self)
        return str(self.board_path(mid))
