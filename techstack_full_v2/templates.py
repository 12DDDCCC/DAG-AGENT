# -*- coding: utf-8 -*-
"""50节点任务业务模板：注册于驱动进程，安全结束后 restore。

四个 template_id（structure.TEMPLATE_IDS），每个模板支持两种形态：
  整合型节点 spec（无 leaf 参数）——节点先提案拆分、后整合交付；
  叶子节点 spec（leaf 参数）——直接研究交付，不再拆分。
执行者只能选注册 template_id + 有限业务参数；考卷/交付清单由模板定。
批结构校验（validate_batch）在 scheduler 出题前整批执行，偏离任务书
固定结构（50 节点 / 12 拆分）的提案整批拒绝。
"""
from __future__ import annotations
from pathlib import Path

from dag_agent.autogrow import SPLIT_TEMPLATES, TemplateViolation

STRUCTURE_PY = Path(__file__).resolve().parent / 'structure.py'


def exam_pair(checker, manifest):
    """考卷文件 = 检查器 + manifest + structure（检查器的同目录依赖）。"""
    return [str(Path(checker)), str(Path(manifest)), str(STRUCTURE_PY)]
from techstack_full_v2.structure import (ASPECTS, ASPECT_TITLE, CROSS_STUDIES,
                                         DOMAINS, DOMAIN_TECHS, TECHS,
                                         TPL_CROSS, TPL_DOMAIN, TPL_DOMAINS,
                                         TPL_TECH, cross_artifacts,
                                         cross_study_artifacts,
                                         cross_split_children,
                                         domain_artifacts,
                                         domain_compare_artifacts,
                                         domain_split_children,
                                         domains_split_children,
                                         tech_artifacts, tech_split_children)

IDS = (TPL_DOMAINS, TPL_DOMAIN, TPL_TECH, TPL_CROSS)


def command(checker, manifest, stage, trusted_db=None):
    """考卷命令。trusted_db（平台任务库路径）→ 附 --trusted-db：root
    转交文件按可信上游清单核对（2026-11-08 缺陷修复）；其余 stage 忽略。"""
    cmd = (f'python "{Path(checker).as_posix()}" --stage {stage} '
           f'--manifest "{Path(manifest).as_posix()}"')
    if trusted_db is not None:
        cmd += f' --trusted-db "{Path(trusted_db).as_posix()}"'
    return cmd


def _inputs_line():
    return ('【输入】inputs/<来源节点>/ 中的冻结输入与上游交付'
            '（requirements.json、proposal-protocol.md、deliverable-schema.md 必读）；'
            'inputs 只读，不得改写。')


def _net_line():
    return ('【来源】事实必须有真实一手来源：用 Bash 执行 curl -sL "<URL>" -o sources/<证据ID>.html '
            '保存原文快照并记录获取时间；无法访问或无法核实的条目在 limitations 如实声明，禁止编造。')


def brief_research_domain(domain):
    techs = '、'.join(DOMAIN_TECHS[domain])
    return (f'{_inputs_line()}\n'
            f'【角色】领域整合节点（{domain}）。两种情形二选一：\n'
            f'（a）inputs 下尚无子交付快照 → 按协议评估拆分：只写 .dag/proposal.json'
            f'（template_id=full-domain-v1，template_params={{"domain":"{domain}"}}），'
            f'children 恰为 {techs} 各技术研究（template_id=full-tech-v1，params {{"technology":<tech>}}）'
            f'加一个 {domain}-compare 比较叶子（params {{"domain":"{domain}","leaf":"compare"}}，'
            f'deps=全部技术 local_id），正常结束会话，不生成本节点交付。\n'
            f'（b）inputs 下已存在全部子交付快照 → 整合：原样转交全部上游交付到当前目录'
            f'（文件名与SHA256见子交付，不得改写），另写 {domain}-integration.md/json，'
            f'不再提案。\n'
            f'【交付清单】{", ".join(domain_artifacts(domain))}\n'
            f'【自测入口】--stage {domain}-integration')


