"""Offline contract checks; success does not certify research truth."""
from __future__ import annotations
import argparse
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlparse

IMPORTS = ('requirements.json', 'proposal-protocol.md', 'deliverable-schema.md', 'input-provenance.json')
TECHS = ('postgresql', 'clickhouse')
ANALYSES = tuple(f'{t}-{s}' for t in TECHS for s in ('analysis.md', 'evidence.json'))
RESEARCH = ANALYSES + ('database-comparison.md', 'database-comparison.json')
ROOT_FILES = IMPORTS + RESEARCH + ('99-数据库选型报告.md', 'review.json')


class Invalid(ValueError):
    pass


def need(ok, message):
    if not ok:
        raise Invalid(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    def unique(pairs):
        out = {}
        for key, val in pairs:
            need(key not in out, f'重复JSON字段: {key}')
            out[key] = val
        return out
    def bad_number(value):
        raise Invalid(f'非法JSON数值: {value}')
    return json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=unique,
                      parse_constant=bad_number)


def text(value, minimum=1):
    return isinstance(value, str) and len(value.strip()) >= minimum


def money(value):
    need(isinstance(value, str), '金额和数量必须使用十进制字符串')
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise Invalid('非法十进制数') from exc
    need(number.is_finite() and number >= 0, '数值必须有限且非负')
    return number


def bound(folder, name):
    candidates = list((folder / 'inputs').glob(f'*/{name}'))
    need(bool(candidates), f'缺绑定输入: {name}')
    need(all(p.is_file() for p in candidates), f'绑定输入不是文件: {name}')
    need(len({sha(p) for p in candidates}) == 1, f'绑定输入版本有歧义: {name}')
    return candidates[0]


def imports(folder, manifest, direct=False):
    for name in IMPORTS:
        path = folder / name if direct else bound(folder, name)
        need(path.is_file() and sha(path) == manifest['imports'][name], f'输入哈希不符: {name}')
    path = folder / 'requirements.json' if direct else bound(folder, 'requirements.json')
    return read_json(path)


def citations(path, allowed, required=()):
    need(path.is_file(), f'缺正文: {path.name}')
    content = path.read_text(encoding='utf-8')
    need(len(content.strip()) >= 100, f'正文为空或过短: {path.name}')
    need(not any(word in content for word in ('无实际研究', '无实际分析', '无研究内容')), '拒绝已知填充反例')
    refs = set(re.findall(r'\[@([a-zA-Z0-9_-]+)\]', content))
    need(refs and refs <= allowed, f'正文有缺失或未知证据引用: {path.name}')
    need(set(required) <= refs, f'正文未覆盖必要证据: {path.name}')
    return content


