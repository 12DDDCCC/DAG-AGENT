# -*- coding: utf-8 -*-
"""50节点零模型桩：仅在独立测试目录使用（mode=zero-model）。

生成能通过 checks.py 全部 stage 检查的合成交付；提案由桩程序化构造并以
`fixture-` 前缀明确标记。绝不能进入真实任务工件（真实模式由 claude-cli
后端执行，本模块不被加载）。
"""
from __future__ import annotations
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import time
import uuid

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from techstack_full_v2.checks import (ASPECTS, CROSS_STUDIES, DOMAINS,
                                      DOMAIN_TECHS, IMPORTS, TECHS, bound,
                                      category_of, read_json, sha,
                                      root_expected_deliverables)
from techstack_full_v2.structure import (ASPECT_TITLE, cross_artifacts,
                                         cross_study_artifacts,
                                         domain_artifacts,
                                         domain_compare_artifacts,
                                         root_expected_deliverables,
                                         tech_artifacts, domains_split_children,
                                         domain_split_children,
                                         tech_split_children,
                                         cross_split_children)

MARK = '合成测试数据，不用于技术决策'


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n',
                          encoding='utf-8')


def collect(folder, names):
    """从 inputs/<来源>/ 原样转交（同名校验由 bound 保证）。"""
    for name in names:
        shutil.copyfile(bound(folder, name), Path(folder) / name)


def synth_evidence(subject, mode, extra=None, with_costs=False,
                   domain_refs=None):
    facts = []
    for i, kind in enumerate(('capability', 'benchmark', 'case')):
        fact = {'id': f'{subject[:24]}-{kind[:4]}-{i+1:02d}', 'kind': kind,
                'claim': f'{subject} 的{kind}合成契约样例（第{i+1}条），用于验证结构、引用与追溯，不陈述真实产品能力。',
                'basis': 'synthetic' if mode == 'zero-model' else 'published',
                'source': {'url': f'https://fixtures.invalid/{subject}/{kind}',
                           'title': '合成测试来源，不是真实厂商文档',
                           'locator': '合成样例 §1 版本 0.0',
                           'accessed_on': '2026-11-07'}}
        if kind == 'benchmark':
            fact['metric'] = {'value': '1200', 'unit': 'QPS',
                              'workload': '合成负载样例，仅检验字段结构，没有任何真实性能测试。',
                              'environment': '合成环境，未安装或运行被研究软件，禁止解释为实测。'}
        facts.append(fact)
    data = {'schema_version': 1, 'subject': subject, 'mode': mode,
            'review_status': 'unverified', 'facts': facts,
            'limitations': ['仅零模型测试夹具，无任何外部事实验证，不可用于技术决策或采购。']}
    if with_costs:
        data['costs'] = [{'id': f'{subject[:20]}-cost-01', 'item': '合成计算资源月度费用',
                          'quantity': '720', 'quantity_unit': '小时',
                          'unit_price': '0.50', 'monthly_total': '360.00',
                          'period': 'month', 'currency': 'CNY',
                          'price_date': '2026-11-07', 'region': 'CN',
                          'source_id': facts[0]['id']}]
    if domain_refs is not None:
        data['domain_refs'] = domain_refs
    if extra:
        data.update(extra)
    return data


def evidence_md(subject, data, headings_names):
    refs = ' '.join(f'[@{f["id"]}]' for f in data['facts'])
    out = f'# {subject}\n{MARK}\n'
    for h in headings_names:
        out += f'\n## {h}\n本段仅验证契约结构与交付流转，全部内容为合成样例。{refs}\n'
    return out


def all_deep_ids(folder, techs):
    """从 inputs 快照收集给定技术的全部深度证据 ID。"""
    ids = set()
    for t in techs:
        for a in ASPECTS:
            p = bound(folder, f'{t}-{a}-evidence.json')
            ids |= {f['id'] for f in read_json(p)['facts']}
    return ids


def make_aspect(folder, subject, mode):
    tech, aspect = subject.rsplit('-', 1)
    data = synth_evidence(subject, mode, with_costs=(aspect == 'cost'))
    write_json(folder / f'{subject}-evidence.json', data)
    (folder / f'{subject}.md').write_text(
        evidence_md(subject, data, ASPECT_SPEC_HEAD[aspect]), encoding='utf-8')


ASPECT_SPEC_HEAD = {
    'feature': ('核心机制', '适用与不适用场景', '接口与运维边界'),
    'performance': ('指标与来源', '适用条件', '不适用条件'),
    'case': ('案例与来源', '采用原因', '迁移限制'),
    'cost': ('计费口径', '成本计算', '未计项目'),
}


