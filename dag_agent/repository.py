"""v3 SQLite 存储层：单写短事务、WAL、状态与事件同事务。

原则：模型执行/检查等长操作一律在事务外；事务内只做读写与状态迁移。
Windows 注意：WAL + busy_timeout 承接 v0.1 的读/替换互斥教训。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from .models import (Artifact, Assessment, Attempt, Dependency, Event, Mission,
                     Node, Review, Snapshot)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS missions(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, brief TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  goal_version INTEGER NOT NULL DEFAULT 1,
  closure_basis TEXT NOT NULL DEFAULT 'machine',
  anchor TEXT NOT NULL DEFAULT '',
  budget_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nodes(
  id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(id),
  title TEXT NOT NULL, statement TEXT DEFAULT '',
  parent_id TEXT, kind TEXT NOT NULL DEFAULT 'task',
  status TEXT NOT NULL DEFAULT 'pending',
  check_cmd TEXT, check_version INTEGER NOT NULL DEFAULT 1,
  acceptance_json TEXT,
  priority INTEGER NOT NULL DEFAULT 0,
  strength_actual TEXT NOT NULL DEFAULT '',
  accepted_artifact_version INTEGER,
  declared_artifacts_json TEXT NOT NULL DEFAULT '[]',
  revision INTEGER NOT NULL DEFAULT 1,
  group_id TEXT, group_order INTEGER NOT NULL DEFAULT 0,
  switch_after INTEGER NOT NULL DEFAULT 1,
  exam_files_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_mission ON nodes(mission_id, status);
CREATE TABLE IF NOT EXISTS dependencies(
  node_id TEXT NOT NULL REFERENCES nodes(id),
  depends_on TEXT NOT NULL REFERENCES nodes(id),
  expected_version INTEGER,
  created_at TEXT NOT NULL,
  PRIMARY KEY(node_id, depends_on)
);
CREATE TABLE IF NOT EXISTS attempts(
  id TEXT PRIMARY KEY, node_id TEXT NOT NULL REFERENCES nodes(id),
  mission_id TEXT NOT NULL,
  backend TEXT NOT NULL DEFAULT 'stub',
  status TEXT NOT NULL DEFAULT 'running',
  workdir TEXT DEFAULT '', pid INTEGER, heartbeat_at TEXT DEFAULT '',
  resources_json TEXT DEFAULT '{}', result_summary TEXT DEFAULT '',
  input_binding_json TEXT NOT NULL DEFAULT '[]',
  claim_fingerprint_json TEXT NOT NULL DEFAULT '{}',
  category TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL, ended_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempts_node ON attempts(node_id, status);
CREATE TABLE IF NOT EXISTS artifacts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id TEXT NOT NULL REFERENCES attempts(id),
  node_id TEXT NOT NULL, mission_id TEXT NOT NULL,
  rel_path TEXT NOT NULL, content_hash TEXT NOT NULL,
  version INTEGER NOT NULL, snapshot_id INTEGER,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_node ON artifacts(node_id, version);
CREATE TABLE IF NOT EXISTS snapshots(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mission_id TEXT NOT NULL, node_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
  artifact_version INTEGER NOT NULL,
  node_revision INTEGER NOT NULL DEFAULT 1,
  goal_version INTEGER NOT NULL DEFAULT 1,
  manifest_hash TEXT NOT NULL DEFAULT '',
  files_json TEXT NOT NULL DEFAULT '[]',
  valid INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_node ON snapshots(node_id, id);
CREATE TABLE IF NOT EXISTS assessments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
  artifact_version INTEGER NOT NULL,
  goal_version INTEGER NOT NULL,
  check_cmd TEXT NOT NULL, check_version INTEGER NOT NULL,
  method TEXT NOT NULL, evidence TEXT DEFAULT '',
  verdict TEXT NOT NULL, authoritative INTEGER NOT NULL DEFAULT 1,
  invalid INTEGER NOT NULL DEFAULT 0,
  snapshot_id INTEGER, check_digest TEXT NOT NULL DEFAULT '',
  input_binding_hash TEXT NOT NULL DEFAULT '', env_note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id TEXT NOT NULL, verdict TEXT NOT NULL,
  note TEXT DEFAULT '', blind INTEGER NOT NULL DEFAULT 1,
  snapshot_id INTEGER, basis TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mission_id TEXT NOT NULL, at TEXT NOT NULL,
  type TEXT NOT NULL, node_id TEXT, payload_json TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_mission ON events(mission_id, id);
CREATE TABLE IF NOT EXISTS action_receipts(
  key TEXT PRIMARY KEY, at TEXT NOT NULL, result_json TEXT DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'done'
);
CREATE TABLE IF NOT EXISTS split_proposals(
  id TEXT PRIMARY KEY,
  mission_id TEXT NOT NULL,
  node_id TEXT NOT NULL,
  source_attempt_id TEXT NOT NULL UNIQUE,
  raw_sha256 TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  raw_json TEXT NOT NULL,
  fingerprint_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  reject_reason TEXT NOT NULL DEFAULT '',
  plan_backend TEXT NOT NULL DEFAULT '',
  plan_resources_json TEXT NOT NULL DEFAULT '{}',
  plan_output_json TEXT NOT NULL DEFAULT '',
  children_created_json TEXT NOT NULL DEFAULT '[]',
  nodes_added INTEGER NOT NULL DEFAULT 0,
  plan_attempts INTEGER NOT NULL DEFAULT 0,
  has_delivery INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_split_proposals_mission
  ON split_proposals(mission_id, status);
CREATE INDEX IF NOT EXISTS idx_split_proposals_node
  ON split_proposals(node_id, status);
CREATE TABLE IF NOT EXISTS run_control(
  id INTEGER PRIMARY KEY CHECK(id=1),
  stop_requested INTEGER NOT NULL DEFAULT 0,
  hard_stop INTEGER NOT NULL DEFAULT 0,
  generation INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS node_exam_freeze(
  node_id TEXT PRIMARY KEY,
  exam_digest TEXT NOT NULL,
  proposal_id TEXT,
  frozen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dep_bindings(
  node_id TEXT NOT NULL,
  depends_on TEXT NOT NULL,
  snapshot_id INTEGER NOT NULL,
  manifest_hash TEXT NOT NULL,
  files_json TEXT NOT NULL,
  artifact_version INTEGER,
  PRIMARY KEY(node_id, depends_on)
);
"""

