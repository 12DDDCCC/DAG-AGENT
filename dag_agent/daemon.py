"""v3.2 运行接线·常驻运行管理器（任务书第4步"本地常驻平台真实使用行为"）。

- 一个 mission 仅有一个有效调度器：重复 start 拒绝（互斥）。
- 启动运行后，同一进程的 MCP 会话仍可查询/调权/暂停/恢复/取消
  （工具走各自短事务仓储；调度器每 tick 重读账本——干预下一可派发 tick 生效，
  priority 不抢占已执行会话）。
- 停止语义：drain=不再派发、在途有界结束；kill=drain+取消在途尝试并请求
  后端终止进程树；以 stopped=True 和调度线程退出为完成依据。
- 单服务异常重启：start 时先 recover_mission（本服务假设下一切 running
  视孤儿收割）——旧进程不获得新提交权（旧 attempt 被 orphaned，其提交被
  状态机拒绝）。
- 投影自动更新：每 tick 经 render_mission_versioned（版本守卫+原子发布）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from pathlib import Path

from .graph import Graph
from .projection import render_mission_versioned
from .recovery import RetryPolicy, recover_mission
from .repository import Repository, now as _now
from .scheduler import Budget, Scheduler


_ACTIVE_DB_LOCKS: dict[str, dict] = {}
_DB_LOCKS_GUARD = threading.Lock()
_LOCK_BYTE_OFFSET = 65536


class _RunFileLock:
    """跨进程任务库互斥锁（<db.parent>/.run.lock）。

    - Windows 使用 msvcrt.locking 在偏移 65536 处加 1 字节非阻塞独占锁
      （不影响 0..N 处 JSON 元数据被任意进程正常读取）；
    - POSIX 使用 fcntl.flock(LOCK_EX | LOCK_NB)；
    - 同时维护进程内 _ACTIVE_DB_LOCKS 表，防止同进程内不同 RunManager
      实例对同一 mission.db 重复启动；
    - 进程崩溃或被强杀（taskkill /F / SIGKILL）时，OS 内核在文件描述符
      关闭瞬间自动释放文件锁，不会产生死锁残留。
    """

    def __init__(self, lock_path: Path, fh, key: str, meta: dict):
        self.lock_path = lock_path
        self._fh = fh
        self._key = key
        self.meta = meta
        self._released = False
        self._guard = threading.Lock()

    @classmethod
    def acquire(cls, db_path: Path, mid: str) -> tuple["_RunFileLock | None", str | None, dict | None]:
        lock_path = Path(db_path).resolve().parent / ".run.lock"
        key = os.path.normcase(str(lock_path))
        meta = {
            "pid": os.getpid(),
            "mission_id": mid,
            "db_path": str(Path(db_path).resolve()),
            "acquired_at": _now(),
        }
        with _DB_LOCKS_GUARD:
            existing = _ACTIVE_DB_LOCKS.get(key)
            if existing is not None:
                return (
                    None,
                    f"任务 {mid} 已有运行中的调度器（锁文件 {lock_path.name} 已被当前进程 PID={existing.get('pid')} 持有）",
                    dict(existing),
                )
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fh = None
            try:
                fh = open(lock_path, "a+b")
                if os.name == "nt":
                    import msvcrt
                    fh.seek(_LOCK_BYTE_OFFSET)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                if fh is not None:
                    try:
                        fh.close()
                    except Exception:
                        pass
                holder = cls._read_holder(lock_path)
                h_desc = f"（持有进程 PID={holder.get('pid')}）" if holder.get("pid") else ""
                return (
                    None,
                    f"任务 {mid} 已有运行中的调度器（跨进程锁 {lock_path.name} 被占用{h_desc}）",
                    holder,
                )
            try:
                fh.seek(0)
                fh.truncate()
                fh.write((json.dumps(meta, ensure_ascii=False) + "\n").encode("utf-8"))
                fh.flush()
            except OSError:
                pass
            _ACTIVE_DB_LOCKS[key] = meta
            return cls(lock_path, fh, key, meta), None, None

    @staticmethod
    def _read_holder(lock_path: Path) -> dict:
        try:
            raw = lock_path.read_text(encoding="utf-8", errors="replace").strip()
            if raw:
                obj = json.loads(raw.splitlines()[0])
                if isinstance(obj, dict):
                    return obj
        except Exception:
            pass
        return {}

    def release(self) -> None:
        with self._guard:
            if self._released:
                return
            self._released = True
        with _DB_LOCKS_GUARD:
            _ACTIVE_DB_LOCKS.pop(self._key, None)
        if self._fh is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    self._fh.seek(_LOCK_BYTE_OFFSET)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        # 保持锁文件稳定存在，严禁 unlink：持锁状态完全由 OS 字节锁/文件句柄决定，
        # 彻底避免 POSIX inode 竞争和 Windows 删除竞争。


class RunManager:
    """运行管理器：mid → 运行句柄，同时通过 <db_dir>/.run.lock 提供跨进程任务库互斥保护。"""

    _instance: "RunManager | None" = None
    _instance_lock = threading.Lock()

    def __init__(self, home: Path):
        self.home = Path(home)
        self._runs: dict[str, dict] = {}
        self._archive: dict[str, dict] = {}   # F4：回收条目的轻量恢复配置
        self._ARCHIVE_MAX = 32
        self._lock = threading.RLock()

    @classmethod
    def get(cls, home: Path | None = None) -> "RunManager":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(Path(home) if home else
                                    Path.home() / ".dag-agent" / "v3" / "missions")
            return cls._instance

    def cleanup_finished(self, keep: int = 8) -> int:
        """生命周期有界回收（R6/Codex 复核实证：每个保留的完成运行条目
        持有 ~11 个 OS 句柄——1h Soak 句柄斜率 20.3/min 增长的主因）。
        仅回收线程已死的条目：关池→关库→弹出；重对象释放，**轻量启动
        配置/终局快照转入 _archive**（F4/Codex 三轮：线程终止≠mission
        终态——未闭合任务的运行参数必须可恢复，否则 kick 失效）；
        keep<=0=全部回收（明确语义）。归档上限 _ARCHIVE_MAX 条（FIFO）。
        返回回收数。"""
        removed = 0
        with self._lock:
            finished = [k for k, v in self._runs.items()
                        if not v["thread"].is_alive()]
            drop = (finished if keep <= 0
                    else (finished[:-keep] if len(finished) > keep else []))
            for k in drop:
                run = self._runs.pop(k, None)
                if run is None:
                    continue
                sched = run.get("scheduler")
                # E3/P1：回收与归档回写必须严格携带本运行句柄记录的原运行代际（my_gen），
                # 绝不能重读库中已被后续新代推进的 generation，防止旧清理跨代覆盖新锁持有者
                my_gen = run.get("generation")
                snap_f = dict(run.get("final_snapshot") or {})
                fin_status = snap_f.get("mission_status")
                p_arch = dict(run.get("params") or {})
                if my_gen is not None and "_generation" not in p_arch:
                    p_arch["_generation"] = my_gen
                self._archive[k] = {
                    "params": p_arch,
                    "generation": my_gen,
                    "final_snapshot": snap_f,
                    "archived_at": _now()}
                # N5（Codex 四轮）：**未终态**任务的恢复配置持久化到任务
                # 库 run_control.params_json——内存归档有界可淘汰，恢复
                # 能力不随之丢失（kick 从库回退）；终态任务无需恢复
                if fin_status not in ("done", "cancelled"):
                    try:
                        _persist_params(sched.repo, p_arch, generation=my_gen, expected_generation=my_gen)
                    except Exception:
                        pass
                while len(self._archive) > self._ARCHIVE_MAX:
                    self._archive.pop(next(iter(self._archive)), None)
                try:
                    pool = getattr(sched, "_pool", None)
                    if pool is not None:
                        pool.shutdown(wait=False)
                except Exception:
                    pass
                try:
                    sched.repo.close()
                except Exception:
                    pass
                fl = run.get("file_lock")
                if fl is not None:
                    fl.release()
                removed += 1
        return removed

    # ---- 生命周期 ----

    def start(self, mid: str, root: str = "", max_concurrency: int = 2,
              max_retries: int = 2, max_calls: int | None = None,
              max_cost_usd: float | None = None, real: bool = False,
              node_cap: int | None = None, project: bool = True,
              timeout_s: float = 3600,
              backend_factory=None,
              auto_split: bool = False,
              max_splits_per_mission: int = 5,
              planner=None,
              expected_generation: int | None = None,
              backend_name: str | None = None,
              backend_options: dict | None = None,
              provider_pause_ack: bool = False) -> dict:
        from .scheduler import AutogrowConfig
        from .backends import normalize_backend, create_backend
        if backend_factory is not None and (backend_name is not None or backend_options):
            raise ValueError("backend_factory 与 backend_name/backend_options 不可同时指定")
        # 任务书 2026-11-06：任务级拆分额度显式接线（A0 默认 5 不动）。
        # bool 是 int 子类，显式排除；范围 1..50 覆盖最大合法任务图。
        if isinstance(max_splits_per_mission, bool) or \
                not isinstance(max_splits_per_mission, int) or \
                not (1 <= max_splits_per_mission <= 50):
            raise ValueError(
                f"max_splits_per_mission 必须为 1..50 的整数，收到："
                f"{max_splits_per_mission!r}")
        selected, options = normalize_backend(backend_name, backend_options)
        params = dict(mid=mid, root=root, max_concurrency=max_concurrency,
                      max_retries=max_retries, max_calls=max_calls,
                      max_cost_usd=max_cost_usd, real=real, node_cap=node_cap,
                      project=project, timeout_s=timeout_s,
                      backend_factory=backend_factory,
                      auto_split=auto_split,
                      max_splits_per_mission=max_splits_per_mission,
                      planner=planner)
        if backend_factory is None:
            params.update(backend_name=selected, backend_options=options)
        # 生命周期有界回收（每次准入顺手清一次——完成条目不再无限累积）
        try:
            self.cleanup_finished()
        except Exception:
            pass
        # R2：检查与登记在同一互斥区——绝不出现两个有效调度器
        with self._lock:
            run = self._runs.get(mid)
            if run and run["thread"].is_alive():
                return {"ok": False,
                        "error": f"任务 {mid} 已有运行中的调度器（一个任务仅一个）"}
            db = self._locate_db(mid)
            if db is None:
                return {"ok": False, "error": f"任务库不存在："
                                              f"{self.home / mid / 'mission.db'}"}
            file_lock, lock_err, lock_holder = _RunFileLock.acquire(db, mid)
            if file_lock is None:
                return {
                    "ok": False,
                    "error": lock_err,
                    "lock_file": str(Path(db).resolve().parent / ".run.lock"),
                    "lock_holder": lock_holder,
                }
            started_thread = False
            repo = None
            try:
                repo = Repository(db)
                if expected_generation is not None:
                    row_g = repo._conn.execute(
                        "SELECT generation FROM run_control WHERE id=1"
                    ).fetchone()
                    cur_g = row_g["generation"] if row_g else 0
                    if cur_g != expected_generation:
                        return {"ok": False,
                                "error": f"任务代际冲突（预期代际 {expected_generation} != 库代际 {cur_g}）：拒绝启动"}
                from dag_agent.assessment import default_checker3
                graph = Graph(repo, checker3=default_checker3)
                # D1（2026-11-08）：供应商暂停持久检查——额度/余额/认证类
                # 暂停后重启/换进程 start 不得静默重置限制（防重试风暴）。
                # 恢复必须显式 provider_pause_ack=True（人工确认）。
                pp = None
                try:
                    row_pp = repo._conn.execute(
                        "SELECT params_json FROM run_control WHERE id=1"
                    ).fetchone()
                    pp = (json.loads(row_pp["params_json"] or "{}")
                          .get("provider_pause")) if row_pp else None
                except Exception:
                    pp = None
                pause_ack_cleaned = False
                if pp:
                    if not provider_pause_ack:
                        return {"ok": False,
                                "error": (f"任务存在供应商暂停"
                                          f"（{pp.get('error_class')}："
                                          f"{pp.get('reason', '')[:120]}）——"
                                          "确认处置完成后以 provider_pause_ack"
                                          "=True 重新启动"),
                                "provider_pause": pp}
                    # R5/A1：ack 仅标记待清除，不在预检前提前改写数据库；
                    # 必须在后端可用性预检通过后，随启动配置持久化同事务原子清除，
                    # 确保预检失败或异常中断时原 provider_pause 完整保留。
                    pause_ack_cleaned = True
                # Validate the selected real adapter before recovery mutates attempts.
                prepared_backend = None
                if backend_factory is None and real:
                    prepared_backend = create_backend(selected, options)
                    # 2026-11-08：全部真实后端统一可用性预检（CLI 在位+
                    # 凭据就绪）——错误配置在启动时明确拒绝，不在派发后
                    # 逐 attempt 烧失败
                    avail, why = prepared_backend.available()
                    if not avail:
                        repo.close()
                        return {"ok": False, "error": why}
                # 单服务重启恢复：一切 running 视孤儿收割（旧进程不获新提交权）
                try:
                    mission = repo.get_mission(mid)
                except KeyError:
                    return {"ok": False, "error": f"任务不存在：{mid}"}
                rec = recover_mission(repo, graph, mid)
                root_id = root or self._root_of(repo, mid)
                if mission.status == "done":
                    repo.close()
                    return {"ok": True, "status": "done",
                            "detail": "任务已闭合，无须运行", "root": root_id,
                            "recovered": rec}
                if backend_factory is None:
                    if real:
                        backend = prepared_backend
                    else:
                        backend = _NullBackend()  # 零模型占位（测试/空跑）
                else:
                    backend = backend_factory()
                if real and backend is not None:
                    params["effective_backend"] = {
                        "backend_name": getattr(backend, "name", selected),
                        "model": getattr(backend, "model", None),
                        "provider": getattr(backend, "provider", None),
                        "cli": str(getattr(backend, "cli", "") or ""),
                    }
                sched = Scheduler(repo, graph, backend,
                                  max_concurrency=max_concurrency,
                                  budget=Budget(max_calls=max_calls,
                                                max_cost_usd=max_cost_usd),
                                  retry_policy=RetryPolicy(max_retries=max_retries),
                                  node_cap=node_cap,
                                  auto_split=AutogrowConfig(
                                      enabled=bool(auto_split),
                                      max_splits_per_mission=max_splits_per_mission),
                                  planner=planner)
                # R5：新进程恢复供应商保护状态（冷却截止/连续次数/暂停）——
                # 从 run_control.params_json 读取（pp 读取点已取 raw，
                # 这里用完整 params 还原全部字段）
                try:
                    _saved_params = {}
                    if row_pp is not None and row_pp["params_json"]:
                        _saved_params = json.loads(row_pp["params_json"])
                    if pp is None or pause_ack_cleaned:
                        # 无暂停记录 / ack 已清——只恢复冷却与连击，
                        # 不复活已确认清除的 pause
                        _saved_params.pop("provider_pause", None)
                    sched.restore_provider_state(_saved_params)
                except Exception:
                    pass
                th = threading.Thread(target=self._loop,
                                      args=(mid, sched, repo, root_id, project,
                                            timeout_s),
                                      daemon=True, name=f"dag-run-{mid}")
                # C2（Codex 六轮）+ A1（第三轮）：代际+1、启动配置与持久运行态
                # 合并（保留 wall_deadline_utc/provider_cooldown/provider_pause，
                # 仅在 pause_ack_cleaned 时原子移除 provider_pause）在同一事务内完成。
                try:
                    _gen0 = _persist_params(
                        sched.repo, params, sched=sched,
                        expected_generation=expected_generation,
                        clear_keys=("provider_pause",) if pause_ack_cleaned else ())
                except Exception as e:
                    repo.close()
                    return {"ok": False,
                            "error": f"启动配置持久化失败（已回滚，不启动运行）：{e}"}
                params["_generation"] = _gen0
                self._runs[mid] = {"thread": th, "scheduler": sched,
                                   "status": "running", "params": params,
                                   "generation": _gen0,
                                   "file_lock": file_lock}
                th.start()
                started_thread = True
                return {"ok": True, "status": "running", "root": root_id,
                        "recovered": rec,
                        "reaped_attempts": rec.get("reaped_attempts", []),
                        "lock_file": str(file_lock.lock_path)}
            except BaseException:
                if repo is not None:
                    try:
                        repo.close()
                    except Exception:
                        pass
                raise
            finally:
                if not started_thread:
                    if repo is not None:
                        try:
                            repo.close()
                        except Exception:
                            pass
                    file_lock.release()

    def _locate_db(self, mid: str) -> Path | None:
        db = self.home / mid / "mission.db"
        if db.is_file():
            return db
        # 兼容非常规布局：在 home 下按 mid 匹配唯一 mission.db
        # 必须使用只读连接查询，严禁在获得互斥锁之前触发 Repository 初始化与自动迁移
        for cand in self.home.rglob("mission.db"):
            try:
                uri = f"{cand.resolve().as_uri()}?mode=ro"
                conn = sqlite3.connect(uri, uri=True, timeout=1.0)
                try:
                    row = conn.execute(
                        "SELECT id FROM missions WHERE id=? LIMIT 1", (mid,)
                    ).fetchone()
                    if row is not None:
                        return cand
                finally:
                    conn.close()
            except Exception:
                continue
        return None

    def kick(self, mid: str) -> dict:
        """R2：resume/unblock 后的续跑通道——任务 active、无在途调度器、
        仍有可运行节点 → 按留存参数自动重启调度（修全暂停恢复不续跑）。
        F4（Codex 三轮）：被有界回收的条目从 _archive 恢复轻量启动配置
        （线程终止≠mission 终态——未闭合任务仍可续跑）。"""
        with self._lock:
            run = self._runs.get(mid)
            if run and run["thread"].is_alive():
                return {"ok": True, "kicked": False, "reason": "already_running"}
            if run:
                params = dict(run.get("params") or {})
                if "_generation" not in params and run.get("generation") is not None:
                    params["_generation"] = run.get("generation")
            else:
                arch = self._archive.get(mid)
                params = dict(arch["params"]) if arch else None
                if params and "_generation" not in params and arch.get("generation") is not None:
                    params["_generation"] = arch.get("generation")
        if not params:
            # N5（Codex 四轮）：内存归档淘汰后从**任务库**回退恢复配置
            # （cleanup_finished 对未终态任务持久化 run_control.params_json）
            try:
                db0 = self._locate_db(mid)
                if db0 is not None:
                    repo0 = Repository(db0)
                    try:
                        raw = repo0._conn.execute(
                            "SELECT generation, params_json FROM run_control WHERE id=1"
                        ).fetchone()
                        if raw and raw["params_json"]:
                            cur_gen = raw["generation"]
                            parsed = json.loads(raw["params_json"]) or None
                            if parsed:
                                p_gen = parsed.get("_generation")
                                if p_gen is None or p_gen != cur_gen:
                                    return {"ok": False,
                                            "error": f"任务配置错代（配置代际 {p_gen} != 库代际 {cur_gen}）：拒绝恢复"}
                                params = parsed
                    finally:
                        repo0.close()
            except Exception:
                params = None
        else:
            # C2（Codex 六轮）：从内存/归档恢复时也必须核对库内代际
            try:
                db0 = self._locate_db(mid)
                if db0 is not None:
                    repo0 = Repository(db0)
                    try:
                        raw = repo0._conn.execute(
                            "SELECT generation FROM run_control WHERE id=1"
                        ).fetchone()
                        if raw:
                            cur_gen = raw["generation"]
                            p_gen = params.get("_generation")
                            if p_gen is None or p_gen != cur_gen:
                                return {"ok": False,
                                        "error": f"任务配置错代（配置代际 {p_gen} != 库代际 {cur_gen}）：拒绝恢复"}
                    finally:
                        repo0.close()
            except Exception:
                pass
        if not params:
            return {"ok": False, "error": f"任务 {mid} 无历史运行参数"}
        p_gen = params.get("_generation")
        if p_gen is None:
            return {"ok": False, "error": f"任务 {mid} 历史运行参数缺少代际标识：拒绝恢复"}
        # B3（Codex 五轮）：不可重建组件显式失败——原运行用了
        # backend_factory（参数带组件标识）时，kick 不得静默换执行
        # 语义（注入 stub 换 null 的教训）
        _comp = next((k for k in params
                      if k.startswith("_") and k.endswith("_component")), None)
        if _comp is not None and not params.get("backend_factory"):
            return {"ok": False,
                    "error": f"原运行使用组件 {params[_comp]}"
                             f"（{_comp[:-len('_component')][1:]}），"
                            "无法自动重建——请以相同组件重新 start"
                            "（显式失败，不静默换执行语义）"}
        db = self._locate_db(mid)
        if db is None:
            return {"ok": False, "error": "任务库不存在"}
        repo = Repository(db)
        try:
            m = repo.get_mission(mid)
            if m.status != "active":
                return {"ok": True, "kicked": False,
                        "reason": f"任务{m.status}"}
            if not any(n.status in ("pending", "failed", "waiting_children",
                                    "waiting_split")
                       for n in repo.list_nodes(mid)):
                return {"ok": True, "kicked": False, "reason": "无可运行节点"}
        finally:
            try:
                repo.close()
            except Exception:
                pass
        kw = {k: v for k, v in params.items()
              if not k.startswith("_") and k not in _PERSISTED_RUNTIME_KEYS}
        r = self.start(**kw, expected_generation=p_gen)   # params 含 mid（关键字传入），严格锁死代际防竞态
        r["kicked"] = bool(r.get("ok"))
        return r

    def run_blocking(self, mid: str, timeout: float = 1800, **kw) -> dict:
        """R2：统一入口的阻塞式运行（旧 dag3_run 收编于此，不产生第二调度器）。"""
        r = self.start(mid, **kw)
        if not r.get("ok") or r.get("status") == "done":
            return r
        with self._lock:
            run = self._runs.get(mid)
        if run:
            run["thread"].join(timeout=timeout)
            st = self.status(mid)
            return {"ok": True, "blocking": True, **st}
        return r

    def _loop(self, mid: str, sched: Scheduler, repo: Repository,
              root_id: str, project: bool, timeout_s: float):
        def _safe_project(g):
            try:
                render_mission_versioned(repo, g, mid)
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                payload = {
                    "error_type": type(e).__name__,
                    "message": str(e),
                    "errno": getattr(e, "errno", None),
                    "winerror": getattr(e, "winerror", None),
                    "traceback": tb,
                    "target": "board",
                }
                try:
                    repo.record_event(mid, "projection_error", **payload)
                except Exception:
                    pass
                if sched is not None and hasattr(sched, "counters"):
                    sched.counters["project_errors"] = sched.counters.get("project_errors", 0) + 1
                sys.stderr.write(f"[projection_error] mission={mid} error={type(e).__name__}: {e}\n")
                sys.stderr.flush()

        on_tick = None
        if project:
            graph = sched.graph
            def on_tick():
                _safe_project(graph)
        summary = None
        final_snapshot = {}
        try:
            summary = sched.run_until_done(mid, root_id, timeout=timeout_s,
                                           poll=0.2, on_tick=on_tick)
        finally:
            if project:
                _safe_project(sched.graph)
            # R2+：关仓储前抓终局快照——完成句柄的 status 不再返回 unknown
            try:
                final_snapshot = {
                    "mission_status": repo.get_mission(mid).status,
                    "budget": {"calls": sched.budget.calls_used,
                               "cost_usd": round(sched.budget.cost_used, 4)},
                    "halt_reason": sched.halt_reason,
                    "counters": dict(getattr(sched, "counters", {}) or {}),
                    "summary": None,
                }
            except Exception:
                final_snapshot = {
                    "mission_status": "unknown",
                    "counters": dict(getattr(sched, "counters", {}) or {}),
                }
            with self._lock:
                run = self._runs.get(mid)
                fl = None
                if run:
                    run["status"] = "done"
                    run["summary"] = summary or final_snapshot
                    run["final_snapshot"] = final_snapshot
                    fl = run.get("file_lock")
            try:
                repo.close()
            finally:
                if fl is not None:
                    fl.release()

    @staticmethod
    def _root_of(repo: Repository, mid: str) -> str:
        row = repo._conn.execute(
            "SELECT id FROM nodes WHERE mission_id=? AND parent_id IS NULL",
            (mid,)).fetchone()
        return row["id"] if row else ""

    # ---- 查询与干预 ----

    def status(self, mid: str) -> dict:
        with self._lock:
            run = self._runs.get(mid)
            if not run:
                arch = self._archive.get(mid)
                if arch:
                    # F4：已归档条目如实呈现（未运行+终局快照）
                    return {"ok": True, "running": False, "archived": True,
                            "phase": "archived",
                            **(arch.get("final_snapshot") or {})}
                return {"ok": True, "running": False}
            alive = run["thread"].is_alive()
            sched: Scheduler = run["scheduler"]
            inflight = 0
            with sched._inflight_lock:
                inflight = len(sched._futures)
            if not alive and run.get("final_snapshot"):
                snap = run["final_snapshot"]   # R2+：完成态用快照，不碰已关仓储
                return {"ok": True, "running": False, "phase": run["status"],
                        "inflight": inflight, **snap}
            return {"ok": True, "running": alive,
                    "phase": run["status"], "inflight": inflight,
                    "halt_reason": sched.halt_reason,
                    "counters": dict(getattr(sched, "counters", {}) or {}),
                    "budget": {"calls": sched.budget.calls_used,
                               "cost_usd": round(sched.budget.cost_used, 4)},
                    "mission_status": self._mission_status(sched.repo, mid)}

    @staticmethod
    def _mission_status(repo: Repository, mid: str) -> str:
        try:
            return repo.get_mission(mid).status
        except Exception:
            return "unknown"

    def stop(self, mid: str, mode: str = "drain") -> dict:
        with self._lock:
            run = self._runs.get(mid)
        if run is None:
            # F5（Codex 三轮）：已回收/未知句柄停止幂等（无残留可停，
            # 不抛错；残留 running 由退出复查兜底的历史轮如实保留）
            return {"ok": True, "stopped": True, "mode": mode,
                    "cancelled_attempts": [], "idempotent": True}
        sched: Scheduler = run["scheduler"]
        cancelled = []
        alive = run["thread"].is_alive()
        # N6（Codex 四轮）：捕获**本次停止目标**的运行代际——退出复查
        # 只许清理该代际的残留；复查前若有新 start（generation 已变），
        # 旧停止不得取消新代尝试
        target_gen = run.get("generation")
        if alive:
            # F5：**一次**停止写写足权限（kill=drain+hard_stop 同事务）——
            # 两次写之间的调度线程退出+关库竞态不再存在；即便线程恰在
            # 写前退出（连接已关），旗标此前已落库、无残留可停，吞掉
            # 连接错误如实继续
            try:
                sched.request_stop(mode)  # 旗标落库（与 apply 事务串行化）+ 唤醒
            except Exception:
                pass
            if mode == "kill":
                # Codex R5：kill 必须接通 1B 进程级取消——经
                # Scheduler.cancel_attempt（停进程树+撤销提交权+事件留痕+
                # 节点回 pending）
                try:
                    with sched.repo.tx() as cur:
                        running = [r["id"] for r in cur.execute(
                            "SELECT id FROM attempts WHERE mission_id=?"
                            " AND status='running'", (mid,)).fetchall()]
                    for aid in running:
                        try:
                            sched.cancel_attempt(aid, reason="RunManager.stop(kill)")
                            cancelled.append(aid)
                        except Exception:
                            pass  # 单个取消失败不阻断其余；超时兜底仍在
                except Exception:
                    pass  # 调度线程恰已退出关库：leftover 复查兜底
        run["thread"].join(timeout=60)
        # Codex R5：退出确认后复查——快照与认领交界的残留 attempt 一律
        # 取消（kill 语义：公开停止返回后库内不允许 running 残留）。
        # 注意：调度线程退出时已关闭其 repo（daemon._loop finally），
        # 复查必须用 daemon 自建连接；join 成功=pool 已 wait 收割完毕=
        # 无活子进程，此处只做账本收尾（与 cancel_attempt 同语义：
        # attempt→cancelled + 事件 + 节点回 pending），无需取消事件句柄。
        if mode == "kill":
            stopped_alive = run["thread"].is_alive()
            if not stopped_alive:
                from dag_agent.repository import Repository as _Repo
                repo2 = _Repo(self._locate_db(mid))
                try:
                    with repo2.tx() as cur:
                        # N6：同事务内重核代际——新代际已启动则跳过清理
                        # （旧停止的复查权止于目标代际）
                        rc2 = cur.execute(
                            "SELECT generation FROM run_control WHERE id=1"
                        ).fetchone()
                        cur_gen = rc2["generation"] if rc2 else None
                        if (target_gen is None
                                or cur_gen != target_gen):
                            # N6：目标代际未知或已换代 → 保守跳过清理
                            # （不误伤新代；残留由下次 start 的
                            # recover_mission 孤儿收割兜底）
                            leftover = []
                        else:
                            leftover = [r["id"] for r in cur.execute(
                                "SELECT id FROM attempts WHERE mission_id=?"
                                " AND status='running'", (mid,)).fetchall()]
                        for aid in leftover:
                            cur.execute(
                                "UPDATE attempts SET status='cancelled',"
                                " category='cancel', ended_at=? WHERE id=?",
                                (_now(), aid))
                            nrow = cur.execute(
                                "SELECT node_id FROM attempts WHERE id=?",
                                (aid,)).fetchone()
                            if nrow:
                                cur.execute(
                                    "UPDATE nodes SET status='pending',"
                                    " updated_at=? WHERE id=? AND"
                                    " status='in_work'",
                                    (_now(), nrow["node_id"]))
                            repo2.append_event(
                                cur, mid, "attempt_cancelled", None,
                                attempt=aid,
                                reason="RunManager.stop(kill):退出复查")
                            cancelled.append(aid)
                finally:
                    repo2.close()
        stopped_ok = not run["thread"].is_alive()
        if stopped_ok:
            fl = run.get("file_lock")
            if fl is not None:
                fl.release()
        return {"ok": True, "stopped": stopped_ok,
                "mode": mode, "cancelled_attempts": cancelled}


class _NullBackend:
    """零模型占位后端：真实执行请 real=true（claude CLI×MiniMax）。"""
    name = "null"

    def run(self, statement, workdir, check_cmd, title=""):
        return {"ok": False, "summary": "NullBackend：未接真实执行器"
                                        "（dag3_run_start 请 real=true）",
                "session_id": "", "resources": {"cost_usd": 0.0}}


def _clean_params_for_persist(params: dict) -> dict:
    clean: dict = {}
    for k, v in (params or {}).items():
        try:
            json.dumps(v)
            clean[k] = v
        except (TypeError, ValueError):
            # B3（Codex 五轮）：不可序列化组件（如 backend_factory）
            # 记录**组件标识**——恢复时找不到同名组件必须显式失败，
            # 不得静默换执行语义（注入 stub 被换成 null 的教训）
            name = None
            try:
                fn = getattr(v, "__name__", None) or type(v).__name__
                name = str(fn)
            except Exception:
                name = "?"
            clean[f"_{k}_component"] = name
    return clean


_PERSISTED_RUNTIME_KEYS = (
    "wall_deadline_utc",
    "wall_total_s",
    "provider_cooldown",
    "provider_pause",
    "effective_backend",
)


def _persist_params(repo, params: dict, sched=None, generation: int | None = None,
                    expected_generation: int | None = None,
                    *, clear_keys: tuple[str, ...] = ()) -> int:
    """N5/C2（Codex 六轮）+ A1（第三轮）：轻量恢复配置写入任务库 run_control.params_json。
    同一事务内完成代际更新（若传入 sched 则 generation+1 且 clear_stop_flags）、
    持久运行状态合并（保留已有 wall_deadline_utc/wall_total_s/provider_cooldown/
    provider_pause，仅在 clear_keys 显式指定时原子移除）与配置写入。
    写失败立即抛出异常，不吞异常以确保事务完整与预算保护。
    R1（第七轮修复）：支持 expected_generation 校验，防旧 kick 恢复时竞态覆盖新代际配置。"""
    clean = _clean_params_for_persist(params)
    ts = _now()

    def _do_write(target_repo):
        with target_repo.tx() as cur:
            row = cur.execute(
                "SELECT generation, params_json FROM run_control WHERE id=1"
            ).fetchone()
            cur_gen = row["generation"] if row else 0
            if expected_generation is not None and cur_gen != expected_generation:
                raise ValueError(
                    f"代际冲突（预期代际 {expected_generation} != 库代际 {cur_gen}）：拒绝覆盖新配置/新代际"
                )
            existing = {}
            if row and row["params_json"]:
                try:
                    existing = json.loads(row["params_json"]) or {}
                except Exception:
                    existing = {}
            # 权威运行态状态合并与一致性控制（E2/P1 修复）
            # 1. provider_cooldown：动态限流权威合并，取更严的冷却截止时间与更高连续次数
            if "provider_cooldown" in clear_keys:
                clean.pop("provider_cooldown", None)
                params.pop("provider_cooldown", None)
            else:
                ex_cd = existing.get("provider_cooldown")
                in_cd = clean.get("provider_cooldown")
                if ex_cd and in_cd:
                    merged_cd = dict(ex_cd)
                    merged_cd["rate_streak"] = max(ex_cd.get("rate_streak", 0), in_cd.get("rate_streak", 0))
                    ex_until = str(ex_cd.get("until_utc") or "")
                    in_until = str(in_cd.get("until_utc") or "")
                    merged_cd["until_utc"] = max(ex_until, in_until)
                    clean["provider_cooldown"] = merged_cd
                    params["provider_cooldown"] = merged_cd
                elif ex_cd:
                    clean["provider_cooldown"] = ex_cd
                    params["provider_cooldown"] = ex_cd

            # 2. provider_pause：额度/余额/认证暂停权威保持（仅 clear_keys 显式确认清除）
            if "provider_pause" in clear_keys:
                clean.pop("provider_pause", None)
                params.pop("provider_pause", None)
            else:
                if existing.get("provider_pause"):
                    clean["provider_pause"] = existing["provider_pause"]
                    params["provider_pause"] = existing["provider_pause"]
                elif sched is not None and getattr(sched, "provider_pause", None):
                    clean["provider_pause"] = sched.provider_pause
                    params["provider_pause"] = sched.provider_pause

            # 3. wall_deadline_utc / wall_total_s：不可变截止时间绝对防漂移
            if "wall_deadline_utc" in clear_keys:
                clean.pop("wall_deadline_utc", None)
                params.pop("wall_deadline_utc", None)
            elif existing.get("wall_deadline_utc"):
                clean["wall_deadline_utc"] = existing["wall_deadline_utc"]
                params["wall_deadline_utc"] = existing["wall_deadline_utc"]
                if existing.get("wall_total_s") is not None:
                    clean["wall_total_s"] = existing["wall_total_s"]
                    params["wall_total_s"] = existing["wall_total_s"]

            # 4. effective_backend 继承
            if "effective_backend" in clear_keys:
                clean.pop("effective_backend", None)
                params.pop("effective_backend", None)
            elif clean.get("effective_backend") is None and existing.get("effective_backend") is not None:
                clean["effective_backend"] = existing["effective_backend"]
                params["effective_backend"] = existing["effective_backend"]
            new_gen = generation if generation is not None else (cur_gen + (1 if sched is not None else 0))
            clean["_generation"] = new_gen
            params["_generation"] = new_gen
            payload = json.dumps(clean, ensure_ascii=False)
            cur.execute(
                "INSERT INTO run_control(id,stop_requested,hard_stop,"
                "generation,params_json,updated_at)"
                " VALUES(1,0,0,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET"
                " stop_requested=0, hard_stop=0,"
                " generation=?, params_json=?,"
                " updated_at=?",
                (new_gen, payload, ts, new_gen, payload, ts))
            return new_gen

    try:
        new_g = _do_write(repo)
    except Exception as e:
        if getattr(repo, "_closed", False):
            from .repository import Repository as _R
            r2 = _R(Path(repo.db_path))
            try:
                new_g = _do_write(r2)
            finally:
                r2.close()
        else:
            raise e

    if sched is not None:
        sched.stop_requested.clear()
        sched.hard_stop.clear()
    return new_g