def make_tech_integration(folder, tech, mode):
    names = list(tech_artifacts(tech))
    collect(folder, [n for n in names if n not in
                     (f'{tech}-integration.md', f'{tech}-integration.json')])
    ids = set()
    for a in ASPECTS:
        ids |= {f['id'] for f in read_json(bound(folder, f'{tech}-{a}-evidence.json'))['facts']}
    upstream = {n: sha(folder / n) for n in names
                if n not in (f'{tech}-integration.md', f'{tech}-integration.json')}
    write_json(folder / f'{tech}-integration.json', {
        'schema_version': 1, 'subject': f'{tech}-integration', 'mode': mode,
        'review_status': 'unverified', 'upstream': upstream,
        'conclusions': [f'{tech} 四个深度交付的结构化整合结论（合成），至少三十个字符以通过检查。'],
        'open_questions': []})
    refs = ' '.join(f'[@{i}]' for i in sorted(ids))
    (folder / f'{tech}-integration.md').write_text(
        f'# {tech} 整合\n{MARK}\n\n## 整合结论\n四份深度交付原样汇集，本段为合成整合说明。\n{refs}\n\n'
        f'## 证据索引\n全部上游证据ID已在上文引用；哈希见 integration.json。\n', encoding='utf-8')


def make_domain_compare(folder, domain, mode):
    techs = DOMAIN_TECHS[domain]
    requirements = read_json(bound(folder, 'requirements.json'))
    deep = {t: all_deep_ids(folder, [t]) for t in techs}
    data = {'schema_version': 1, 'subject': f'{domain}-comparison', 'mode': mode,
            'review_status': 'unverified',
            'input_sha256': {f'{t}-integration.json': sha(bound(folder, f'{t}-integration.json'))
                             for t in techs},
            'criteria': [{'requirement_id': r['id'],
                          'assessment': '针对该条冻结需求的合成逐项比较：说明两两技术在此需求上的适用条件差异、证据局限与未验证项；本句仅验证结构与引用完整性，不构成真实结论。',
                          'evidence': {t: [sorted(deep[t])[0]] for t in techs}}
                         for r in requirements['requirements']],
            'recommendation': {'ranking': list(techs), 'status': 'provisional',
                               'rationale': '合成推荐仅用于检查字段与排序结构，不构成技术选型结论；全部证据均为零模型夹具生成，'
                                            '没有任何真实基准、价格或案例支撑，排序与取舍逻辑不代表真实适用性；'
                                            '真实决策必须重新收集官方一手来源、冻结工作负载、执行实测并由独立人员复核需求覆盖与成本遗漏后才能做出。'}}
    write_json(folder / f'{domain}-comparison.json', data)
    refs = ' '.join(f'[@{i}]' for t in techs for i in sorted(deep[t])[:2])
    (folder / f'{domain}-comparison.md').write_text(
        f'# {domain} 比较\n{MARK}\n\n## 逐项比较\n合成逐项比较内容。{refs}\n\n'
        f'## 成本对比\n合成成本对比（数值见各技术成本证据）。\n\n'
        f'## 初步推荐\n{data["recommendation"]["rationale"]}\n', encoding='utf-8')


def make_domain_integration(folder, domain, mode):
    names = [n for n in domain_artifacts(domain)
             if n not in (f'{domain}-integration.md', f'{domain}-integration.json')]
    collect(folder, names)
    ids = set()
    for t in DOMAIN_TECHS[domain]:
        for a in ASPECTS:
            ids |= {f['id'] for f in read_json(bound(folder, f'{t}-{a}-evidence.json'))['facts']}
    write_json(folder / f'{domain}-integration.json', {
        'schema_version': 1, 'subject': f'{domain}-integration', 'mode': mode,
        'review_status': 'unverified',
        'upstream': {n: sha(folder / n) for n in names},
        'conclusions': [f'{domain} 领域全部子交付的结构化整合结论（合成），字符数满足检查要求。'],
        'open_questions': []})
    refs = ' '.join(f'[@{i}]' for i in sorted(ids)[:60])
    (folder / f'{domain}-integration.md').write_text(
        f'# {domain} 领域整合\n{MARK}\n\n## 领域结论\n全部子交付原样汇集（合成说明）。\n{refs}\n\n'
        f'## 证据索引\n上游证据ID见上文引用；完整哈希清单见 integration.json。\n', encoding='utf-8')


