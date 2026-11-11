"""Six-node, task-local runner. Default zero-model; real mode must be explicit."""
from __future__ import annotations
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
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
from dag_agent.briefing import check_briefing
from dag_agent.daemon import RunManager
from dag_agent.graph import Graph
from dag_agent.repository import Repository
from techstack_research_v2.checks import IMPORTS, ROOT_FILES, sha
from techstack_research_v2.templates import artifacts, brief, command, register, restore

PKG = Path(__file__).resolve().parent


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def prepare(home, mode):
    frozen = home / 'frozen'
    inputs = frozen / 'inputs'
    inputs.mkdir(parents=True)
    for name in IMPORTS[:-1]:
        source = PKG / 'inputs/requirements.json' if name == 'requirements.json' else PKG / name
        shutil.copyfile(source, inputs / name)
    save(inputs / 'input-provenance.json', {
        'origin': 'host_import', 'model_generated': False, 'mode': mode,
        'files': {name: sha(inputs / name) for name in IMPORTS[:-1]}})
    checker = frozen / 'checks.py'
    shutil.copyfile(PKG / 'checks.py', checker)
    manifest = frozen / 'manifest.json'
    save(manifest, {'schema_version': 1, 'mode': mode,
                    'imports': {name: sha(inputs / name) for name in IMPORTS}})
    # Validate the published example with the platform parser, not a local clone.
    import re
    raw = re.search(r'```json\s*(.*?)```', (inputs / 'proposal-protocol.md').read_text(encoding='utf-8-sig'), re.S).group(1)
    parse_proposal(raw.encode('utf-8'), {}, 'n-review', 'a-review')
    commands = {stage: command(checker, manifest, stage)
                for stage in ('import', 'research', 'postgresql', 'clickhouse', 'comparison', 'root')}
    for stage, cmd in commands.items():
        ok, why, _ = preflight(check_cmd=cmd)
        if not ok:
            raise ValueError(f'{stage} preflight: {why}')
        if not check_briefing(brief(stage, cmd))['ok']:
            raise ValueError(f'{stage}: incomplete briefing')
    return checker, manifest, inputs, commands


