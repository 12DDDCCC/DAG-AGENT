# -*- coding: utf-8 -*-
"""拆分额度接线回归（任务书 2026-11-06 §4）：
默认仍5 / 显式12生效 / 非法数值拒绝 / 恢复保留 / 额度耗尽拒绝继续生长。"""
from __future__ import annotations
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from dag_agent.autogrow import TemplateViolation
from dag_agent.autogrow import new_proposal_id
from dag_agent.acceptance import compute_check_digest
from dag_agent.daemon import RunManager
from dag_agent.graph import Graph
from dag_agent.repository import Repository
from dag_agent.scheduler import AutogrowConfig

REAL_DIGEST = compute_check_digest(check_cmd='python -c pass', exam_files=())


def _mk_mission(home: Path):
    repo = Repository(home / 'mission.db')
    graph = Graph(repo, checker3=None)
    mid, root = graph.create_mission('额度回归', '【输入】x\n【交付清单】x\n'
                                     '【接口签名】x\n【样例】x\n【自测入口】python -c pass\n')
    return repo, graph, mid, root


def test_default_config_still_5():
    c = AutogrowConfig()
    assert c.max_splits_per_mission == 5 and c.max_depth == 5 and c.enabled


def test_explicit_12_accepted_by_start_validation(tmp_path):
    r = RunManager(Path(tmp_path)).start('m-none', max_splits_per_mission=12)
    assert r.get('ok') is False and '任务库不存在' in r.get('error', '')


@pytest.mark.parametrize('bad', [0, -1, 51, 100, True, False, '12', 12.5, None])
def test_illegal_values_rejected(bad, tmp_path):
    with pytest.raises(ValueError):
        RunManager(Path(tmp_path)).start('m-x', max_splits_per_mission=bad)


def test_quota_persisted_and_survives_recovery(tmp_path):
    """显式12进 run_control.params_json；kick 重建时保留。"""
    home = Path(tmp_path)
    repo, graph, mid, root = _mk_mission(home)
    repo.close()
    mgr = RunManager(home)
    r = mgr.start(mid, max_concurrency=1, max_calls=0, auto_split=True,
                  max_splits_per_mission=12)
    assert r.get('ok'), r
    time.sleep(0.3)  # max_calls=0 调度器立即熔断，让线程落定
    mgr.stop(mid, mode='kill')
    rec = Repository(home / 'mission.db')._conn.execute(
        'SELECT params_json FROM run_control WHERE id=1').fetchone()
    params = json.loads(rec['params_json'])
    assert params.get('max_splits_per_mission') == 12, params
    kw = {k: v for k, v in params.items() if not k.startswith('_')}
    assert kw.get('max_splits_per_mission') == 12
    r2 = mgr.kick(mid)  # start(**kw) 不抛 ValueError 即恢复保留
    assert 'max_splits' not in str(r2.get('error', ''))


def test_batch_rejection_when_over_quota(tmp_path):
    """额度耗尽：graph.apply_auto_split 第 max+1 次拒绝（护栏在图层）。"""
    repo, graph, mid, root = _mk_mission(Path(tmp_path))
    try:
        graph.add_node(mid, '父', '【输入】x\n【交付清单】x\n【接口签名】x\n'
                       '【样例】x\n【自测入口】python -c pass\n', parent_id=root,
                       check_cmd='python -c pass')
        aid = graph.start_attempt(mid, 'n2', backend='test')
        with repo.tx() as cur:
            cur.execute("UPDATE nodes SET status='waiting_split' WHERE id='n2'")
            cur.execute("UPDATE attempts SET status='split', category='split' WHERE id=?", (aid,))
        spec = {'title': 'c1', 'statement': 's', 'check_cmd': 'python -c pass',
                'acceptance': None, 'exam_files': [], 'declared_artifacts': [],
                'priority': 0, 'integration_check': None, 'exploratory': True,
                'source_local_id': 'c1', 'deps': [], '_frozen_digest': REAL_DIGEST}
        kids = [dict(spec, title=f'c{i}', source_local_id=f'c{i}') for i in (1, 2)]
        fp = {'goal_version': 1, 'node_revision': 1, 'check_version': 1,
              'check_digest': REAL_DIGEST, 'input_binding_hash': ''}
        with repo.tx() as cur:
            repo.append_event(cur, mid, 'split_proposed', 'n2')
            cur.execute(
                "INSERT INTO split_proposals(id,mission_id,node_id,source_attempt_id,"
                "raw_sha256,content_sha256,raw_json,fingerprint_json,status,"
                "plan_attempts,created_at) VALUES(?,?,?,?,?,?,?,?,?,0,datetime('now'))",
                (new_proposal_id(), mid, 'n2', aid, 'r', 'c', json.dumps({'children': kids}),
                 json.dumps(fp), 'queued'))
        pid = repo._conn.execute(
            "SELECT id FROM split_proposals WHERE mission_id=?", (mid,)
        ).fetchone()['id']
        graph.apply_auto_split(mid, 'n2', aid, pid, kids, fp,
                               max_splits_per_mission=1, max_depth=5,
                               node_cap=None)
        # 第二个父节点的新提案：额度已耗尽（1/1），必须被护栏拒绝
        p2 = graph.add_node(mid, '父2', '【输入】x\n【交付清单】x\n【接口签名】x\n'
                            '【样例】x\n【自测入口】python -c pass\n', parent_id=root,
                            check_cmd='python -c pass')
        aid2 = graph.start_attempt(mid, p2, backend='test')
        with repo.tx() as cur:
            cur.execute("UPDATE nodes SET status='waiting_split' WHERE id=?", (p2,))
            cur.execute("UPDATE attempts SET status='split', category='split' WHERE id=?", (aid2,))
            repo.append_event(cur, mid, 'split_proposed', p2)
            cur.execute(
                "INSERT INTO split_proposals(id,mission_id,node_id,source_attempt_id,"
                "raw_sha256,content_sha256,raw_json,fingerprint_json,status,"
                "plan_attempts,created_at) VALUES(?,?,?,?,?,?,?,?,?,0,datetime('now'))",
                (new_proposal_id(), mid, p2, aid2, 'r2', 'c2', json.dumps({'children': kids}),
                 json.dumps(fp), 'queued'))
        pid2 = repo._conn.execute(
            "SELECT id FROM split_proposals WHERE node_id=?", (p2,)).fetchone()['id']
        with pytest.raises(Exception) as ei:
            graph.apply_auto_split(mid, p2, aid2, pid2, kids, fp,
                                   max_splits_per_mission=1, max_depth=5,
                                   node_cap=None)
        assert '护栏' in str(ei.value)
    finally:
        repo.close()