# v3.2→v3.3 迁移：旧库补列（幂等）。加性变更，不改旧行语义。
_MIGRATIONS_EXTRA = (
    "ALTER TABLE split_proposals ADD COLUMN has_delivery INTEGER NOT NULL DEFAULT 0",
    # F2（Codex 三轮）：dep_bindings 持久完整文件元信息（rel/sha/size）——
    # 仅 manifest_hash 字符串+文件名清单可被同 ID 元数据联合改写欺骗
    "ALTER TABLE dep_bindings ADD COLUMN files_meta_json TEXT NOT NULL DEFAULT '[]'",
    # N5（Codex 四轮）：恢复配置持久化——RunManager 有界回收的轻量
    # params 落库，kick 在内存归档淘汰后仍可从任务库恢复
    "ALTER TABLE run_control ADD COLUMN params_json TEXT NOT NULL DEFAULT ''",
)


def _backfill_dep_bindings_meta(conn) -> int:
    """N4（Codex 四轮）：健康旧库的 dep_bindings 回填 files_meta_json。
    仅当快照清单规范化重算==旧冻结 manifest_hash 时回填（验证通过才
    升级，不从最新指针静默换绑）；验证失败保持 '[]'（认领 fail-closed
    拒绝，人工处置）。幂等：已回填行不再匹配 WHERE。返回回填行数。"""
    try:
        from .artifacts import manifest_hash_of as _mho
    except Exception:
        return 0
    rows = conn.execute(
        "SELECT b.node_id, b.depends_on, b.snapshot_id, b.manifest_hash,"
        " s.files_json AS sfiles FROM dep_bindings b"
        " LEFT JOIN snapshots s ON s.id=b.snapshot_id"
        " WHERE b.files_meta_json='[]' AND b.snapshot_id IS NOT NULL"
    ).fetchall()
    n = 0
    for r in rows:
        if r["sfiles"] is None:
            continue
        try:
            meta = json.loads(r["sfiles"] or "[]")
        except Exception:
            continue
        if not meta:
            continue
        try:
            ok = _mho(meta) == r["manifest_hash"]
        except Exception:
            ok = False
        if not ok:
            continue  # 不一致旧快照不自动"修成合法"——保持空=认领拒绝
        conn.execute(
            "UPDATE dep_bindings SET files_meta_json=? WHERE node_id=?"
            " AND depends_on=? AND snapshot_id=?",
            (r["sfiles"], r["node_id"], r["depends_on"], r["snapshot_id"]))
        n += 1
    return n