def brief_research_tech(tech):
    return (f'{_inputs_line()}\n'
            f'【角色】技术整合节点（{tech}）。两种情形二选一：\n'
            f'（a）inputs 下尚无子交付快照 → 按协议评估拆分：只写 .dag/proposal.json'
            f'（template_id=full-tech-v1，template_params={{"technology":"{tech}"}}），'
            f'children 恰为 {tech}-feature/{tech}-performance/{tech}-case/{tech}-cost '
            f'四个深度叶子（params {{"technology":"{tech}","leaf":<aspect>}}，deps 全空可并行），'
            f'正常结束会话，不生成本节点交付。\n'
            f'（b）inputs 下已存在全部子交付快照 → 整合：原样转交四份深度交付到当前目录'
            f'（不得改写），另写 {tech}-integration.md/json（upstream 含四份交付的真实SHA256），'
            f'不再提案。\n'
            f'【交付清单】{", ".join(tech_artifacts(tech))}\n'
            f'【自测入口】--stage {tech}-integration')


def brief_cross():
    return (f'{_inputs_line()}\n'
            f'【角色】跨领域整合节点。两种情形二选一：\n'
            f'（a）inputs 下尚无子交付快照 → 按协议评估拆分：只写 .dag/proposal.json'
            f'（template_id=full-cross-v1，template_params={{}}），children 恰为 '
            f'{"、".join(CROSS_STUDIES)} 五个研究叶子（params {{"leaf":<name>}}，deps 全空可并行），'
            f'正常结束会话，不生成本节点交付。\n'
            f'（b）inputs 下已存在全部子交付快照 → 整合：原样转交五份研究交付，'
            f'另写 cross-integration.md/json（upstream 含五份交付真实SHA256），不再提案。\n'
            f'【交付清单】{", ".join(cross_artifacts())}\n'
            f'【自测入口】--stage cross-integration')


def brief_research_root_split():
    return (f'{_inputs_line()}\n'
            f'【角色】总研究节点。inputs 下尚无子交付快照 → 按协议评估拆分：只写 '
            f'.dag/proposal.json（template_id=full-domains-v1，template_params={{}}），'
            f'children 恰为 database/message-queue/cache 三个领域节点'
            f'（template_id=full-domain-v1，params {{"domain":<同名>}}）与 cross'
            f'（template_id=full-cross-v1，params {{}}，deps=["database","message-queue","cache"]），'
            f'正常结束会话，不生成本节点交付。')


def brief_aspect(tech, aspect):
    t = ASPECT_TITLE[aspect]
    return (f'{_inputs_line()}\n{_net_line()}\n'
            f'【角色】叶子研究节点（{tech} 的{t}），不拆分、不写提案，直接研究并交付。\n'
            f'【交付清单】{tech}-{aspect}.md 与 {tech}-{aspect}-evidence.json（契约见绑定的'
            f'deliverable-schema.md；证据ID建议前缀 {tech[:3]}{aspect[0]}-）。\n'
            f'【自测入口】--stage {tech}-{aspect}')


def brief_compare(domain):
    techs = '、'.join(DOMAIN_TECHS[domain])
    return (f'{_inputs_line()}\n'
            f'【角色】叶子比较节点（{domain} 领域），不拆分、不写提案，直接交付。'
            f'基于 inputs 快照中 {techs} 的技术整合交付（含其上游证据）逐项比较'
            f'需求覆盖与成本，input_sha256 记录各技术 integration.json 真实SHA256，'
            f'引用上游真实证据ID。\n'
            f'【交付清单】{domain}-comparison.md 与 {domain}-comparison.json\n'
            f'【自测入口】--stage {domain}-compare')


def brief_cross_study(name):
    t = ASPECT_TITLE[name]
    return (f'{_inputs_line()}\n{_net_line()}\n'
            f'【角色】叶子研究节点（跨领域：{t}），不拆分、不写提案，直接交付。'
            f'基于 inputs 快照中三个领域的全部上游证据开展研究；domain_refs 须引用'
            f'三领域真实证据ID（每领域至少1条）。'
            + ('须给出7技术月度成本合计与年度换算。' if name == 'total-cost' else '')
            + f'\n【交付清单】cross-{name}.md 与 cross-{name}-evidence.json\n'
            f'【自测入口】--stage {name}')


def brief_domain_integration(domain):
    return (f'{_inputs_line()}\n'
            f'【角色】领域整合会话（{domain}）：inputs 下已存在全部子交付快照，'
            f'原样转交全部上游交付到当前目录（不得改写），另写 {domain}-integration.md/json，'
            f'不再提案。\n【交付清单】{", ".join(domain_artifacts(domain))}\n'
            f'【自测入口】--stage {domain}-integration')


