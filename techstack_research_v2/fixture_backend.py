"""Synthetic fixtures only. This module is never loaded by real mode."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import time
import uuid

from techstack_research_v2.checks import ANALYSES, IMPORTS, RESEARCH, bound, read_json, sha

MARK = '合成测试数据，不用于技术决策'


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def make_analysis(folder, tech):
    prefix = 'pg' if tech == 'postgresql' else 'ch'
    facts = []
    for kind in ('capability', 'benchmark', 'case'):
        fact = {'id': f'{prefix}-{kind}', 'kind': kind,
                'claim': f'这是{tech}的{kind}合成契约样例，用于验证引用关联与字段结构，不陈述真实产品能力或案例。',
                'basis': 'synthetic',
                'source': {'url': f'https://fixtures.invalid/{tech}/{kind}',
                           'title': '合成测试来源，不是真实厂商文档', 'locator': '固定测试样例第一节', 'accessed_on': '2026-11-05'}}
        if kind == 'benchmark':
            fact['metric'] = {'value': '1200', 'unit': 'QPS',
                              'workload': '合成负载，固定种子样例，仅检验数据格式，没有进行数据库性能测试。',
                              'environment': '合成环境，没有安装或运行被研究数据库，禁止将该数值解释为实测结果。'}
        facts.append(fact)
    evidence = {'schema_version': 1, 'technology': tech, 'mode': 'zero-model',
                'review_status': 'unverified', 'facts': facts,
                'costs': [{'id': 'compute', 'item': '合成计算资源月度费用', 'quantity': '720',
                           'quantity_unit': '小时', 'unit_price': '0.50', 'monthly_total': '360.00',
                           'period': 'month', 'currency': 'CNY', 'source_id': f'{prefix}-case'}],
                'limitations': ['这些证据仅为机器检查测试夹具，没有任何外部事实验证，不可用于产品采购或技术选择。']}
    write_json(folder / f'{tech}-evidence.json', evidence)
    refs = ' '.join(f'[@{f["id"]}]' for f in facts)
    content = f'# {tech}\n{MARK}\n'
    for heading in ('技术概述', '核心特性', '性能与边界', '优势', '劣势', '适用场景', '成本'):
        content += f'\n## {heading}\n本段只验证契约和交付流转，测试数值及案例均为合成，不能支持真实技术推荐。{refs}\n'
    (folder / f'{tech}-analysis.md').write_text(content, encoding='utf-8')


def make_comparison(folder):
    requirements = read_json(bound(folder, 'requirements.json'))
    data = {'schema_version': 1,
            'input_sha256': {name: sha(bound(folder, name)) for name in ANALYSES},
            'criteria': [{'requirement_id': r['id'],
                          'assessment': '这是针对预设需求的合成比较，只验证引用、输入版本和计算关系；没有真实基准或客户案例，因此不能证明任何候选方案满足生产条件。',
                          'evidence': {'postgresql': ['pg-capability'], 'clickhouse': ['ch-capability']}}
                         for r in requirements['requirements']],
            'annual_cost_cny': {'postgresql': '4320.00', 'clickhouse': '4320.00'},
            'recommendation': {'technology': 'postgresql', 'status': 'provisional',
                               'rationale': '合成推荐仅用于检查字段，不构成技术选型结论。两份证据均由测试夹具生成，价格与性能数字没有任何外部事实支撑。真实决策必须重新收集官方来源、冻结工作负载、执行测量并由独立人员核验需求覆盖及成本遗漏；在此之前不能做生产选型。'}}
    write_json(folder / 'database-comparison.json', data)
    refs = ' '.join(f'[@{p}-{k}]' for p in ('pg', 'ch') for k in ('capability', 'benchmark', 'case'))
    (folder / 'database-comparison.md').write_text(
        f'# 数据库比较\n{MARK}\n{data["recommendation"]["rationale"]}\n{refs}\n', encoding='utf-8')


def collect(folder, names):
    for name in names:
        shutil.copyfile(bound(folder, name), folder / name)


def make_root(folder):
    collect(folder, IMPORTS + RESEARCH)
    refs = ' '.join(f'[@{p}-{k}]' for p in ('pg', 'ch') for k in ('capability', 'benchmark', 'case'))
    content = f'# 数据库选型冒烟报告\n{MARK}\n'
    for heading in ('执行摘要', '需求与决策', '成本与风险', '实施步骤', '验证边界'):
        content += f'\n## {heading}\n本次只验证平台受控生长和交付契约。输入由宿主导入，提案由桩生成；并未运行真实模型或验证数据库事实。{refs}\n'
    (folder / '99-数据库选型报告.md').write_text(content, encoding='utf-8')
    write_json(folder / 'review.json', {'machine_scope': 'structure_consistency_traceability',
                                       'content_review': 'pending', 'mode': 'zero-model'})


class FixtureBackend:
    name = 'stub'

    def run(self, statement, workdir, check_cmd, title='', cancel_event=None):
        started = datetime.now(timezone.utc).isoformat()
        t0 = time.monotonic()
        folder = Path(workdir)
        stage = re.search(r'--stage (\w+)', check_cmd).group(1)
        if stage == 'research' and '做整合交付' not in statement:
            protocol = bound(folder, 'proposal-protocol.md').read_text(encoding='utf-8-sig')
            proposal = json.loads(re.search(r'```json\s*(.*?)```', protocol, re.S).group(1))
            proposal['proposal_id'] = 'fixture-' + uuid.uuid4().hex
            (folder / '.dag').mkdir(exist_ok=True)
            write_json(folder / '.dag/proposal.json', proposal)
        elif stage in ('postgresql', 'clickhouse'):
            if cancel_event is not None and cancel_event.wait(.15):
                return {'ok': False, 'summary': 'cancelled', 'session_id': '', 'resources': {}}
            make_analysis(folder, stage)
        elif stage == 'comparison':
            make_comparison(folder)
        elif stage == 'research':
            collect(folder, RESEARCH)
        elif stage == 'root':
            make_root(folder)
        else:
            raise ValueError(f'Unexpected fixture stage: {stage}')
        ended = datetime.now(timezone.utc).isoformat()
        sid = 'fixture-' + uuid.uuid4().hex
        return {'ok': True, 'summary': 'synthetic contract fixture', 'session_id': sid,
                'resources': {'origin': 'stub', 'agent_id': sid, 'tokens': 0,
                              'duration_ms': int((time.monotonic() - t0) * 1000),
                              'executor_started_at': started, 'executor_ended_at': ended,
                              'cost_currency': 'USD', 'cost': {'amount': 0.0, 'basis': 'reported'},
                              'receipt_status': 'valid'}}