# v3.1→v3.2 迁移：旧库补列（幂等——列已存在则跳过）。加性变更，不改旧行语义。
_MIGRATIONS = (
    "ALTER TABLE missions ADD COLUMN closure_basis TEXT NOT NULL DEFAULT 'machine'",
    "ALTER TABLE missions ADD COLUMN anchor TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE missions ADD COLUMN budget_json TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE nodes ADD COLUMN acceptance_json TEXT",
    "ALTER TABLE nodes ADD COLUMN priority INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE nodes ADD COLUMN strength_actual TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE nodes ADD COLUMN revision INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE nodes ADD COLUMN group_id TEXT",
    "ALTER TABLE nodes ADD COLUMN group_order INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE nodes ADD COLUMN switch_after INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE nodes ADD COLUMN exam_files_json TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE attempts ADD COLUMN input_binding_json TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE attempts ADD COLUMN claim_fingerprint_json TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE attempts ADD COLUMN category TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE artifacts ADD COLUMN snapshot_id INTEGER",
    "ALTER TABLE assessments ADD COLUMN authoritative INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE assessments ADD COLUMN invalid INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE assessments ADD COLUMN snapshot_id INTEGER",
    "ALTER TABLE assessments ADD COLUMN check_digest TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE assessments ADD COLUMN input_binding_hash TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE assessments ADD COLUMN env_note TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE reviews ADD COLUMN snapshot_id INTEGER",
    "ALTER TABLE reviews ADD COLUMN basis TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE action_receipts ADD COLUMN status TEXT NOT NULL DEFAULT 'done'",
)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


_active_ledger_instances: dict[Path, set[int]] = {}
_active_ledgers_guard = threading.Lock()


def register_active_ledger(path: Path | str, instance: object = None) -> None:
    try:
        p = Path(path).resolve()
        with _active_ledgers_guard:
            s = _active_ledger_instances.setdefault(p, set())
            inst_id = id(instance) if instance is not None else 0
            s.add(inst_id)
    except Exception:
        pass


def unregister_active_ledger(path: Path | str, instance: object = None) -> None:
    try:
        p = Path(path).resolve()
        with _active_ledgers_guard:
            s = _active_ledger_instances.get(p)
            if s is not None:
                inst_id = id(instance) if instance is not None else 0
                s.discard(inst_id)
                if not s:
                    _active_ledger_instances.pop(p, None)
    except Exception:
        pass


def is_active_ledger_file(path: Path | str) -> bool:
    """C1（Codex 六轮）/ R2（第七轮）：判定目标文件是否为平台活跃任务账本或其伴生文件（-wal / -shm / -journal）。
    排除对象严格绑定平台已知的真实路径/文件身份与实例所有权生命周期，多实例共享同库时关闭其中一个不影响仍活跃的实例。"""
    try:
        cand = Path(path).resolve()
        with _active_ledgers_guard:
            active = set(_active_ledger_instances.keys())
        for lpath in active:
            if cand == lpath or (cand.parent == lpath.parent and cand.name.lower() == lpath.name.lower()):
                return True
            if cand.parent == lpath.parent:
                if cand.name.lower() in (
                    f"{lpath.name}-wal".lower(),
                    f"{lpath.name}-shm".lower(),
                    f"{lpath.name}-journal".lower(),
                    ".run.lock",
                ):
                    return True
        return False
    except Exception:
        return False