# ---- 批结构校验（整批孩子集合断言；偏离任务书固定结构即整批拒绝） ----



SIG = ('【接口签名】UTF-8 Markdown 与 JSON，遵守绑定的 deliverable-schema.md；'
       '只写当前目录，不改动 inputs/。\n')
SAMPLE = ('【样例】证据引用格式 [@证据ID]；成本 quantity×unit_price=monthly_total '
          '（十进制字符串，月度/CNY）；整合 upstream 为 {"文件名":"SHA256"}。\n')


def _finalize(stmt):
    """派发硬闸门五要素：补【接口签名】【样例】段（幂等）。"""
    nl = chr(10)
    if '【接口签名】' not in stmt:
        stmt = stmt.replace(nl + '【交付清单】', nl + SIG + SAMPLE + '【交付清单】', 1)
        stmt = stmt.replace(nl + '【角色】', nl + SIG + SAMPLE + '【角色】', 1)
    return stmt

def validate_batch_structure(children, parent_title=''):
    from dag_agent.autogrow import ProposalError
    if not isinstance(children, list) or not children:
        raise TemplateViolation('批结构校验：children 为空')
    lids = [c.get('local_id') for c in children]

    def fail(why):
        raise TemplateViolation(f'批结构偏离任务书固定结构：{why}')

    # 形态1：research 批（4 领域/跨领域孩子）
    if set(lids) == {'database', 'message-queue', 'cache', 'cross'}:
        if len(children) != 4:
            fail('research 批必须恰好4孩子')
        expected = {c['local_id']: c for c in domains_split_children()}
        for c in children:
            e = expected[c['local_id']]
            if c.get('template_id') != e['template_id'] or \
                    c.get('template_params') != e['template_params']:
                fail(f'{c["local_id"]} 模板/参数不符')
            if c.get('depends_on') != e['depends_on']:
                fail(f'{c["local_id"]} deps 不符（cross 必须依赖三领域）')
        return
    # 形态2：技术深度批（4 aspect 叶子）
    if all(isinstance(l, str) and l.rsplit('-', 1)[-1] in ASPECTS and
           l.rsplit('-', 1)[0] in TECHS for l in lids):
        if len(children) != 4 or len(set(lids)) != 4:
            fail('技术批必须恰好4个不同 aspect 叶子')
        tech = lids[0].rsplit('-', 1)[0]
        for c in children:
            if c.get('template_id') != TPL_TECH or \
                    c.get('template_params') != {'technology': tech,
                                                 'leaf': c['local_id'].rsplit('-', 1)[-1]}:
                fail(f'{c["local_id"]} 模板/参数不符')
            if c.get('depends_on'):
                fail('深度叶子 deps 必须为空（独立并行研究）')
        return
    # 形态3：跨领域批（5 研究叶子）
    if set(lids) == set(CROSS_STUDIES):
        if len(children) != 5:
            fail('跨领域批必须恰好5孩子')
        for c in children:
            if c.get('template_id') != TPL_CROSS or \
                    c.get('template_params') != {'leaf': c['local_id']}:
                fail(f'{c["local_id"]} 模板/参数不符')
            if c.get('depends_on'):
                fail('跨领域研究叶子 deps 必须为空')
        return
    # 形态4：领域批（n 技术整合孩子 + 1 比较叶子）
    compare = [c for c in children if str(c.get('local_id', '')).endswith('-compare')]
    techs = [c for c in children if c not in compare]
    if len(compare) == 1 and techs:
        dom = None
        for d, ts in DOMAIN_TECHS.items():
            if {c.get('local_id') for c in techs} == set(ts):
                dom = d
        if dom is None:
            fail(f'技术孩子集合不是任何领域的技术集：{lids}')
        c0 = compare[0]
        if c0.get('local_id') != f'{dom}-compare' or \
                c0.get('template_id') != TPL_DOMAIN or \
                c0.get('template_params') != {'domain': dom, 'leaf': 'compare'} or \
                sorted(c0.get('depends_on') or []) != sorted(DOMAIN_TECHS[dom]):
            fail(f'{c0.get("local_id")} 比较叶子模板/参数/deps 不符')
        for c in techs:
            if c.get('template_id') != TPL_TECH or \
                    c.get('template_params') != {'technology': c['local_id']}:
                fail(f'{c["local_id"]} 技术孩子模板/参数不符')
            if c.get('depends_on'):
                fail('技术孩子 deps 必须为空')
        return
    fail(f'无法识别的批结构：{lids}')