def build(home, checker, manifest, inputs, commands):
    repo = Repository(home / 'mission.db')
    graph = Graph(repo, checker3=default_checker3)
    exams = [str(checker), str(manifest)]
    try:
        mid, root = graph.create_mission('数据库六节点冒烟', brief('root', commands['root']),
                                        root_check=commands['root'], root_declared_artifacts=ROOT_FILES)
        # Public API explicitly freezes the root checker and its manifest before execution.
        graph.repair_check(mid, root, new_check_cmd=commands['root'], exam_files=exams,
                           reason='运行前由出题方冻结任务考卷及输入摘要')
        imp = graph.add_node(mid, '资料导入（宿主）', brief('import', commands['import']), parent_id=root,
                             check_cmd=commands['import'], declared_artifacts=IMPORTS, exam_files=tuple(exams))
        research = graph.add_node(mid, '数据库技术研究', brief('research', commands['research']), parent_id=root,
                                  deps=(imp,), check_cmd=commands['research'],
                                  declared_artifacts=artifacts('research'), exam_files=tuple(exams))
        initial_frontier = graph.frontier(mid)
        if initial_frontier != [imp]:
            raise ValueError(f'导入前frontier错误: {initial_frontier}')
        aid = graph.start_attempt(mid, imp, backend='host-import')
        workdir = Path(repo.get_attempt(aid).workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        for name in IMPORTS:
            shutil.copyfile(inputs / name, workdir / name)
        imported = graph.submit_attempt(mid, imp, aid, artifact_paths=list(IMPORTS))
        if imported.get('verdict') != 'pass':
            raise ValueError(f'导入未通过: {imported}')
        with repo.tx() as cur:
            repo.append_event(cur, mid, 'host_inputs_imported', imp, model_generated=False,
                              files={name: sha(inputs / name) for name in IMPORTS})
        if graph.frontier(mid) != [research]:
            raise ValueError('导入后应只有研究节点就绪')
        digest = compute_check_digest(check_cmd=commands['root'], exam_files=tuple(exams))
        return mid, root, research, digest
    finally:
        repo.close()


def summarize(home, mid, root, research, digest):
    repo = Repository(home / 'mission.db')
    graph = Graph(repo, checker3=default_checker3)
    try:
        nodes = repo.list_nodes(mid)
        proposals = [dict(p) for p in repo.list_split_proposals(mid)]
        events = repo.events_of(mid)
        event_types = [e.type for e in events]
        root_node = repo.get_node(root)
        current_digest = compute_check_digest(check_cmd=root_node.check_cmd, exam_files=graph._exam_files_of(root_node))
        attempts = [dict(r) for r in repo._conn.execute('SELECT id,node_id,backend,status,category,workdir,input_binding_json,resources_json FROM attempts')]
        deps = {n.id: [d.depends_on for d in repo.deps_of(n.id)] for n in nodes}
        published = home / 'accepted' / root / f'v{root_node.accepted_artifact_version}'
        checks = {
            'six_nodes': len(nodes) == 6,
            'mission_done_machine': repo.get_mission(mid).status == 'done' and repo.get_mission(mid).closure_basis == 'machine',
            'all_accepted': all(n.status == 'accepted' for n in nodes),
            'one_applied_split': len(proposals) == 1 and proposals[0]['status'] == 'applied' and proposals[0]['nodes_added'] == 3,
            'event_chain': all(t in event_types for t in ('host_inputs_imported', 'split_proposed', 'split_activated', 'split_applied')),
            'root_check_unchanged': current_digest == digest,
            'root_files_published': all((published / name).is_file() for name in ROOT_FILES),
        }
        child_nodes = [n for n in nodes if n.parent_id == research]
        from techstack_research_v2.templates import IDS
        selected = [c.get('template_id') for p in proposals for c in json.loads(p['raw_json'])['children']]
        checks['trusted_templates_selected'] = sorted(selected) == sorted(IDS.values()) and all(
            '--manifest' in (n.check_cmd or '') and '/checks.py' in (n.check_cmd or '') for n in child_nodes)
        checks['proposal_attempt_released'] = len([a for a in attempts if a['category'] == 'split' and a['node_id'] == research]) == 1
        checks['child_input_bindings'] = len(child_nodes) == 3 and all(
            any(a['node_id'] == n.id and a['status'] == 'accepted' and
                {b['source'] for b in json.loads(a['input_binding_json'])} == set(deps[n.id])
                for a in attempts) for n in child_nodes)
        root_assessments = repo.assessments_of(root)
        checks['root_clean_replay'] = any(a.authoritative and not a.invalid and a.verdict == 'pass'
                                           and '干净重放通过' in a.evidence for a in root_assessments)
        spans = []
        for a in attempts:
            if a['node_id'] in {n.id for n in child_nodes if '--stage comparison' not in (n.check_cmd or '')}:
                resources = json.loads(a['resources_json'])
                if resources.get('executor_started_at') and resources.get('executor_ended_at'):
                    spans.append((datetime.fromisoformat(resources['executor_started_at']),
                                  datetime.fromisoformat(resources['executor_ended_at'])))
        parallel_overlap = any(a[0] < b[1] and b[0] < a[1] for i, a in enumerate(spans) for b in spans[i+1:])
        checks['frozen_artifacts_valid'] = all(graph.store.verify_accepted(repo, n.id).get('ok') for n in nodes if n.status == 'accepted')
        save(home / 'events.json', [asdict(e) for e in events])
        save(home / 'attempts.json', attempts)
        save(home / 'proposals.json', proposals)
        save(home / 'root_assessments.json', [asdict(a) for a in root_assessments])
        return {'checks': checks, 'success': all(checks.values()),
                'mission_status': repo.get_mission(mid).status,
                'nodes': [{'id': n.id, 'title': n.title, 'status': n.status, 'deps': deps[n.id]} for n in nodes],
                'root_published': str(published), 'content_review': 'pending',
                'planning_mode': 'trusted_templates', 'template_ids': selected,
                'analysis_execution_overlap': parallel_overlap}
    finally:
        repo.close()


def run(home, mode='zero-model', timeout=120, backend_name='claude-cli', backend_options=None):
    home = Path(home).resolve()
    if home.exists() and any(home.iterdir()):
        raise ValueError('运行目录必须为空；不会清理或覆盖已有结果')
    home.mkdir(parents=True, exist_ok=True)
    report = {'mode': mode, 'success': False, 'synthetic_fixture': mode == 'zero-model',
              'limits': {'max_calls': 12, 'node_cap': 7, 'expected_nodes': 6, 'timeout_s': timeout},
              'content_review': 'pending', 'started_at': datetime.now(timezone.utc).isoformat()}
    manager = None
    old = None
    mid = None
    try:
        if mode not in ('zero-model', 'real'):
            raise ValueError('未知运行模式')
        checker, manifest, inputs, commands = prepare(home, mode)
        old = register(checker, manifest)
        if mode == 'real':
            from dag_agent.backends import create_backend
            backend = create_backend(backend_name, backend_options)
            available, reason = backend.available()
            if not available:
                raise ValueError(f'后端预检未通过: {reason}')
        else:
            from techstack_research_v2.fixture_backend import FixtureBackend
            backend = FixtureBackend()
        mid, root, research, digest = build(home, checker, manifest, inputs, commands)
        report.update(mission_id=mid, root=root, research=research)
        manager = RunManager(home)
        start = manager.start(mid, root=root, backend_factory=lambda: backend, real=mode == 'real',
                              auto_split=True, project=True, max_concurrency=2,
                              # Scheduler halts at count >= cap; six expected nodes need a cap of seven.
                              max_retries=1, max_calls=12, node_cap=7, timeout_s=timeout)
        if not start.get('ok'):
            raise ValueError(f'启动失败: {start}')
        deadline = time.monotonic() + timeout
        while True:
            state = manager.status(mid)
            if not state.get('running'):
                break
            if time.monotonic() >= deadline:
                report['timed_out'] = True
                break
            time.sleep(.1)
        report['last_status'] = state
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        # Stop before restoring the process-local template registry, even on status failures.
        if manager is not None and mid is not None:
            try:
                stopped = manager.stop(mid, mode='kill')
                report['stop'] = stopped
            except Exception as exc:
                report['stop_error'] = f'{type(exc).__name__}: {exc}'
            if report.get('stop', {}).get('stopped'):
                try:
                    report.update(summarize(home, mid, root, research, digest))
                except Exception as exc:
                    report['summary_error'] = f'{type(exc).__name__}: {exc}'
                restore(old)
                old = None
                try:
                    manager.cleanup_finished(keep=0)
                except Exception as exc:
                    report['cleanup_error'] = f'{type(exc).__name__}: {exc}'
        elif old is not None:
            restore(old)
            old = None
        if any(k in report for k in ('error', 'stop_error', 'summary_error', 'timed_out')) or not report.get('stop', {}).get('stopped'):
            report['success'] = False
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        save(home / 'run_results.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('zero-model', 'real'), default='zero-model')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--backend-name', choices=('claude-cli', 'zcode-cli'), default='claude-cli')
    parser.add_argument('--backend-options', type=Path, help='JSON adapter options, no inline credentials')
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    if args.output is None:
        base = ROOT / 'work/db-smoke-runs'
        base.mkdir(parents=True, exist_ok=True)
        args.output = Path(tempfile.mkdtemp(prefix=args.mode + '-', dir=base))
    options = json.loads(args.backend_options.read_text(encoding='utf-8')) if args.backend_options else None
    result = run(args.output, args.mode, args.timeout, args.backend_name, options)
    print(json.dumps({'success': result['success'], 'mode': args.mode,
                      'results': str(args.output.resolve() / 'run_results.json'),
                      'error': result.get('error'), 'summary_error': result.get('summary_error')}, ensure_ascii=False))
    return 0 if result['success'] else 1


if __name__ == '__main__':
    sys.exit(main())
