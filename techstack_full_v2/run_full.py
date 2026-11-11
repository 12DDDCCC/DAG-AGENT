# -*- coding: utf-8 -*-
"""50节点选型研究驱动：默认零模型；real 模式必须显式（claude-cli × MiniMax）。

用法：
  python -m techstack_full_v2.run_full --mode zero-model --output <dir>
  python -m techstack_full_v2.run_full --mode real --output <dir>
      [--backend-options env_file.json]   # {"env_file": "D:/AI/DAG-agent/.env.local"}
运行参数（任务书 §7 固定）：max_concurrency=3、max_splits_per_mission=12、
max_depth=5、node_cap=51、max_retries=1、max_calls=128、CLI 超时 600s、
墙钟 6h（--timeout 可调小用于测试）。
"""
from __future__ import annotations
import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dag_agent.acceptance import compute_check_digest, preflight
from dag_agent.assessment import default_checker3
from dag_agent.autogrow import parse_proposal
from dag_agent.daemon import RunManager
from dag_agent.graph import Graph
from dag_agent.repository import Repository
from techstack_full_v2.checks import (IMPORTS, category_of,
                                      root_expected_deliverables, sha)
from techstack_full_v2.structure import (EXPECTED_NODES, EXPECTED_SPLITS,
                                         CROSS_STUDIES, DOMAINS, DOMAIN_TECHS,
                                         TECHS, ASPECTS, domain_artifacts,
                                         tech_artifacts, cross_artifacts,
                                         domain_compare_artifacts,
                                         cross_study_artifacts)
from techstack_full_v2.templates import brief_research_domain, command, register, restore

PKG = Path(__file__).resolve().parent

ALL_STAGES = (['import'] + [f'{t}-{a}' for t in TECHS for a in ASPECTS]
              + [f'{t}-integration' for t in TECHS]
              + [f'{d}-compare' for d in DOMAINS]
              + [f'{d}-integration' for d in DOMAINS]
              + list(CROSS_STUDIES) + ['cross-integration', 'research-integration', 'root'])


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n',
                          encoding='utf-8')


def research_integration_brief(cmd):
    return (f'【输入】inputs/<来源节点>/ 中的冻结输入与全部上游交付。\n'
            f'【交付清单】research-integration.md, research-integration.json\n'
            f'【接口签名】UTF-8 Markdown 与 JSON，遵守绑定的 deliverable-schema.md；只写当前目录。\n'
            f'【样例】整合 upstream 为 {{"文件名":"SHA256"}}；证据引用 [@证据ID]。\n'
            f'【自测入口】{cmd}\n'
            f'【角色】总研究整合会话：inputs 下已存在全部子交付快照，原样转交全部上游交付'
            f'到当前目录（不得改写），另写 research-integration.md/json，不再提案。'
            f'整合三领域与跨领域全部已验收交付，保持证据可追溯。')


def prepare(home, mode):
    frozen = home / 'frozen'
    inputs = frozen / 'inputs'
    inputs.mkdir(parents=True, exist_ok=True)
    for name in IMPORTS[:-1]:
        source = PKG / 'inputs' / name
        shutil.copyfile(source, inputs / name)
    save(inputs / 'input-provenance.json', {
        'origin': 'host_import', 'model_generated': False, 'mode': mode,
        'files': {name: sha(inputs / name) for name in IMPORTS[:-1]}})
    checker = frozen / 'checks.py'
    shutil.copyfile(PKG / 'checks.py', checker)
    shutil.copyfile(PKG / 'structure.py', frozen / 'structure.py')
    shutil.copyfile(PKG / 'templates.py', frozen / 'templates.py')
    manifest = frozen / 'manifest.json'
    save(manifest, {'schema_version': 1, 'mode': mode,
                    'imports': {name: sha(inputs / name) for name in IMPORTS},
                    'templates_sha256': sha(PKG / 'templates.py'),
                    # 2026-11-08 可信上游：root 转交文件核对的平台库位置
                    # （manifest 由平台生成，执行者不可写；库是活库，
                    # 走 manifest 而非命令行/exam_files——避免 T6 入口
                    # 覆盖校验把活库锁进考卷冻结摘要造成假漂移）
                    'trusted_db': str((frozen.parent / 'mission.db').resolve())})
    # 用平台权威解析器验证协议示例
    import re
    raw = re.search(r'```json\s*(.*?)```',
                    (inputs / 'proposal-protocol.md').read_text(encoding='utf-8-sig'),
                    re.S).group(1)
    parse_proposal(raw.encode('utf-8'), {}, 'n-review', 'a-review')
    # 全部 50 个 stage 的考卷预检（空目录必须 FAIL=咬得动）
    for stage in ALL_STAGES:
        cmd = command(checker, manifest, stage)
        ok, why, _ = preflight(check_cmd=cmd)
        if not ok:
            raise ValueError(f'{stage} preflight: {why}')
    return checker, manifest, inputs