class Repository:
    """每任务一个 SQLite 文件：<home>/<mid>/mission.db。

    线程安全：写走单写锁+短事务（BEGIN IMMEDIATE）；读线程本地连接
    （WAL 多读一写）——共享单连接时事务外裸读与写事务交错会触发
    InterfaceError / 中间态读（T2 规模压测并发 10 抓出的缺陷）。
    """

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._closed = False
        self._conns_guard = threading.Lock()
        self._local = threading.local()
        self._all_conns: dict[int, sqlite3.Connection] = {}
        registered = False
        try:
            main = self._new_conn()
            self._local.conn = main
            self._all_conns[threading.get_ident()] = main
            with self.tx() as cur:
                cur.executescript(_SCHEMA)
                for ddl in _MIGRATIONS:
                    try:
                        cur.execute(ddl)
                    except sqlite3.OperationalError:
                        pass  # 列已存在（旧库已迁移过）
                for ddl in _MIGRATIONS_EXTRA:
                    try:
                        cur.execute(ddl)
                    except sqlite3.OperationalError:
                        pass
                # N4（Codex 四轮）：健康旧库 dep_bindings 元信息回填
                # （验证旧冻结 manifest 通过才回填；幂等；失败保留原记录）
                try:
                    _backfill_dep_bindings_meta(cur)
                except Exception:
                    pass  # 回填失败不阻断开库——认领 fail-closed 兜底
                cur.execute("PRAGMA user_version=3")  # v3.2 schema（协议01/02/03）
            register_active_ledger(self.db_path, instance=self)
            registered = True
        except Exception:
            if registered:
                unregister_active_ledger(self.db_path, instance=self)
            for conn in list(self._all_conns.values()):
                try:
                    conn.close()
                except Exception:
                    pass
            self._all_conns.clear()
            self._closed = True
            raise

    def _new_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path),
                               check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @property
    def _conn(self) -> sqlite3.Connection:
        """线程本地连接：首次访问懒建并登记，同线程复用。"""
        if self._closed:
            raise RuntimeError("Repository 已关闭")
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_conn()
            self._local.conn = conn
            with self._conns_guard:
                self._all_conns[threading.get_ident()] = conn
        return conn

    def close(self):
        self._closed = True
        unregister_active_ledger(self.db_path, instance=self)
        with self._conns_guard:
            conns = list(self._all_conns.values())
            self._all_conns.clear()
        for c in conns:
            try:
                c.close()
            except Exception:
                pass

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Cursor]:
        """单写短事务：BEGIN IMMEDIATE，异常回滚。禁止在事务内做模型调用/检查。"""
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN IMMEDIATE")
                yield cur
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def get_run_control(self) -> dict:
        """运行权限旗标（R2 停止串行化：stop 写旗标与 apply 读旗标共用
        SQLite 写锁，构成线性化点）。读取不得取 self._lock——唯一调用点
        在 apply 写事务内（已持锁；同连接同事务可见最新已提交状态）。"""
        row = self._conn.execute(
            "SELECT stop_requested, hard_stop, generation FROM run_control"
            " WHERE id=1").fetchone()
        if row is None:
            return {"stop_requested": False, "hard_stop": False,
                    "generation": 0}
        return {"stop_requested": bool(row["stop_requested"]),
                "hard_stop": bool(row["hard_stop"]),
                "generation": int(row["generation"])}

    # ---- missions ----

    def insert_mission(self, m: Mission, cur: sqlite3.Cursor):
        cur.execute(
            "INSERT INTO missions(id,name,brief,status,goal_version,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (m.id, m.name, m.brief, m.status, m.goal_version, m.created_at, m.updated_at),
        )

    def get_mission(self, mid: str) -> Mission:
        row = self._conn.execute("SELECT * FROM missions WHERE id=?", (mid,)).fetchone()
        if row is None:
            raise KeyError(f"任务不存在：{mid}")
        return Mission(**dict(row))

    def update_mission(self, mid: str, **fields):
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as cur:
            cur.execute(
                f"UPDATE missions SET {sets}, updated_at=? WHERE id=?",
                (*fields.values(), now(), mid),
            )

    # ---- nodes ----

    def insert_node(self, n: Node, cur: sqlite3.Cursor):
        cur.execute(
            "INSERT INTO nodes(id,mission_id,title,statement,parent_id,kind,status,"
            "check_cmd,check_version,declared_artifacts_json,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (n.id, n.mission_id, n.title, n.statement, n.parent_id, n.kind, n.status,
             n.check_cmd, n.check_version, getattr(n, "declared_artifacts_json", "[]"),
             n.created_at, n.updated_at),
        )

    def get_node(self, nid: str) -> Node:
        row = self._conn.execute("SELECT * FROM nodes WHERE id=?", (nid,)).fetchone()
        if row is None:
            raise KeyError(f"节点不存在：{nid}")
        return Node(**dict(row))

    def list_nodes(self, mid: str) -> list[Node]:
        rows = self._conn.execute(
            "SELECT * FROM nodes WHERE mission_id=? ORDER BY id", (mid,)
        ).fetchall()
        return [Node(**dict(r)) for r in rows]

    def update_node(self, nid: str, **fields):
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as cur:
            cur.execute(
                f"UPDATE nodes SET {sets}, updated_at=? WHERE id=?",
                (*fields.values(), now(), nid),
            )

    def next_node_id(self, cur: sqlite3.Cursor) -> str:
        row = cur.execute("SELECT COUNT(*) AS c FROM nodes").fetchone()
        return f"n{row['c'] + 1}"

    # ---- dependencies ----

    def insert_dep(self, d: Dependency, cur: sqlite3.Cursor):
        cur.execute(
            "INSERT INTO dependencies(node_id,depends_on,expected_version,created_at)"
            " VALUES(?,?,?,?)",
            (d.node_id, d.depends_on, d.expected_version, d.created_at),
        )

    def deps_of(self, nid: str) -> list[Dependency]:
        rows = self._conn.execute(
            "SELECT * FROM dependencies WHERE node_id=? ORDER BY depends_on", (nid,)
        ).fetchall()
        return [Dependency(**dict(r)) for r in rows]

    # ---- attempts ----

    def insert_attempt(self, a: Attempt, cur: sqlite3.Cursor):
        cur.execute(
            "INSERT INTO attempts(id,node_id,mission_id,backend,status,workdir,pid,"
            "heartbeat_at,resources_json,result_summary,started_at,ended_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (a.id, a.node_id, a.mission_id, a.backend, a.status, a.workdir, a.pid,
             a.heartbeat_at, a.resources_json, a.result_summary, a.started_at, a.ended_at),
        )

    def get_attempt(self, aid: str) -> Attempt:
        row = self._conn.execute("SELECT * FROM attempts WHERE id=?", (aid,)).fetchone()
        if row is None:
            raise KeyError(f"尝试不存在：{aid}")
        return Attempt(**dict(row))

    def update_attempt(self, aid: str, **fields):
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as cur:
            cur.execute(f"UPDATE attempts SET {sets} WHERE id=?", (*fields.values(), aid))

    def running_attempt_of(self, nid: str) -> Optional[Attempt]:
        row = self._conn.execute(
            "SELECT * FROM attempts WHERE node_id=? AND status='running'", (nid,)
        ).fetchone()
        return Attempt(**dict(row)) if row else None

    # ---- artifacts / assessments / reviews / events / receipts ----

    def insert_artifact(self, a: Artifact, cur: sqlite3.Cursor):
        cur.execute(
            "INSERT INTO artifacts(attempt_id,node_id,mission_id,rel_path,content_hash,"
            "version,created_at) VALUES(?,?,?,?,?,?,?)",
            (a.attempt_id, a.node_id, a.mission_id, a.rel_path, a.content_hash,
             a.version, a.created_at),
        )

    def next_artifact_version(self, cur: sqlite3.Cursor, nid: str) -> int:
        row = cur.execute(
            "SELECT COALESCE(MAX(version),0)+1 AS v FROM artifacts WHERE node_id=?", (nid,)
        ).fetchone()
        return row["v"]

    def artifacts_of(self, nid: str) -> list[Artifact]:
        rows = self._conn.execute(
            "SELECT * FROM artifacts WHERE node_id=? ORDER BY version", (nid,)
        ).fetchall()
        return [Artifact(**dict(r)) for r in rows]

    # ---- v3.2 快照（协议01：manifest 表） ----

    def insert_snapshot(self, s: Snapshot, cur: sqlite3.Cursor) -> int:
        cur.execute(
            "INSERT INTO snapshots(mission_id,node_id,attempt_id,artifact_version,"
            "node_revision,goal_version,manifest_hash,files_json,valid,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (s.mission_id, s.node_id, s.attempt_id, s.artifact_version,
             s.node_revision, s.goal_version, s.manifest_hash, s.files_json,
             s.valid, s.created_at))
        return cur.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]

    def snapshots_of(self, nid: str) -> list[Snapshot]:
        rows = self._conn.execute(
            "SELECT * FROM snapshots WHERE node_id=? ORDER BY id", (nid,)
        ).fetchall()
        return [Snapshot(**dict(r)) for r in rows]

    def latest_valid_snapshot(self, nid: str) -> Optional[Snapshot]:
        row = self._conn.execute(
            "SELECT * FROM snapshots WHERE node_id=? AND valid=1"
            " ORDER BY id DESC LIMIT 1", (nid,)).fetchone()
        return Snapshot(**dict(row)) if row else None

    def snapshot_by_id(self, sid: int) -> Optional[Snapshot]:
        row = self._conn.execute(
            "SELECT * FROM snapshots WHERE id=?", (sid,)).fetchone()
        return Snapshot(**dict(row)) if row else None

    def insert_assessment(self, s: Assessment, cur: sqlite3.Cursor):
        cur.execute(
            "INSERT INTO assessments(node_id,attempt_id,artifact_version,goal_version,"
            "check_cmd,check_version,method,evidence,verdict,authoritative,"
            "snapshot_id,check_digest,input_binding_hash,env_note,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (s.node_id, s.attempt_id, s.artifact_version, s.goal_version, s.check_cmd,
             s.check_version, s.method, s.evidence, s.verdict,
             getattr(s, "authoritative", 1), getattr(s, "snapshot_id", None),
             getattr(s, "check_digest", ""), getattr(s, "input_binding_hash", ""),
             getattr(s, "env_note", ""), s.created_at),
        )

    def assessments_of(self, nid: str) -> list[Assessment]:
        rows = self._conn.execute(
            "SELECT * FROM assessments WHERE node_id=? ORDER BY id", (nid,)
        ).fetchall()
        return [Assessment(**dict(r)) for r in rows]

    # ---- v3.1：主观层审阅（append-only，永不与机器验收合并） ----

    def insert_review(self, r: Review, cur: sqlite3.Cursor):
        cur.execute(
            "INSERT INTO reviews(node_id,verdict,note,blind,created_at)"
            " VALUES(?,?,?,?,?)",
            (r.node_id, r.verdict, r.note, r.blind, r.created_at))

    def reviews_of(self, nid: str) -> list[Review]:
        rows = self._conn.execute(
            "SELECT * FROM reviews WHERE node_id=? ORDER BY id", (nid,)).fetchall()
        return [Review(**dict(r)) for r in rows]

    def search_nodes(self, mid: str, tokens: list[str], limit: int = 20) -> list[Node]:
        """v3.1 外部记忆检索：按 token 在 title/statement 里 LIKE 匹配（OR 语义）。"""
        if not tokens:
            return []
        conds = " OR ".join(
            "(title LIKE ? OR statement LIKE ?)" for _ in tokens)
        params: list[str] = []
        for t in tokens:
            params += [f"%{t}%", f"%{t}%"]
        rows = self._conn.execute(
            f"SELECT * FROM nodes WHERE mission_id=? AND ({conds})"
            f" ORDER BY id LIMIT ?", (mid, *params, limit)).fetchall()
        return [Node(**dict(r)) for r in rows]

    def append_event(self, cur: sqlite3.Cursor, mid: str, etype: str,
                     nid: Optional[str] = None, **payload):
        cur.execute(
            "INSERT INTO events(mission_id,at,type,node_id,payload_json) VALUES(?,?,?,?,?)",
            (mid, now(), etype, nid, json.dumps(payload, ensure_ascii=False)),
        )

    def record_event(self, mid: str, etype: str, nid: Optional[str] = None, **payload):
        with self.tx() as cur:
            self.append_event(cur, mid, etype, nid=nid, **payload)

    def events_of(self, mid: str) -> list[Event]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE mission_id=? ORDER BY id", (mid,)
        ).fetchall()
        return [Event(**dict(r)) for r in rows]

    def receipt_first(self, key: str, result_json: str = "{}") -> bool:
        """动作回执：首次登记返回 True；已存在且 status='done' 返回 False
        （幂等重放）；已存在且 status='failed' → 删除重登返回 True（R2：失败
        回执不得阻断重试实际执行）。"""
        with self.tx() as cur:
            try:
                cur.execute(
                    "INSERT INTO action_receipts(key,at,result_json) VALUES(?,?,?)",
                    (key, now(), result_json),
                )
                return True
            except sqlite3.IntegrityError:
                row = cur.execute(
                    "SELECT status FROM action_receipts WHERE key=?",
                    (key,)).fetchone()
                if row is not None and row["status"] == "failed":
                    cur.execute("DELETE FROM action_receipts WHERE key=?", (key,))
                    cur.execute(
                        "INSERT INTO action_receipts(key,at,result_json)"
                        " VALUES(?,?,?)", (key, now(), result_json))
                    return True
                return False

    def receipt_get(self, key: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT result_json FROM action_receipts WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row["result_json"]) if row else None

    # ---- 自动生长提案（A-G1：持久队列，重启不清零） ----

    def insert_split_proposal(self, p: dict, cur: sqlite3.Cursor):
        cur.execute(
            "INSERT INTO split_proposals(id,mission_id,node_id,"
            "source_attempt_id,raw_sha256,content_sha256,raw_json,"
            "fingerprint_json,status,reject_reason,plan_backend,"
            "plan_resources_json,plan_output_json,children_created_json,"
            "nodes_added,plan_attempts,has_delivery,created_at,decided_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p["id"], p["mission_id"], p["node_id"], p["source_attempt_id"],
             p["raw_sha256"], p["content_sha256"], p["raw_json"],
             p["fingerprint_json"], p.get("status", "queued"),
             p.get("reject_reason", ""), p.get("plan_backend", ""),
             p.get("plan_resources_json", "{}"), p.get("plan_output_json", ""),
             p.get("children_created_json", "[]"),
             int(p.get("nodes_added", 0)), int(p.get("plan_attempts", 0)),
             int(p.get("has_delivery", 0)),
             p["created_at"], p.get("decided_at")))

    def get_split_proposal(self, pid: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM split_proposals WHERE id=?", (pid,)).fetchone()

    def split_proposal_by_attempt(self, aid: str) -> Optional[sqlite3.Row]:
        """同源 attempt 唯一约束的查询面（幂等/冲突判定）。"""
        return self._conn.execute(
            "SELECT * FROM split_proposals WHERE source_attempt_id=?",
            (aid,)).fetchone()

    def list_split_proposals(self, mid: str,
                             status: Optional[str] = None) -> list[sqlite3.Row]:
        if status:
            return self._conn.execute(
                "SELECT * FROM split_proposals WHERE mission_id=? AND status=?"
                " ORDER BY created_at, id", (mid, status)).fetchall()
        return self._conn.execute(
            "SELECT * FROM split_proposals WHERE mission_id=?"
            " ORDER BY created_at, id", (mid,)).fetchall()

    def queued_split_proposals(self, mid: str) -> list[sqlite3.Row]:
        """可规划提案查询（F5：留审提案持久休眠，不进可规划队列）——
        只取：①父 waiting_split（正常规划）；②父已流转且无交付（迟到，
        须到规划层显式拒绝留痕）。有交付且父非 waiting_split（等判卷
        分诊/维修人工处置）的提案在此休眠，不派发、不占规划位。"""
        return self._conn.execute(
            "SELECT p.* FROM split_proposals p JOIN nodes n ON"
            " p.node_id=n.id WHERE p.mission_id=? AND p.status='queued'"
            " AND (n.status='waiting_split' OR p.has_delivery=0)"
            " ORDER BY p.created_at, p.id", (mid,)).fetchall()

    def update_split_proposal(self, pid: str, **fields):
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as cur:
            cur.execute(
                f"UPDATE split_proposals SET {sets} WHERE id=?",
                (*fields.values(), pid))