def make_cross_study(folder, name, mode):
    dr = {}
    for d in DOMAINS:
        ids = all_deep_ids(folder, DOMAIN_TECHS[d])
        dr[d] = [sorted(ids)[0]]
    data = synth_evidence(f'cross-{name}', mode, with_costs=(name == 'total-cost'),
                          domain_refs=dr)
    if name == 'total-cost':
        data['costs'] = [{'id': 'cross-total-cost-monthly', 'item': '七技术月度成本合计（合成）',
                          'quantity': '1', 'quantity_unit': '套',
                          'unit_price': '83333.33', 'monthly_total': '83333.33',
                          'period': 'month', 'currency': 'CNY',
                          'price_date': '2026-11-07', 'region': 'CN',
                          'source_id': data['facts'][0]['id']}]
    write_json(folder / f'cross-{name}-evidence.json', data)
    (folder / f'cross-{name}.md').write_text(
        evidence_md(f'cross-{name}', data, ('分析', '依据与来源', '限制')), encoding='utf-8')


def make_cross_integration(folder, mode):
    names = [n for n in cross_artifacts()
             if n not in ('cross-integration.md', 'cross-integration.json')]
    collect(folder, names)
    ids = set()
    for n in CROSS_STUDIES:
        ids |= {f['id'] for f in read_json(bound(folder, f'cross-{n}-evidence.json'))['facts']}
    write_json(folder / 'cross-integration.json', {
        'schema_version': 1, 'subject': 'cross-integration', 'mode': mode,
        'review_status': 'unverified',
        'upstream': {n: sha(folder / n) for n in names},
        'conclusions': ['五份跨领域研究的结构化整合结论（合成），满足检查的字符数要求。'],
        'open_questions': []})
    refs = ' '.join(f'[@{i}]' for i in sorted(ids)[:60])
    (folder / 'cross-integration.md').write_text(
        f'# 跨领域整合\n{MARK}\n\n## 整合结论\n五份研究原样汇集（合成说明）。\n{refs}\n\n'
        f'## 证据索引\n见上文引用与 integration.json 哈希清单。\n', encoding='utf-8')


def make_research_integration(folder, mode):
    expected = set()
    from techstack_full_v2.structure import domain_artifacts as da, cross_artifacts as ca
    for d in DOMAINS:
        expected |= set(da(d))
    expected |= set(ca())
    names = sorted(expected - {'research-integration.md', 'research-integration.json'})
    collect(folder, names)
    all_ids = set()
    for t in TECHS:
        for a in ASPECTS:
            all_ids |= {f['id'] for f in read_json(bound(folder, f'{t}-{a}-evidence.json'))['facts']}
    for n in CROSS_STUDIES:
        all_ids |= {f['id'] for f in read_json(bound(folder, f'cross-{n}-evidence.json'))['facts']}
    write_json(folder / 'research-integration.json', {
        'schema_version': 1, 'subject': 'research-integration', 'mode': mode,
        'review_status': 'unverified',
        'upstream': {n: sha(folder / n) for n in names},
        'conclusions': ['三个领域与跨领域全部交付的结构化整合（合成），满足检查要求。'],
        'open_questions': []})
    refs = ' '.join(f'[@{i}]' for i in sorted(all_ids)[:120])
    (folder / 'research-integration.md').write_text(
        f'# 总研究整合\n{MARK}\n\n## 整合结论\n全部领域与跨领域交付原样汇集（合成说明）。\n{refs}\n\n'
        f'## 证据索引\n证据ID见上文；哈希见 research-integration.json。\n', encoding='utf-8')


def make_root(folder, mode):
    # 根须保留全部交付与导入来源（direct 检查在根部找 IMPORTS）
    files = sorted(root_expected_deliverables()) + list(IMPORTS)
    collect(folder, files)
    dm = {'schema_version': 1,
          'deliverables': {n: sha(folder / n)
                           for n in root_expected_deliverables()}}
    write_json(folder / 'delivery-manifest.json', dm)
    sources, seen = [], set()
    all_ids = set()
    for ev in sorted(Path(folder).glob('inputs/*/*-evidence.json')):
        data = read_json(ev)
        if data.get('subject') == 'research-integration':
            continue
        for f in data.get('facts', []):
            all_ids.add(f['id'])
            key = (f['source']['url'], f['source']['locator'])
            if key not in seen:
                seen.add(key)
                sources.append({'id': f'src-{len(sources)+1:03d}',
                                'title': f['source']['title'],
                                'url': f['source']['url'],
                                'locator': f['source']['locator'],
                                'accessed_on': f['source']['accessed_on']})
    write_json(folder / 'source-index.json', {'schema_version': 1, 'mode': mode,
                                              'sources': sources})
    refs = ' '.join(f'[@{i}]' for i in sorted(all_ids)[:120])
    content = f'# 实时数据分析平台技术选型最终报告\n{MARK}\n'
    for h in ('执行摘要', '需求匹配结论', '成本与风险', '实施建议', '限制与验证状态'):
        content += (f'\n## {h}\n本报告整合50节点任务的全部已验收交付（本轮为零模型桩验证，'
                    f'内容为合成）。机器检查覆盖结构、一致性与追溯；content_review=pending，'
                    f'事实充分性待独立复核。{refs}\n')
    (folder / 'final-report.md').write_text(content, encoding='utf-8')
    write_json(folder / 'review.json', {'machine_scope': 'structure_consistency_traceability',
                                        'content_review': 'pending', 'mode': mode})


