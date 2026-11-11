"""Task-local templates registered only in the runner's process."""
from dataclasses import dataclass
from pathlib import Path
from dag_agent.autogrow import SPLIT_TEMPLATES, TemplateViolation
from techstack_research_v2.checks import ANALYSES, IMPORTS, RESEARCH, ROOT_FILES

IDS = {stage: f'db-smoke-{stage}-v1' for stage in ('postgresql', 'clickhouse', 'comparison')}


def artifacts(stage):
    if stage == 'import':
        return IMPORTS
    if stage == 'root':
        return ROOT_FILES
    if stage == 'research':
        return RESEARCH
    if stage == 'comparison':
        return ('database-comparison.md', 'database-comparison.json')
    return tuple(name for name in ANALYSES if name.startswith(stage + '-'))


def command(checker, manifest, stage):
    return f'python "{Path(checker).as_posix()}" --stage {stage} --manifest "{Path(manifest).as_posix()}"'


def brief(stage, cmd):
    task = {
        'root': '整合全部已验收研究；原样转交上游JSON/正文及导入文件，另写最终报告和review.json。',
        'import': '平台运行前导入的冻结输入；来源为host_import，不是模型研究产出。',
        'research': '读取绑定的proposal-protocol.md和deliverable-schema.md。评估拆分为两种数据库分析与比较；决定拆分时只写.dag/proposal.json，正常结束会话，不写正式交付。整合会话不得再提案，原样汇集6份子交付。',
        'comparison': '读取绑定的两份技术分析和证据，按需求逐项比较，引用真实上游证据ID和文件SHA256，重算年度成本。',
        'postgresql': '研究PostgreSQL并交付正文与结构化证据。不得用合成数据冒充实际来源，无法核实的事实明确列限制。',
        'clickhouse': '研究ClickHouse并交付正文与结构化证据。不得用合成数据冒充实际来源，无法核实的事实明确列限制。',
    }[stage]
    return (f'【输入】inputs/<来源节点>/中的冻结输入和依赖产物；查找requirements.json、proposal-protocol.md、deliverable-schema.md。\n'
            f'【交付清单】{", ".join(artifacts(stage))}\n'
            '【接口签名】UTF-8 Markdown和JSON，遵守绑定的deliverable-schema.md；只写当前目录。\n'
            '【样例】证据引用[@pg-capability]；成本quantity×unit_price=monthly_total，数值为十进制字符串。\n'
            f'【自测入口】{cmd}\n【任务】{task}')


@dataclass
class ResearchTemplate:
    stage: str
    checker: Path
    manifest: Path

    def generate(self, child, parent_title):
        if child.get('template_params') not in (None, {}):
            raise TemplateViolation('数据库冒烟模板不接受自定义参数或考卷')
        deps = list(child.get('depends_on', []))
        if self.stage != 'comparison' and deps:
            raise TemplateViolation('两个分析分支应独立执行')
        if self.stage == 'comparison' and (len(deps) != 2 or len(set(deps)) != 2):
            raise TemplateViolation('比较必须依赖两个不同分析分支')
        cmd = command(self.checker, self.manifest, self.stage)
        return {'title': child['title'], 'statement': brief(self.stage, cmd),
                'check_cmd': cmd, 'acceptance': None,
                'exam_files': [str(self.checker), str(self.manifest)],
                'declared_artifacts': list(artifacts(self.stage)), 'priority': 0,
                'integration_check': None, 'exploratory': True,
                'coverage_hint': '结构、一致性、引用与算式；事实充分性待独立复核',
                'source_local_id': child['local_id'], 'deps': deps}


def register(checker, manifest):
    old = {tid: SPLIT_TEMPLATES.get(tid) for tid in IDS.values()}
    for stage, tid in IDS.items():
        SPLIT_TEMPLATES[tid] = ResearchTemplate(stage, checker, manifest)
    return old


def restore(old):
    for tid, template in old.items():
        if template is None:
            SPLIT_TEMPLATES.pop(tid, None)
        else:
            SPLIT_TEMPLATES[tid] = template
