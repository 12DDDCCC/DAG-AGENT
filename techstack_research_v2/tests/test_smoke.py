import json
from pathlib import Path
import re
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from dag_agent.autogrow import SPLIT_TEMPLATES, ProposalError, TemplateViolation, parse_proposal
from techstack_research_v2.checks import ANALYSES, IMPORTS, RESEARCH, Invalid, read_json, verify
from techstack_research_v2.fixture_backend import collect, make_analysis, make_comparison, make_root, write_json
from techstack_research_v2.run_smoke import prepare, run
from techstack_research_v2.templates import IDS, ResearchTemplate


@pytest.fixture(scope='module')
def frozen(tmp_path_factory):
    home = tmp_path_factory.mktemp('frozen')
    checker, manifest, inputs, _ = prepare(home, 'zero-model')
    return checker, manifest, inputs


@pytest.fixture
def samples(tmp_path, frozen):
    checker, manifest_path, imported = frozen
    manifest = read_json(manifest_path)
    analysis = tmp_path / 'analysis'
    comparison = tmp_path / 'comparison'
    research = tmp_path / 'research'
    root = tmp_path / 'root'
    for folder in (analysis, comparison, research, root):
        shutil.copytree(imported, folder / 'inputs/n-import')
    for tech in ('postgresql', 'clickhouse'):
        make_analysis(analysis, tech)
        for folder in (comparison, research):
            dest = folder / f'inputs/n-{tech}'
            dest.mkdir()
            for name in ANALYSES:
                if name.startswith(tech):
                    shutil.copyfile(analysis / name, dest / name)
    make_comparison(comparison)
    dest = research / 'inputs/n-comparison'
    dest.mkdir()
    for name in RESEARCH[-2:]:
        shutil.copyfile(comparison / name, dest / name)
    collect(research, RESEARCH)
    dest = root / 'inputs/n-research'
    dest.mkdir()
    for name in RESEARCH:
        shutil.copyfile(research / name, dest / name)
    make_root(root)
    return manifest, {'import': imported, 'postgresql': analysis, 'clickhouse': analysis,
                      'comparison': comparison, 'research': research, 'root': root}


def test_positive_all_contract_stages(samples):
    manifest, folders = samples
    for stage, folder in folders.items():
        verify(stage, folder, manifest)


@pytest.mark.parametrize('mutation', ['missing_source', 'duplicate_id', 'wrong_unit', 'wrong_cost',
                                     'unknown_cost_source', 'self_review', 'nonfinite', 'synthetic_as_real'])
def test_evidence_rejects_mutations(samples, mutation):
    manifest, folders = samples
    folder = folders['postgresql']
    path = folder / 'postgresql-evidence.json'
    data = read_json(path)
    if mutation == 'missing_source':
        data['facts'][0]['source']['url'] = '官方 文档'
    elif mutation == 'duplicate_id':
        data['facts'][1]['id'] = data['facts'][0]['id']
    elif mutation == 'wrong_unit':
        data['facts'][1]['metric']['unit'] = '很快'
    elif mutation == 'wrong_cost':
        data['costs'][0]['monthly_total'] = '361.00'
    elif mutation == 'unknown_cost_source':
        data['costs'][0]['source_id'] = 'fabricated-id'
    elif mutation == 'self_review':
        data['review_status'] = 'verified'
    elif mutation == 'nonfinite':
        data['costs'][0]['quantity'] = 'NaN'
    else:
        data['mode'] = 'real'
        manifest = {**manifest, 'mode': 'real'}
    write_json(path, data)
    with pytest.raises(Invalid):
        verify('postgresql', folder, manifest)


@pytest.mark.parametrize('mutation', ['hash', 'annual_cost', 'unknown_reference', 'missing_requirement'])
def test_comparison_rejects_mutations(samples, mutation):
    manifest, folders = samples
    folder = folders['comparison']
    path = folder / 'database-comparison.json'
    data = read_json(path)
    if mutation == 'hash':
        data['input_sha256']['postgresql-evidence.json'] = '0' * 64
    elif mutation == 'annual_cost':
        data['annual_cost_cny']['postgresql'] = '1'
    elif mutation == 'unknown_reference':
        data['criteria'][0]['evidence']['postgresql'] = ['ch-capability']
    else:
        data['criteria'].pop()
    write_json(path, data)
    with pytest.raises(Invalid):
        verify('comparison', folder, manifest)