def build_proposal(stage, folder, mode):
    """按父节点 stage 构造桩提案（fixture- 前缀明确标记）。"""
    if stage == 'research-integration':
        children = domains_split_children()
    elif stage.endswith('-integration') and stage[:-len('-integration')] in DOMAINS:
        children = domain_split_children(stage[:-len('-integration')])
    elif stage.endswith('-integration') and stage[:-len('-integration')] in TECHS:
        children = tech_split_children(stage[:-len('-integration')])
    elif stage == 'cross-integration':
        children = cross_split_children()
    else:
        raise ValueError(f'非拆分节点: {stage}')
    return {'schema_version': '1', 'proposal_id': f'fixture-{uuid.uuid4().hex}',
            'source_attempt_id': 'platform', 'parent_node_id': 'platform',
            'goal_version': -1, 'node_revision': -1, 'check_version': -1,
            'check_digest': 'platform', 'input_binding_hash': 'platform',
            'reason': 'fixture 桩提案：验证受控生长结构（非真实模型自主拆分）',
            'children': [dict(c, statement=f'{c["title"]}（fixture 桩孩子）',
                              input_refs=[], deliverables=[]) for c in children]}


class FixtureBackendFull:
    """50节点零模型桩后端（仅测试目录）。"""
    name = 'fixture-full'

    def __init__(self, mode='zero-model'):
        self.mode = mode

    def run(self, statement, workdir, check_cmd, title='', cancel_event=None):
        started = datetime.now(timezone.utc).isoformat()
        t0 = time.monotonic()
        folder = Path(workdir)
        m = re.search(r'--stage (\S+)', check_cmd or '')
        stage = m.group(1) if m else 'unknown'
        cat = category_of(stage)
        if cat in ('tech-integration', 'domain-integration',
                   'cross-integration', 'research-integration'):
            # 判据=本节点**直接孩子**的标志性交付文件出现在 inputs 快照中
            # （领域/跨领域快照自带孙辈证据文件，不能作为"已有孩子"依据）
            if cat == 'tech-integration':
                marker = f'{stage[:-len("-integration")]}-feature.md'
            elif cat == 'domain-integration':
                marker = f'{stage[:-len("-integration")]}-comparison.md'
            elif cat == 'cross-integration':
                marker = 'cross-arch-dataflow.md'
            else:
                marker = 'database-integration.md'
            has_children = bool(list(folder.glob(f'inputs/*/{marker}')))
            if not has_children:
                (folder / '.dag').mkdir(exist_ok=True)
                write_json(folder / '.dag/proposal.json',
                           build_proposal(stage, folder, self.mode))
            else:
                {'tech-integration': lambda: make_tech_integration(
                     folder, stage[:-len('-integration')], self.mode),
                 'domain-integration': lambda: make_domain_integration(
                     folder, stage[:-len('-integration')], self.mode),
                 'cross-integration': lambda: make_cross_integration(folder, self.mode),
                 'research-integration': lambda: make_research_integration(folder, self.mode),
                 }[cat]()
        elif cat == 'tech-aspect':
            make_aspect(folder, stage, self.mode)
        elif cat == 'domain-compare':
            make_domain_compare(folder, stage[:-len('-compare')], self.mode)
        elif cat == 'cross-study':
            make_cross_study(folder, stage, self.mode)
        elif cat == 'root':
            make_root(folder, self.mode)
        else:
            raise ValueError(f'fixture 不处理的 stage: {stage}')
        ended = datetime.now(timezone.utc).isoformat()
        sid = 'fixture-' + uuid.uuid4().hex
        return {'ok': True, 'summary': 'synthetic full50 contract fixture',
                'session_id': sid,
                'resources': {'origin': 'stub', 'agent_id': sid, 'tokens': 0,
                              'duration_ms': int((time.monotonic() - t0) * 1000),
                              'executor_started_at': started,
                              'executor_ended_at': ended,
                              'cost_currency': 'USD',
                              'cost': {'amount': 0.0, 'basis': 'reported'},
                              'receipt_status': 'valid'}}