def build(home, checker, manifest):
    repo = Repository(home / 'mission.db')
    graph = Graph(repo, checker3=default_checker3)
    exams = [str(checker), str(manifest), str(home / 'frozen' / 'structure.py')]
    try:
        # 根考卷（2026-11-08）：可信上游库位置已写入 manifest.json
        # （平台生成，执行者不可写）——命令形态与历史一致，考卷冻结
        # 身份不含活库；干净重放沿用同一 frozen manifest 或 --trusted 导出
        root_cmd = command(checker, manifest, 'root')
        mid, root = graph.create_mission(
            '实时数据分析平台技术选型研究（50节点）',
            f'【输入】inputs/<来源节点>/ 中的冻结输入与全部上游交付（只读）。\n'
            f'【交付清单】final-report.md, source-index.json, delivery-manifest.json, review.json\n'
            f'【接口签名】UTF-8 Markdown 与 JSON，遵守绑定的 deliverable-schema.md；只写当前目录。\n'
            f'【样例】证据引用 [@证据ID]；delivery-manifest 为 {{"文件名":"SHA256"}}。\n'
            f'【自测入口】{root_cmd}\n'
            f'【任务】整合全部已验收研究交付，产出最终选型报告（执行摘要、需求匹配结论、'
            f'成本与风险、实施建议、限制与验证状态）、来源索引与交付清单。',
            root_check=root_cmd,
            root_declared_artifacts=sorted(root_expected_deliverables()) + [
                'final-report.md', 'source-index.json',
                'delivery-manifest.json', 'review.json'] + list(IMPORTS))
        graph.repair_check(mid, root, new_check_cmd=root_cmd, exam_files=exams,
                           reason='运行前由出题方冻结根考卷及输入摘要')
        imp_cmd = command(checker, manifest, 'import')
        imp = graph.add_node(mid, '资料导入（宿主）',
                             '【输入】无（本节点即冻结输入载体）。\n'
                             '【交付清单】' + ', '.join(IMPORTS) + '\n'
                             '【接口签名】UTF-8 JSON 与 Markdown，按导入清单逐字节数复制。\n'
                             '【样例】input-provenance.json 记录各文件 SHA256。\n'
                             f'【自测入口】{imp_cmd}\n'
                             '【任务】平台运行前导入的冻结输入；来源为 host_import，不是模型研究产出。',
                             parent_id=root, check_cmd=imp_cmd,
                             declared_artifacts=IMPORTS, exam_files=tuple(exams))
        res_cmd = command(checker, manifest, 'research-integration')
        research = graph.add_node(
            mid, '总研究',
            '【输入】inputs/<来源节点>/ 中的冻结输入（requirements.json、'
            'proposal-protocol.md、deliverable-schema.md 必读）；inputs 只读。\n'
            '【交付清单】research-integration.md, research-integration.json\n'
            '【接口签名】UTF-8 Markdown 与 JSON，遵守绑定的 deliverable-schema.md；'
            '只写当前目录，不改动 inputs/。\n'
            '【样例】整合 upstream 为 {"文件名":"SHA256"}；证据引用格式 [@证据ID]。\n'
            f'【自测入口】{res_cmd}\n'
            '【角色】总研究节点。inputs 下尚无子交付快照 → 按协议评估拆分：只写 '
            '.dag/proposal.json（template_id=full-domains-v1，template_params={}），'
            'children 恰为 database/message-queue/cache 三个领域节点'
            '（template_id=full-domain-v1，params {"domain":<同名>}）与 cross'
            '（template_id=full-cross-v1，params {}，deps=["database","message-queue","cache"]），'
            '正常结束会话，不生成本节点交付。整合会话（inputs 下已有全部子交付快照时）'
            '原样转交全部上游交付到当前目录（不得改写），另写 '
            'research-integration.md/json，不再提案。',
            parent_id=root, deps=(imp,), check_cmd=res_cmd,
            declared_artifacts=sorted(root_expected_deliverables()),
            exam_files=tuple(exams))
        if graph.frontier(mid) != [imp]:
            raise ValueError(f'导入前 frontier 错误: {graph.frontier(mid)}')
        aid = graph.start_attempt(mid, imp, backend='host-import')
        workdir = Path(repo.get_attempt(aid).workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        for name in IMPORTS:
            shutil.copyfile(home / 'frozen' / 'inputs' / name, workdir / name)
        imported = graph.submit_attempt(mid, imp, aid, artifact_paths=list(IMPORTS))
        if imported.get('verdict') != 'pass':
            raise ValueError(f'导入未通过: {imported}')
        with repo.tx() as cur:
            repo.append_event(cur, mid, 'host_inputs_imported', imp, model_generated=False,
                              files={name: sha(home / 'frozen' / 'inputs' / name)
                                     for name in IMPORTS})
        if graph.frontier(mid) != [research]:
            raise ValueError('导入后应只有总研究节点就绪')
        digest = compute_check_digest(check_cmd=root_cmd, exam_files=tuple(exams))
        return mid, root, research, digest
    finally:
        repo.close()


def summarize(home, mid, root, digest, mode, limits):
    repo = Repository(home / 'mission.db')
    graph = Graph(repo, checker3=default_checker3)
    try:
        nodes = repo.list_nodes(mid)
        proposals = [dict(p) for p in repo.list_split_proposals(mid)]
        events = repo.events_of(mid)
        event_types = [e.type for e in events]
        root_node = repo.get_node(root)
        current_digest = compute_check_digest(check_cmd=root_node.check_cmd,
                                              exam_files=graph._exam_files_of(root_node))
        attempts = [dict(r) for r in repo._conn.execute(
            'SELECT id,node_id,backend,status,category,workdir,input_binding_json,'
            'resources_json FROM attempts')]
        deps = {n.id: [d.depends_on for d in repo.deps_of(n.id)] for n in nodes}
        published = home / 'accepted' / root / f'v{root_node.accepted_artifact_version}'
        applied = [p for p in proposals if p['status'] == 'applied']
        nodes_added = sum(int(p['nodes_added'] or 0) for p in applied)
        selected = [c.get('template_id') for p in applied
                    for c in json.loads(p['raw_json'])['children']]
        # 实际生效配置（从 run_control.params_json 读，不硬编码）——
        # 2026-11-08：CLI 超时等由启动参数决定，摘要必须如实输出
        rc = repo._conn.execute(
            'SELECT params_json FROM run_control WHERE id=1').fetchone()
        params = json.loads(rc['params_json'] or '{}') if rc else {}
        effective = {k: params[k] for k in (
            'backend_name', 'backend_options', 'max_calls', 'timeout_s',
            'max_splits_per_mission', 'max_concurrency', 'max_retries',
            'node_cap', 'real') if k in params}
        # 分类统计（2026-11-08：宿主导入/模型执行/拆分会话分列）
        host_import_count = sum(1 for a in attempts
                                if a['backend'] == 'host-import')
        model_backend = effective.get('backend_name')
        model_attempts = ([a for a in attempts if a['backend'] == model_backend]
                          if model_backend else
                          [a for a in attempts
                           if a['backend'] not in ('host-import',)])
        split_session_ids = {p['source_attempt'] for p in applied
                             if p.get('source_attempt')}
        parents = {n.parent_id for n in nodes}
        acc_by_node = {}
        for a in attempts:
            if a['status'] == 'accepted':
                acc_by_node[a['node_id']] = a
        # 2026-11-08 强断言1：初始三节点必须由首次拆分前的创建事件证明
        # （不再用"单根+总数>=3"弱口径）
        first_split_seq = min((e.id for e in events if e.type == 'split_applied'),
                              default=None)
        imp_id = next((e.node_id for e in events
                       if e.type == 'host_inputs_imported'), None)
        pre_split_created = {e.node_id for e in events
                             if e.type in ('create', 'add_node')
                             and (first_split_seq is None or e.id < first_split_seq)}
        expected_initial = {root} | ({imp_id} if imp_id else set())
        research_id = next((nid for nid in pre_split_created
                            if nid not in expected_initial), None)
        if research_id:
            expected_initial.add(research_id)
        # 2026-11-08 强断言2：绑定逐项核对（来源/版本/快照/摘要/文件）
        # ——不再接受"列表不是 None"
        snaps = {s['id']: dict(s) for s in repo._conn.execute(
            'SELECT id,node_id,artifact_version,manifest_hash,files_json'
            ' FROM snapshots')}
        binding_issues = []
        for n in nodes:
            a = acc_by_node.get(n.id)
            if a is None or n.id == root:
                continue
            for b in json.loads(a['input_binding_json'] or '[]'):
                s = snaps.get(b.get('snapshot_id'))
                if (s is None
                        or s['node_id'] != b.get('source')
                        or s['artifact_version'] != b.get('artifact_version')
                        or s['manifest_hash'] != b.get('manifest_hash')):
                    binding_issues.append(
                        {'node': n.id, 'source': b.get('source'),
                         'why': 'snapshot identity mismatch'})
                    continue
                files_meta = json.loads(s['files_json'] or '[]')
                declared_files = b.get('files') or []
                if not files_meta or not declared_files:
                    binding_issues.append(
                        {'node': n.id, 'source': b.get('source'),
                         'why': 'empty binding files'})
                    continue
                meta_names = {f['rel_path'] for f in files_meta}
                if meta_names != set(declared_files):
                    binding_issues.append(
                        {'node': n.id, 'source': b.get('source'),
                         'why': 'file list mismatch'})
        checks = {
            f'nodes_{EXPECTED_NODES}': len(nodes) == EXPECTED_NODES,
            f'splits_{EXPECTED_SPLITS}': len(applied) == EXPECTED_SPLITS
            and nodes_added == EXPECTED_NODES - 3,
            'mission_done_machine': repo.get_mission(mid).status == 'done'
            and repo.get_mission(mid).closure_basis == 'machine',
            'all_accepted': all(n.status == 'accepted' for n in nodes),
            # 初始三节点：由首次拆分前的创建事件证明（root+导入+总研究）
            'initial_three_from_events': (
                sum(1 for n in nodes if not n.parent_id) == 1
                and len(nodes) >= 3
                and pre_split_created == expected_initial
                and len(expected_initial) == 3),
            'event_chain': all(t in event_types for t in (
                'host_inputs_imported', 'split_proposed', 'split_activated',
                'split_applied')),
            'root_check_unchanged': current_digest == digest,
            'root_files_published': all(
                (published / n).is_file()
                for n in json.loads(root_node.declared_artifacts_json or '[]')),
            'trusted_templates_selected': all(t in (
                'full-domains-v1', 'full-domain-v1', 'full-tech-v1', 'full-cross-v1')
                for t in selected),
            'no_queued_proposals': all(p['status'] == 'applied' for p in proposals),
            'frozen_artifacts_valid': all(
                graph.store.verify_accepted(repo, n.id).get('ok')
                for n in nodes if n.status == 'accepted'),
            # 绑定强校验：无任何身份/清单/空绑定问题
            'child_input_bindings_strict': not binding_issues,
        }
        # 并行证据（2026-11-08 强断言3）：仅成功真实执行叶子，且重叠对
        # 之间无依赖边——任意区间重叠不再充数。零模型夹具无并行语义，
        # 不作断言（real 模式硬性要求）。
        leaf_spans = []
        for a in attempts:
            if a['status'] != 'accepted' or a['backend'] == 'host-import':
                continue
            if a['node_id'] in parents:
                continue
            r = json.loads(a['resources_json'] or '{}')
            s, e_ = r.get('executor_started_at'), r.get('executor_ended_at')
            if s and e_:
                leaf_spans.append((a['node_id'],
                                   datetime.fromisoformat(s),
                                   datetime.fromisoformat(e_)))
        overlap_pairs = [
            [x[0], y[0]] for i, x in enumerate(leaf_spans)
            for y in leaf_spans[i + 1:]
            if x[1] < y[2] and y[1] < x[2]
            and y[0] not in deps.get(x[0], [])
            and x[0] not in deps.get(y[0], [])]
        if mode == 'real':
            checks['parallel_overlap_independent_leaves'] = bool(overlap_pairs)
            # real 模式不得仅凭字符串标签认定真实执行：模型执行 attempt
            # 必须带结构化回执（provider_model 非空 + executor 起止）
            real_evidence = all(
                (json.loads(a['resources_json'] or '{}').get('provider_model')
                 and json.loads(a['resources_json'] or '{}').get('executor_started_at'))
                for a in model_attempts if a['status'] == 'accepted')
            checks['real_execution_structured_receipts'] = (
                bool([a for a in model_attempts if a['status'] == 'accepted'])
                and bool(real_evidence))
        save(home / 'events.json', [asdict(e) for e in events])
        save(home / 'attempts.json', attempts)
        save(home / 'proposals.json', proposals)
        save(home / 'nodes.json', [{'id': n.id, 'title': n.title, 'status': n.status,
                                    'parent': n.parent_id, 'deps': deps[n.id],
                                    'check': (n.check_cmd or '')[-60:]}
                                   for n in nodes])
        return {'checks': checks, 'success': all(checks.values()),
                'mission_status': repo.get_mission(mid).status,
                'node_count': len(nodes),
                'applied_splits': len(applied),
                'template_ids_used': sorted(set(selected)),
                'content_review': 'pending',
                'effective_config': effective,
                'accounting': {
                    'host_import_attempts': host_import_count,
                    'model_execution_attempts': len(model_attempts),
                    'model_execution_accepted': sum(
                        1 for a in model_attempts if a['status'] == 'accepted'),
                    'split_sessions': len(split_session_ids),
                    'assessments_passed': sum(
                        1 for n in nodes if n.status == 'accepted'),
                },
                'binding_issues': binding_issues[:20],
                'overlap_pairs_sample': overlap_pairs[:8],
                'planning_mode': 'trusted_templates+fixture' if mode == 'zero-model'
                else 'trusted_templates+real-executor',
                'root_published': str(published)}
    finally:
        repo.close()


def _fixture_factory():
    """零模型确定性夹具工厂（具名函数——run_control 持久化的组件标识
    可被 resume 识别重建；匿名 lambda 会记成 <lambda> 无法核实）。"""
    from techstack_full_v2.fixture_full import FixtureBackendFull
    return FixtureBackendFull('zero-model')


def run(home, mode='zero-model', timeout=21600, backend_name='claude-cli',
        backend_options=None):
    home = Path(home).resolve()
    if home.exists() and any(home.iterdir()):
        raise ValueError('运行目录必须为空；不会清理或覆盖已有结果——'
                         '续跑请用 --resume（独立入口，核对配置后恢复）')
    home.mkdir(parents=True, exist_ok=True)
    backend_options = dict(backend_options or {})
    report = {'mode': mode, 'success': False,
              'synthetic_fixture': mode == 'zero-model',
              'limits': {'max_calls': 128, 'node_cap': 51,
                         'expected_nodes': EXPECTED_NODES,
                         'expected_splits': EXPECTED_SPLITS,
                         'max_splits_per_mission': 12, 'max_concurrency': 3,
                         'max_retries': 1,
                         # CLI 超时如实反映启动配置（2026-11-08：不再硬编码 600）
                         'cli_timeout_s': backend_options.get('timeout', 600),
                         'wall_clock_s': timeout},
              'content_review': 'pending',
              'started_at': datetime.now(timezone.utc).isoformat()}
    manager = None
    old = None
    mid = root = research = digest = None
    try:
        if mode not in ('zero-model', 'real'):
            raise ValueError('未知运行模式')
        checker, manifest, inputs = prepare(home, mode)
        old = register(checker, manifest)
        if mode == 'real':
            from dag_agent.backends import create_backend
            backend = create_backend(backend_name, backend_options)
            available, reason = backend.available()
            if not available:
                raise ValueError(f'后端预检未通过: {reason}')
        mid, root, research, digest = build(home, checker, manifest)
        report.update(mission_id=mid, root=root, research=research)
        manager = RunManager(home)
        # 2026-11-08（跨进程续跑修复）：real 后端走可序列化
        # backend_name/backend_options（run_control.params_json 持久化，
        # 冷启动可恢复）；仅零模型夹具保留进程内 factory（夹具由本模块
        # 在恢复时重建，见 resume()）。
        if mode == 'real':
            start_kw = dict(backend_name=backend_name,
                            backend_options=backend_options, real=True)
        else:
            start_kw = dict(backend_factory=_fixture_factory, real=False)
        start = manager.start(mid, root=root,
                              auto_split=True, max_splits_per_mission=12,
                              project=True, max_concurrency=3,
                              max_retries=1, max_calls=128, node_cap=51,
                              timeout_s=timeout, **start_kw)
        if not start.get('ok'):
            raise ValueError(f'启动失败: {start}')
        report['run_started_at'] = datetime.now(timezone.utc).isoformat()
        deadline = time.monotonic() + timeout
        last_len = 0
        while True:
            state = manager.status(mid)
            if not state.get('running'):
                break
            if time.monotonic() >= deadline:
                report['timed_out'] = True
                break
            # 简短进展日志（不刷屏：仅在节点数变化时记录）
            try:
                n = len(Repository(home / 'mission.db').list_nodes(mid))
                if n != last_len:
                    print(f'[{datetime.now().strftime("%H:%M:%S")}] nodes={n}',
                          flush=True)
                    last_len = n
            except Exception:
                pass
            time.sleep(2)
        report['last_status'] = state
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        if manager is not None and mid is not None:
            try:
                stopped = manager.stop(mid, mode='kill')
                report['stop'] = stopped
            except Exception as exc:
                report['stop_error'] = f'{type(exc).__name__}: {exc}'
            if report.get('stop', {}).get('stopped'):
                try:
                    report.update(summarize(home, mid, root, digest, mode,
                                            report['limits']))
                except Exception as exc:
                    import traceback
                    report['summary_error'] = \
                        f'{type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}'
                restore(old)
                old = None
                try:
                    manager.cleanup_finished(keep=0)
                except Exception as exc:
                    report['cleanup_error'] = f'{type(exc).__name__}: {exc}'
        elif old is not None:
            restore(old)
            old = None
        if any(k in report for k in ('error', 'stop_error', 'summary_error',
                                     'timed_out')) or \
                not report.get('stop', {}).get('stopped'):
            report['success'] = False
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        save(home / 'real_run_results.json' if mode == 'real'
             else home / 'run_results.json', report)
    return report


def _persist_wall_deadline(repo, mid: str, deadline_utc_iso: str,
                           wall_total_s: float, manager=None) -> None:
    """R4/A1：固化不可变绝对截止时间（执行者/恢复语义可核对的锚）——
    增量写 run_control.params_json 并同步 RunManager 内存 params（防
    cleanup_finished 或后续持久化覆盖）。"""
    with repo.tx() as cur:
        row = cur.execute(
            "SELECT params_json FROM run_control WHERE id=1").fetchone()
        p = json.loads(row["params_json"] or "{}") if row else {}
        p.setdefault('wall_deadline_utc', deadline_utc_iso)
        p.setdefault('wall_total_s', wall_total_s)
        cur.execute(
            "INSERT INTO run_control(id, stop_requested, hard_stop,"
            " generation, params_json, updated_at)"
            " VALUES(1, 0, 0, 0, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET"
            " params_json=excluded.params_json,"
            " updated_at=excluded.updated_at",
            (json.dumps(p, ensure_ascii=False),
             datetime.now(timezone.utc).isoformat(timespec="seconds")))
        saved_dl = p.get('wall_deadline_utc')
        saved_total = p.get('wall_total_s')
    if manager is not None:
        try:
            with manager._lock:
                run_entry = manager._runs.get(mid)
                if run_entry and isinstance(run_entry.get('params'), dict):
                    run_entry['params']['wall_deadline_utc'] = saved_dl
                    run_entry['params']['wall_total_s'] = saved_total
        except Exception:
            pass


def resume(home, backend_name=None, backend_options=None,
           provider_pause_ack: bool = False):
    """跨进程恢复既有 mission（第二轮 R3/R4 强化）。

    语义与核对（任一不符明确拒绝）：
    - done 幂等最前置：已闭合任务直接返回（不因墙钟过期/工作区升级
      产生任何调用）；
    - frozen 考卷/输入与当前源码一致；配置代际一致；
    - R4：不可变绝对截止时间 wall_deadline_utc（UTC ISO，首次恢复由
      created_at+原总时长推导并固化；后续恢复只算 deadline-now，
      不再把剩余时间回写成新总时长）；
    - R4：合法零值保留（max_retries=0 不被 or 默认改写）；
    - R4：backend_options 全字段核对（model/cli_path/env_file/timeout/
      allowed_tools/thinking/provider_config/permission_mode——默认
      不允许任何静默变更），backend_name 未显式提供时沿用已保存值
      （不被默认 claude-cli 覆盖）；
    - R4.10：provider_pause 检查在任务包入口接线（ack 透传 daemon.start）；
    - 单服务边界：重复恢复者由 RunManager.start 的"一任务一调度器"
      互斥拒绝（不声称跨进程文件锁）。
    返回 dict 含内部句柄键（_manager/_old_registry，下划线前缀，写盘
    前由调用方剔除）。
    """
    home = Path(home).resolve()
    db = home / 'mission.db'
    if not db.is_file():
        raise ValueError(f'任务库不存在，无法恢复: {db}')
    repo = Repository(db)
    try:
        missions = repo._conn.execute('SELECT id, status, created_at FROM missions'
                                      ).fetchall()
        if len(missions) != 1:
            raise ValueError(f'任务库含 {len(missions)} 个任务，拒绝恢复')
        mid = missions[0]['id']
        status = missions[0]['status']
        created_at = missions[0]['created_at']
        # R4.9：done 幂等最前置——已完成任务不因墙钟过期或工作区升级
        # 再次执行（零调用）
        if status == 'done':
            return {'ok': True, 'resumed': False, 'mission_id': mid,
                    'status': 'done',
                    'detail': '任务已闭合，幂等返回，不再次调用模型'}
        rc = repo._conn.execute(
            'SELECT generation, params_json FROM run_control WHERE id=1'
        ).fetchone()
        params = json.loads(rc['params_json']) if (rc and rc['params_json']) else None
        if not params:
            raise ValueError('run_control 无持久化运行配置——冷启动恢复不可用'
                             '（旧版本运行或手工建图，无法核对原配置）')
        gen = params.get('_generation')
        if gen is None or (rc and gen != rc['generation']):
            raise ValueError(f'配置代际缺失或与库不一致（{gen}）——拒绝恢复')
        mode = 'real' if params.get('real') else 'zero-model'
        frozen = home / 'frozen'
        for name in ('checks.py', 'structure.py'):
            fp = frozen / name
            cur = PKG / name
            if not fp.is_file() or sha(fp) != sha(cur):
                raise ValueError(
                    f'冻结考卷 {name} 与当前源码不一致——拒绝恢复'
                    '（不默认用当前源码替代旧冻结版本）')
        if (frozen / 'templates.py').is_file() \
                and sha(frozen / 'templates.py') != sha(PKG / 'templates.py'):
            raise ValueError(
                '冻结模板 templates.py 与当前源码不一致——拒绝恢复')
        manifest = frozen / 'manifest.json'
        saved_manifest = json.loads(manifest.read_text(encoding='utf-8'))
        if saved_manifest.get('templates_sha256') \
                and sha(PKG / 'templates.py') != saved_manifest['templates_sha256']:
            raise ValueError(
                '当前 templates.py 与 manifest 记录的模板哈希不一致——拒绝恢复')
        for name, digest in saved_manifest['imports'].items():
            src = PKG / 'inputs' / name
            if src.is_file():
                if sha(src) != digest:
                    raise ValueError(f'冻结输入 {name} 与当前源码不一致'
                                     '——拒绝恢复')
            else:
                frozen_src = frozen / 'inputs' / name
                if not frozen_src.is_file() or sha(frozen_src) != digest:
                    raise ValueError(f'冻结输入 {name} 缺失或与 manifest 不符'
                                     '——拒绝恢复')
        checker, manifest_path = frozen / 'checks.py', frozen / 'manifest.json'
        # R4.1-3：不可变绝对截止时间（UTC）。旧数据迁移=保守：以当前
        # 记录的 timeout_s 作为剩余（不比原计划更宽松）并固化，勘误
        # 说明历史 first-resume 语义。
        now_utc = datetime.now(timezone.utc)
        deadline_iso = params.get('wall_deadline_utc')
        if deadline_iso:
            deadline = datetime.fromisoformat(deadline_iso)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            remaining = (deadline - now_utc).total_seconds()
            wall_total_s = float(params.get('wall_total_s')
                                 or params.get('timeout_s') or 21600)
        else:
            started = datetime.fromisoformat(created_at)
            if started.tzinfo is None:
                started = started.astimezone()   # 库内 naive=本地时间
            wall_cur = float(params.get('timeout_s') or 21600)
            deadline = started + timedelta(seconds=wall_cur)
            remaining = (deadline - now_utc).total_seconds()
            wall_total_s = wall_cur
        if remaining <= 0:
            raise ValueError(
                f'原墙钟已耗尽（deadline={deadline.isoformat()}）'
                '——拒绝恢复（如需继续请显式评估后新建运行）')
        old = register(checker, manifest_path)
        try:
            # 模板注册保持到运行结束（异步调度线程的提案检测/子节点
            # 生成依赖 SPLIT_TEMPLATES）——restore 由 resume_blocking
            # 在调度线程结束后执行；本函数失败路径自行 restore。
            manager = RunManager(home)
            if mode == 'real':
                saved_name = params.get('backend_name')
                saved_opts = params.get('backend_options') or {}
                # R4.8：未显式提供时沿用已保存后端（不被默认覆盖）
                name = backend_name if backend_name else saved_name
                if backend_name and saved_name and backend_name != saved_name:
                    raise ValueError(
                        f'后端与原运行不一致（{backend_name} != {saved_name}）'
                        '——拒绝（换后端=换执行语义，须人工确认）')
                opts = dict(saved_opts)
                if backend_options is not None:
                    # R4.5-7 / A4：全字段核对——None 表示沿用，显式传入（含空字典 {}）
                    # 必须与原配置完全一致，绝不允许显式 {} 清空原配置或静默变更
                    diffs = {k: (saved_opts.get(k), v)
                             for k, v in backend_options.items()
                             if saved_opts.get(k) != v}
                    extra = set(saved_opts) - set(backend_options)
                    if diffs or extra:
                        raise ValueError(
                            f'backend_options 与原运行不一致（全字段核对）'
                            f'——差异={diffs}，缺失={sorted(extra)}；'
                            '恢复不允许静默变更或清空已有执行配置')
                    opts = dict(backend_options)
                if not name:
                    raise ValueError('real 模式恢复缺后端配置——显式提供 '
                                     '--backend-name/--backend-options')
                from dag_agent.backends import create_backend
                backend = create_backend(name, opts)
                saved_eff = params.get('effective_backend')
                if isinstance(saved_eff, dict):
                    for eff_k, attr_k in (('model', 'model'),
                                          ('provider', 'provider'),
                                          ('cli', 'cli')):
                        sv = saved_eff.get(eff_k)
                        cv = getattr(backend, attr_k, None)
                        if attr_k == 'cli' and cv is not None:
                            cv = str(cv)
                        if sv and cv and str(sv) != str(cv):
                            raise ValueError(
                                f'解析后的有效后端身份 {eff_k} 发生漂移'
                                f'（原={sv!r} != 现={cv!r}）——拒绝恢复')
                available, reason = backend.available()
                if not available:
                    raise ValueError(f'后端预检未通过: {reason}')
                start_kw = dict(backend_name=name, backend_options=opts, real=True)
            else:
                comp = params.get('_backend_factory_component')
                if comp is not None and comp != '_fixture_factory':
                    raise ValueError(f'原运行组件 {comp} 无法安全重建——拒绝恢复')
                start_kw = dict(backend_factory=_fixture_factory, real=False)
            # R4.4：合法零值保留（0 不被 or 默认改写）
            def _nz(d, k, default):
                v = d.get(k)
                return default if v is None else v
            max_retries = _nz(params, 'max_retries', 1)
            max_conc = _nz(params, 'max_concurrency', 3)
            max_splits = _nz(params, 'max_splits_per_mission', 12)
            start = manager.start(mid, root=params.get('root') or '',
                                  auto_split=bool(params.get('auto_split')),
                                  max_splits_per_mission=int(max_splits),
                                  project=bool(params.get('project', True)),
                                  max_concurrency=int(max_conc),
                                  max_retries=int(max_retries),
                                  max_calls=params.get('max_calls'),
                                  max_cost_usd=params.get('max_cost_usd'),
                                  node_cap=params.get('node_cap'),
                                  timeout_s=remaining,
                                  expected_generation=gen,
                                  provider_pause_ack=provider_pause_ack,
                                  **start_kw)
            if start.get('ok'):
                # R4.1/A1：固化不可变截止时间并同步 manager 内存 params
                _persist_wall_deadline(repo, mid, deadline.isoformat(),
                                       wall_total_s, manager=manager)
            if not start.get('ok'):
                # R4：start 失败也明确返回失败（句柄/注册在 finally 释放）
                return {'ok': False, 'resumed': False, 'mission_id': mid,
                        'mode': mode, **start,
                        '_old_registry': old, '_manager': manager}
            return {'ok': True, 'resumed': True,
                    'mission_id': mid, 'mode': mode,
                    'wall_remaining_s': round(remaining, 1),
                    'wall_deadline_utc': deadline.isoformat(),
                    'expected_generation': gen,
                    '_old_registry': old, '_manager': manager, **start}
        except BaseException:
            restore(old)
            raise
    finally:
        repo.close()


def _classify_halt(halt_reason: str) -> str:
    """R3：把调度器 halt_reason 归类为可区分的结束原因（不只看 done）。"""
    h = halt_reason or ''
    if '熔断' in h:
        return 'budget_halted'
    if '供应商暂停' in h:
        return 'provider_paused'
    if '死局' in h:
        return 'deadlock'
    if '无进展' in h:
        return 'no_progress'
    if '服务停止' in h or '停止' in h:
        return 'user_stopped'
    if h:
        return 'halted_other'
    return 'unknown'


def _monitor_run(manager, mid: str, wall_remaining_s: float,
                 poll: float = 2.0):
    """R3：统一监控路径（可独立测试）——区分完成/停派/暂停/死局/停止/
    墙钟超时/状态查询失联；超时与失联按约定 stop(kill) 并确认（在途
    费用与回执由既有结算路径保留）。返回 (completed, ended_reason,
    stop_result)。"""
    deadline = time.monotonic() + float(wall_remaining_s)
    completed = False
    ended_reason = 'unknown'
    stop_result = None
    query_errors = 0
    while True:
        try:
            st = manager.status(mid)
            query_errors = 0
        except Exception:
            query_errors += 1
            if query_errors >= 5:
                ended_reason = 'status_query_failed'
                break
            time.sleep(poll)
            continue
        if st.get('mission_status') == 'done':
            completed = True
            ended_reason = 'completed'
            break
        if not st.get('running'):
            ended_reason = _classify_halt(st.get('halt_reason'))
            break
        if time.monotonic() >= deadline:
            ended_reason = 'wall_timeout'
            break
        time.sleep(poll)
    try:
        stop_result = manager.stop(mid, mode='kill' if not completed
                                   else 'drain')
    except Exception as e:
        stop_result = {'stopped': False, 'error': f'{type(e).__name__}: {e}'}
    run = manager._runs.get(mid)
    if run is not None:
        run['thread'].join(timeout=60)
        if run['thread'].is_alive():
            if isinstance(stop_result, dict):
                stop_result = dict(stop_result, stopped=False, thread_alive=True)
            else:
                stop_result = {'stopped': False, 'thread_alive': True}
    return completed, ended_reason, stop_result


def resume_blocking(home, poll=2.0, backend_name=None, backend_options=None,
                    provider_pause_ack: bool = False):
    """恢复并等待终态（独立进程续跑主入口，第二轮 R3 + 第三轮 A3 强化）。

    统一监控路径：持 resume 返回的实际 manager 句柄，区分完成/预算停派/
    供应商暂停/死局/无进展/用户停止/墙钟超时/状态查询失联八类终态；
    超时、失联时按约定 stop(kill) 并确认（保留在途结算与回执）；
    **仅在确认调度器已停止且线程已退出后**才恢复全局模板注册并执行
    cleanup_finished；未停止时保留注册与句柄并明确标记失败。
    """
    info = resume(home, backend_name=backend_name,
                  backend_options=backend_options,
                  provider_pause_ack=provider_pause_ack)
    home = Path(home).resolve()
    db = home / 'mission.db'
    if not info.get('ok') or not info.get('resumed'):
        old = info.pop('_old_registry', None)
        if old:
            restore(old)
        return {k: v for k, v in info.items() if not k.startswith('_')}
    mid = info['mission_id']
    manager: RunManager = info['_manager']
    stop_clean = False
    completed = False
    ended_reason = 'unknown'
    stop_result = None
    try:
        completed, ended_reason, stop_result = _monitor_run(
            manager, mid, info['wall_remaining_s'], poll=poll)
        run_entry = getattr(manager, '_runs', {}).get(mid)
        thread_alive = bool(run_entry is not None
                            and run_entry.get('thread') is not None
                            and run_entry['thread'].is_alive())
        stop_clean = (stop_result is not None
                      and bool(stop_result.get('stopped'))
                      and not thread_alive)
    finally:
        if stop_clean:
            old = info.pop('_old_registry', None)
            if old:
                restore(old)
            try:
                manager.cleanup_finished(keep=0)
            except Exception:
                pass
    out = {'ok': bool(completed and stop_clean), 'resumed': True, 'mission_id': mid,
           'mode': info.get('mode'),
           'ended_reason': ended_reason,
           'completed': bool(completed and stop_clean),
           'timed_out': ended_reason == 'wall_timeout',
           'wall_remaining_s': info['wall_remaining_s'],
           'wall_deadline_utc': info.get('wall_deadline_utc'),
           'expected_generation': info.get('expected_generation'),
           'stop': stop_result, 'stop_called': stop_result is not None}
    if not stop_clean:
        # R3.9 / A3：停止失败或线程仍存活时如实记录，保留模板注册与句柄
        out['stop_failed'] = True
    if completed:
        repo = Repository(db)
        try:
            params_rc = repo._conn.execute(
                'SELECT params_json FROM run_control WHERE id=1').fetchone()
            params = json.loads(params_rc['params_json'] or '{}')
            mode = 'real' if params.get('real') else 'zero-model'
            root_id = params.get('root') or ''
            root_node = repo.get_node(root_id)
            frozen = home / 'frozen'
            exams = [str(frozen / 'checks.py'), str(frozen / 'manifest.json'),
                     str(frozen / 'structure.py')]
            digest = compute_check_digest(check_cmd=root_node.check_cmd,
                                          exam_files=tuple(exams))
            summary = summarize(home, mid, root_id, digest=digest, mode=mode,
                                limits={})
            out.update(summary)
            out.update({'resumed': True, 'mission_id': mid,
                        'ended_reason': ended_reason,
                        'completed': completed,
                        'wall_deadline_utc': info.get('wall_deadline_utc'),
                        'stop': stop_result})
        finally:
            repo.close()
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('zero-model', 'real'), default='zero-model')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--timeout', type=float, default=21600)
    parser.add_argument('--backend-name', default=None,
                        help='执行器名（新建默认 claude-cli；resume 未提供'
                        '时沿用原运行已保存后端，不被默认值覆盖）')
    parser.add_argument('--backend-options', type=Path,
                        help='JSON adapter options 文件路径（内容如 '
                        '{"env_file": "..."}），不得内联凭据')
    parser.add_argument('--resume', action='store_true',
                        help='跨进程恢复既有任务（--output 指向原运行目录；'
                        '核对配置一致后沿用 accepted/预算/拆分额度续跑）')
    parser.add_argument('--pause-ack', action='store_true',
                        help='确认供应商暂停已人工处置，恢复该任务')
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    options = (json.loads(args.backend_options.read_text(encoding='utf-8'))
               if args.backend_options else None)
    if args.resume:
        if args.output is None:
            parser.error('--resume 需要 --output 指向原运行目录')
        try:
            result = resume_blocking(args.output, backend_name=args.backend_name,
                                     backend_options=options,
                                     provider_pause_ack=args.pause_ack)
        except Exception as exc:
            result = {'ok': False, 'resumed': False,
                      'error': f'{type(exc).__name__}: {exc}'}
        out_dir = args.output.resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / 'resume_results.json'
        write_error = None
        fallback_path = None
        try:
            out.write_text(json.dumps(result, ensure_ascii=False, indent=2)
                           + '\n', encoding='utf-8')
        except Exception as exc:
            # R3.9 / A3：结果写盘失败如实留证（fallback 文件+stderr），
            # 纳入顶层失败状态与非零退出码，不指向未生成的目标文件
            write_error = f'{type(exc).__name__}: {exc}'
            fb = out_dir / 'resume_results.write-failed.json'
            try:
                fb.write_text(json.dumps(
                    {'write_error': write_error,
                     'original_result': {k: v for k, v in result.items()
                                         if isinstance(v, (str, int, float,
                                                           bool, type(None)))}},
                    ensure_ascii=False, indent=1), encoding='utf-8')
                fallback_path = str(fb)
            except Exception:
                pass
            import sys as _sys
            _sys.stderr.write(f"[resume] 结果写盘失败: {exc}\n")
        overall_ok = bool((result.get('success') or result.get('ok'))
                          and not write_error)
        top_err = result.get('error') or (
            f'结果写盘失败: {write_error}' if write_error else None)
        summary_out = {
            'resumed': result.get('resumed'),
            'success': overall_ok,
            'ended_reason': result.get('ended_reason'),
            'mission_id': result.get('mission_id'),
            'results': None if write_error else str(out),
            'error': top_err,
        }
        if write_error:
            summary_out['write_error'] = write_error
            summary_out['fallback_results'] = fallback_path
        print(json.dumps(summary_out, ensure_ascii=False))
        return 0 if overall_ok else 1
    if args.output is None:
        base = Path.cwd() / 'dag-agent-runs' / 'full50'
        base.mkdir(parents=True, exist_ok=True)
        args.output = Path(tempfile.mkdtemp(prefix=f'{args.mode}-', dir=base))
    result = run(args.output, args.mode, args.timeout,
                 args.backend_name or 'claude-cli', options)
    print(json.dumps({'success': result['success'], 'mode': args.mode,
                      'results': str(args.output.resolve() /
                                     ('real_run_results.json' if args.mode == 'real'
                                      else 'run_results.json')),
                      'error': result.get('error'),
                      'summary_error': (result.get('summary_error') or '')[:400]},
                     ensure_ascii=False))
    return 0 if result['success'] else 1


if __name__ == '__main__':
    sys.exit(main())