def analysis(folder, tech, manifest):
    data = read_json(folder / f'{tech}-evidence.json')
    need(data['schema_version'] == 1 and data['technology'] == tech, '技术或schema不符')
    need(data['mode'] == manifest['mode'], '证据模式不符，禁止把合成样例冒充实测')
    need(data['review_status'] == 'unverified', '执行者不得自行签收事实复核')
    facts = data['facts']
    need(isinstance(facts, list) and len(facts) >= 3, '至少需要能力、性能、案例三类证据')
    ids, kinds = set(), set()
    for fact in facts:
        fid = fact['id']
        need(isinstance(fid, str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', fid), '证据ID非法')
        need(fid not in ids, '证据ID重复')
        ids.add(fid)
        need(fact['kind'] in ('capability', 'benchmark', 'case'), '证据类型非法')
        kinds.add(fact['kind'])
        need(text(fact['claim'], 30), '证据主张缺少明确描述')
        src = fact['source']
        url = urlparse(src['url'])
        need(url.scheme == 'https' and bool(url.hostname) and not url.username and not url.password,
             '来源必须为不带凭据的HTTPS URL')
        if manifest['mode'] == 'zero-model':
            need(url.hostname.endswith('.invalid') and fact['basis'] == 'synthetic', '测试证据必须显式为synthetic/.invalid')
        else:
            need('.' in url.hostname and not url.hostname.endswith(('.invalid', '.localhost'))
                 and url.hostname not in ('localhost', '127.0.0.1') and fact['basis'] in ('published', 'measured'),
                 '真实模式不接受测试来源或synthetic证据')
        need(text(src['title'], 5) and text(src['locator'], 5), '来源缺标题/定位')
        date.fromisoformat(src['accessed_on'])
        if fact['kind'] == 'benchmark':
            metric = fact['metric']
            need(money(metric['value']) > 0, '性能数值必须为正')
            need(metric['unit'] in ('QPS', 'TPS', 'ms', 'MB/s', 'GB/s'), '性能单位非法')
            need(text(metric['workload'], 20) and text(metric['environment'], 20), '性能缺工作负载/环境')
    need(kinds == {'capability', 'benchmark', 'case'}, '证据类型不完整')
    costs = data['costs']
    need(isinstance(costs, list) and bool(costs), '缺成本明细')
    cost_ids, total = set(), Decimal('0')
    for row in costs:
        need(text(row['id']) and row['id'] not in cost_ids, '成本行ID重复/缺失')
        cost_ids.add(row['id'])
        need(text(row['item'], 5) and text(row['quantity_unit']), '成本项目/单位缺失')
        need(row['period'] == 'month' and row['currency'] == 'CNY', '需统一月度/CNY口径')
        need(row['source_id'] in ids, '成本来源未绑定证据')
        expected = (money(row['quantity']) * money(row['unit_price'])).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        need(money(row['monthly_total']) == expected, '成本乘法不一致')
        total += expected
    need(isinstance(data['limitations'], list) and any(text(x, 20) for x in data['limitations']), '缺适用限制')
    content = citations(folder / f'{tech}-analysis.md', ids, ids)
    for heading in ('技术概述', '核心特性', '性能与边界', '优势', '劣势', '适用场景', '成本'):
        need(re.search(r'^## ' + heading + r'\s*$', content, re.M), f'缺实际二级章节: {heading}')
    if manifest['mode'] == 'zero-model':
        need('合成测试数据，不用于技术决策' in content, '测试报告缺合成标记')
    return ids, total


def comparison(folder, manifest, requirements, from_bound):
    merged, totals = {}, {}
    for tech in TECHS:
        source = bound(folder, f'{tech}-evidence.json').parent if from_bound else folder
        ids, total = analysis(source, tech, manifest)
        need(not set(merged).intersection(ids), '不同技术的证据ID不能重复')
        merged.update({fid: tech for fid in ids})
        totals[tech] = total
    data = read_json(folder / 'database-comparison.json')
    need(data['schema_version'] == 1, '对比schema错误')
    need(set(data['input_sha256']) == set(ANALYSES), '对比输入清单不完整')
    for name in ANALYSES:
        source = bound(folder, name) if from_bound else folder / name
        need(data['input_sha256'][name] == sha(source), f'对比未引用实际上游版本: {name}')
    criteria = data['criteria']
    wanted = {r['id'] for r in requirements['requirements']}
    need(len(criteria) == len(wanted) and {c['requirement_id'] for c in criteria} == wanted, '需求映射缺失/重复')
    for criterion in criteria:
        need(text(criterion['assessment'], 40), '对比缺分析说明')
        for tech in TECHS:
            refs = criterion['evidence'][tech]
            need(isinstance(refs, list) and refs and all(merged.get(r) == tech for r in refs), '对比引用错误技术/未知证据')
    need(set(data['annual_cost_cny']) == set(TECHS), '成本对比缺技术')
    for tech in TECHS:
        need(money(data['annual_cost_cny'][tech]) == totals[tech] * 12, '年度成本换算错误')
    recommendation = data['recommendation']
    need(recommendation['technology'] in TECHS and text(recommendation['rationale'], 80), '缺推荐和论证')
    need(recommendation['status'] == 'provisional', '未经独立复核只能给暂定推荐')
    citations(folder / 'database-comparison.md', set(merged), set(merged))
    return set(merged)


def verify(stage, folder, manifest):
    folder = Path(folder)
    if stage == 'import':
        imports(folder, manifest, direct=True)
        return
    if stage in TECHS:
        imports(folder, manifest)
        analysis(folder, stage, manifest)
        return
    requirements = imports(folder, manifest, direct=stage == 'root')
    if stage == 'comparison':
        comparison(folder, manifest, requirements, from_bound=True)
        return
    need(stage in ('research', 'root'), '未知验收阶段')
    for name in RESEARCH:
        need((folder / name).is_file(), f'缺交付: {name}')
        need(sha(folder / name) == sha(bound(folder, name)), f'整合篡改上游: {name}')
    ids = comparison(folder, manifest, requirements, from_bound=False)
    if stage == 'root':
        for name in IMPORTS:
            need(sha(folder / name) == sha(bound(folder, name)), f'根未保留导入来源: {name}')
        report = citations(folder / '99-数据库选型报告.md', ids, ids)
        for heading in ('执行摘要', '需求与决策', '成本与风险', '实施步骤', '验证边界'):
            need(re.search(r'^## ' + heading + r'\s*$', report, re.M), f'最终报告缺章节: {heading}')
        review = read_json(folder / 'review.json')
        need(review == {'machine_scope': 'structure_consistency_traceability',
                        'content_review': 'pending', 'mode': manifest['mode']}, '不得冒充人工/独立事实签收')
        if manifest['mode'] == 'zero-model':
            need('合成测试数据，不用于技术决策' in report, '最终报告缺测试标记')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=('import', *TECHS, 'comparison', 'research', 'root'), required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = read_json(args.manifest)
        need(manifest['mode'] in ('zero-model', 'real'), 'manifest模式非法')
        need(set(manifest['imports']) == set(IMPORTS), 'manifest输入清单非法')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'考卷配置错误: {exc}')
        return 2
    try:
        verify(args.stage, Path.cwd(), manifest)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, InvalidOperation) as exc:
        print(f'FAIL: {exc}')
        return 1
    print(f'PASS {args.stage}: 结构、一致性和追溯检查；事实复核仍待完成')
    return 0


if __name__ == '__main__':
    sys.exit(main())