@pytest.mark.parametrize('mutation', ['input_tamper', 'integration_tamper', 'self_review', 'fake_heading', 'unknown_citation'])
def test_root_rejects_mutations(samples, mutation):
    manifest, folders = samples
    folder = folders['root']
    report = folder / '99-数据库选型报告.md'
    if mutation == 'input_tamper':
        (folder / 'requirements.json').write_text('{}', encoding='utf-8')
    elif mutation == 'integration_tamper':
        path = folder / 'postgresql-analysis.md'
        path.write_text(path.read_text(encoding='utf-8') + 'altered', encoding='utf-8')
    elif mutation == 'self_review':
        path = folder / 'review.json'
        data = read_json(path)
        data['content_review'] = 'approved'
        write_json(path, data)
    elif mutation == 'fake_heading':
        report.write_text(report.read_text(encoding='utf-8').replace('## ', ''), encoding='utf-8')
    else:
        report.write_text(report.read_text(encoding='utf-8') + '[@nonexistent]', encoding='utf-8')
    with pytest.raises(Invalid):
        verify('root', folder, manifest)


def test_old_filler_cannot_pass_even_with_valid_json(samples):
    manifest, folders = samples
    filler = ('技术概述 核心特性 优势 劣势 适用场景 官方 文档 1ms 2TPS 3QPS\n'
              + '- 这里没有任何真实技术事实描述\n' * 5 + '无实际研究。' * 700)
    for stage, filename in [('postgresql', 'postgresql-analysis.md'), ('root', '99-数据库选型报告.md')]:
        (folders[stage] / filename).write_text(filler, encoding='utf-8')
        with pytest.raises(Invalid):
            verify(stage, folders[stage], manifest)


def test_published_protocol_and_old_wrong_fields(frozen):
    _, _, imported = frozen
    raw = re.search(r'```json\s*(.*?)```', (imported / 'proposal-protocol.md').read_text(encoding='utf-8-sig'), re.S).group(1)
    parsed = parse_proposal(raw.encode(), {}, 'n1', 'a1')
    assert len(parsed.children) == 3
    invalid = json.loads(raw)
    invalid['children'][0]['check_cmd'] = 'executor-selected-exam'
    with pytest.raises(ProposalError):
        parse_proposal(json.dumps(invalid).encode(), {}, 'n1', 'a1')


def test_template_rejects_exam_parameters(frozen):
    checker, manifest, _ = frozen
    with pytest.raises(TemplateViolation):
        ResearchTemplate('postgresql', checker, manifest).generate(
            {'template_params': {'check_cmd': 'always-pass'}}, 'parent')
    with pytest.raises(TemplateViolation):
        ResearchTemplate('comparison', checker, manifest).generate(
            {'template_params': {}, 'depends_on': []}, 'parent')


def test_six_node_zero_model_chain(tmp_path):
    before = {name: SPLIT_TEMPLATES.get(name) for name in IDS.values()}
    result = run(tmp_path / 'run', timeout=60)
    assert result['success'], result
    assert all(result['checks'].values())
    assert result['mode'] == 'zero-model' and result['content_review'] == 'pending'
    assert result['analysis_execution_overlap']
    nodes = result['nodes']
    assert len(nodes[-1]['deps']) == 3  # imported input + two analyses
    assert {name: SPLIT_TEMPLATES.get(name) for name in IDS.values()} == before
    assert read_json(tmp_path / 'run/run_results.json')['success']


def test_real_unavailable_never_falls_back_to_fixture(tmp_path, monkeypatch):
    class Unavailable:
        def available(self):
            return False, 'test backend unavailable'
    monkeypatch.setattr('dag_agent.backends.create_backend', lambda *a: Unavailable())
    result = run(tmp_path / 'run', mode='real')
    assert not result['success'] and '预检未通过' in result['error']
    assert not (tmp_path / 'run/mission.db').exists()


def test_existing_output_preserved(tmp_path):
    marker = tmp_path / 'keep.txt'
    marker.write_text('user data', encoding='utf-8')
    with pytest.raises(ValueError):
        run(tmp_path)
    assert marker.read_text(encoding='utf-8') == 'user data'


def test_status_exception_stops_and_persists(tmp_path, monkeypatch):
    from dag_agent.daemon import RunManager
    def broken_status(self, mid):
        raise RuntimeError('injected status failure')
    monkeypatch.setattr(RunManager, 'status', broken_status)
    result = run(tmp_path / 'run', timeout=30)
    assert not result['success'] and 'injected status failure' in result['error']
    assert result['stop']['stopped']
    assert read_json(tmp_path / 'run/run_results.json')['error'] == result['error']