class _FullTemplate:
    """公共实现：generate 按 child 参数分支产出 spec；批校验共用。"""

    def validate_batch(self, children, parent_title=''):
        validate_batch_structure(children, parent_title)


class DomainsTemplate(_FullTemplate):
    """research 的 4 领域孩子（整合型 spec：先拆分后整合）。"""

    def __init__(self, checker, manifest):
        self.checker, self.manifest = Path(checker), Path(manifest)
        self.exams = exam_pair(self.checker, self.manifest)
        self.cmd = lambda stage: command(self.checker, self.manifest, stage)

    def generate(self, child, parent_title):
        d = child['local_id']
        expected = {c['local_id']: c for c in domains_split_children()}
        e = expected.get(d)
        if e is None or child.get('template_id') != e['template_id'] or \
                child.get('template_params') != e['template_params']:
            raise TemplateViolation(f'{d}: 领域孩子 spec 与固定结构不符')
        if d == 'cross':
            stmt, stage, arts = brief_cross(), 'cross-integration', cross_artifacts()
        else:
            stmt, stage, arts = (brief_research_domain(d), f'{d}-integration',
                                 domain_artifacts(d))
        return {'title': child['title'], 'statement': _finalize(stmt),
                'check_cmd': self.cmd(stage), 'acceptance': None,
                'exam_files': self.exams,
                'declared_artifacts': list(arts), 'priority': 0,
                'integration_check': None, 'exploratory': True,
                'coverage_hint': '结构/一致性/追溯；事实充分性待独立复核',
                'source_local_id': child['local_id'], 'deps': child.get('depends_on') or []}


class DomainTemplate(_FullTemplate):
    """领域拆分批里的两类孩子：技术整合节点 + 比较叶子。"""

    def __init__(self, checker, manifest):
        self.checker, self.manifest = Path(checker), Path(manifest)
        self.exams = exam_pair(self.checker, self.manifest)
        self.cmd = lambda stage: command(self.checker, self.manifest, stage)

    def generate(self, child, parent_title):
        params = child.get('template_params') or {}
        if params.get('leaf') == 'compare':
            d = params.get('domain')
            if d not in DOMAINS or child['local_id'] != f'{d}-compare':
                raise TemplateViolation('比较叶子参数与 local_id 不符')
            if child.get('depends_on') != list(DOMAIN_TECHS[d]):
                raise TemplateViolation('比较叶子 deps 必须为本领域全部技术')
            return {'title': child['title'],
                    'statement': _finalize(brief_compare(d)),
                    'check_cmd': self.cmd(f'{d}-compare'), 'acceptance': None,
                    'exam_files': self.exams,
                    'declared_artifacts': list(domain_compare_artifacts(d)),
                    'priority': 0, 'integration_check': None, 'exploratory': True,
                    'coverage_hint': '逐项比较/成本换算/追溯；事实待独立复核',
                    'source_local_id': child['local_id'],
                    'deps': child.get('depends_on') or []}
        d = params.get('domain')
        if d not in DOMAINS or child['local_id'] != d:
            raise TemplateViolation(f'领域整合孩子参数不符: {child.get("local_id")}')
        return {'title': child['title'],
                'statement': _finalize(brief_research_domain(d)),
                'check_cmd': self.cmd(f'{d}-integration'), 'acceptance': None,
                'exam_files': self.exams,
                'declared_artifacts': list(domain_artifacts(d)),
                'priority': 0, 'integration_check': None, 'exploratory': True,
                'coverage_hint': '结构/一致性/追溯；事实待独立复核',
                'source_local_id': child['local_id'],
                'deps': child.get('depends_on') or []}


class TechTemplate(_FullTemplate):
    """技术整合节点（领域批中）与深度叶子（技术批中）。"""

    def __init__(self, checker, manifest):
        self.checker, self.manifest = Path(checker), Path(manifest)
        self.exams = exam_pair(self.checker, self.manifest)
        self.cmd = lambda stage: command(self.checker, self.manifest, stage)

    def generate(self, child, parent_title):
        params = child.get('template_params') or {}
        tech = params.get('technology')
        if tech not in TECHS:
            raise TemplateViolation(f'technology 参数非法: {tech!r}')
        if 'leaf' in params:
            aspect = params['leaf']
            if aspect not in ASPECTS or child['local_id'] != f'{tech}-{aspect}':
                raise TemplateViolation('深度叶子参数与 local_id 不符')
            if child.get('depends_on'):
                raise TemplateViolation('深度叶子 deps 必须为空')
            arts = (f'{tech}-{aspect}.md', f'{tech}-{aspect}-evidence.json')
            return {'title': child['title'], 'statement': _finalize(brief_aspect(tech, aspect)),
                    'check_cmd': self.cmd(f'{tech}-{aspect}'), 'acceptance': None,
                    'exam_files': self.exams, 'declared_artifacts': list(arts),
                    'priority': 0, 'integration_check': None, 'exploratory': True,
                    'coverage_hint': '证据可溯源/条件完整；事实待独立复核',
                    'source_local_id': child['local_id'],
                    'deps': child.get('depends_on') or []}
        if child['local_id'] != tech:
            raise TemplateViolation(f'技术整合孩子 local_id 不符: {child["local_id"]}')
        return {'title': child['title'], 'statement': _finalize(brief_research_tech(tech)),
                'check_cmd': self.cmd(f'{tech}-integration'), 'acceptance': None,
                'exam_files': self.exams,
                'declared_artifacts': list(tech_artifacts(tech)),
                'priority': 0, 'integration_check': None, 'exploratory': True,
                'coverage_hint': '结构/一致性/追溯；事实待独立复核',
                'source_local_id': child['local_id'],
                'deps': child.get('depends_on') or []}


class CrossTemplate(_FullTemplate):
    """跨领域整合节点（research 批中）与研究叶子（跨领域批中）。"""

    def __init__(self, checker, manifest):
        self.checker, self.manifest = Path(checker), Path(manifest)
        self.exams = exam_pair(self.checker, self.manifest)
        self.cmd = lambda stage: command(self.checker, self.manifest, stage)

    def generate(self, child, parent_title):
        params = child.get('template_params') or {}
        if 'leaf' in params:
            name = params['leaf']
            if name not in CROSS_STUDIES or child['local_id'] != name:
                raise TemplateViolation('跨领域研究叶子参数与 local_id 不符')
            if child.get('depends_on'):
                raise TemplateViolation('跨领域研究叶子 deps 必须为空')
            arts = cross_study_artifacts(name)
            return {'title': child['title'], 'statement': _finalize(brief_cross_study(name)),
                    'check_cmd': self.cmd(name), 'acceptance': None,
                    'exam_files': self.exams, 'declared_artifacts': list(arts),
                    'priority': 0, 'integration_check': None, 'exploratory': True,
                    'coverage_hint': '跨领域证据引用/成本换算；事实待独立复核',
                    'source_local_id': child['local_id'],
                    'deps': child.get('depends_on') or []}
        if child['local_id'] != 'cross':
            raise TemplateViolation(f'跨领域整合孩子 local_id 不符: {child["local_id"]}')
        return {'title': child['title'], 'statement': _finalize(brief_cross()),
                'check_cmd': self.cmd('cross-integration'), 'acceptance': None,
                'exam_files': self.exams,
                'declared_artifacts': list(cross_artifacts()),
                'priority': 0, 'integration_check': None, 'exploratory': True,
                'coverage_hint': '结构/一致性/追溯；事实待独立复核',
                'source_local_id': child['local_id'],
                'deps': child.get('depends_on') or []}


def register(checker, manifest):
    old = {tid: SPLIT_TEMPLATES.get(tid) for tid in IDS}
    SPLIT_TEMPLATES[TPL_DOMAINS] = DomainsTemplate(checker, manifest)
    SPLIT_TEMPLATES[TPL_DOMAIN] = DomainTemplate(checker, manifest)
    SPLIT_TEMPLATES[TPL_TECH] = TechTemplate(checker, manifest)
    SPLIT_TEMPLATES[TPL_CROSS] = CrossTemplate(checker, manifest)
    return old


def restore(old):
    for tid, tpl in old.items():
        if tpl is None:
            SPLIT_TEMPLATES.pop(tid, None)
        else:
            SPLIT_TEMPLATES[tid] = tpl
