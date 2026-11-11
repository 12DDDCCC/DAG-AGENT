# -*- coding: utf-8 -*-
"""50节点选型研究：离线合同检查器。

通过只证明结构/一致性/追溯合格，不证明研究事实为真（content_review 独立）。
考卷被根考卷与全部节点考卷引用；被 exam_files 冻结。零模型模式仅供独立
测试目录使用，真实任务工件禁止 zero-model 夹具（--stage 拒绝）。
"""
from __future__ import annotations
import argparse
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))  # frozen/ 同目录 structure.py
from structure import (ASPECTS, CROSS_STUDIES, DOMAINS,
                                         DOMAIN_TECHS, EXPECTED_NODES, TECHS,
                                         cross_artifacts, cross_study_artifacts,
                                         domain_artifacts,
                                         domain_compare_artifacts,
                                         root_expected_deliverables,
                                         tech_artifacts)

IMPORTS = ("requirements.json", "proposal-protocol.md",
           "deliverable-schema.md", "input-provenance.json")
ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{2,63}$')


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


def text(v, minimum=1):
    return isinstance(v, str) and len(v.strip()) >= minimum


def money(v):
    need(isinstance(v, str), '金额和数量必须使用十进制字符串')
    try:
        n = Decimal(v)
    except InvalidOperation as exc:
        raise Invalid('非法十进制数') from exc
    need(n.is_finite() and n >= 0, '数值必须有限且非负')
    return n


def bound(folder, name):
    """在 inputs/<来源>/ 中按文件名定位（多处出现必须同 sha）。"""
    cands = list(Path(folder).glob(f'inputs/*/{name}'))
    need(bool(cands), f'缺绑定输入: {name}')
    need(all(p.is_file() for p in cands), f'绑定输入不是文件: {name}')
    need(len({sha(p) for p in cands}) == 1, f'绑定输入版本有歧义: {name}')
    return cands[0]


def imports(folder, manifest, direct=False):
    for name in IMPORTS:
        p = Path(folder) / name if direct else bound(folder, name)
        need(p.is_file() and sha(p) == manifest['imports'][name], f'输入哈希不符: {name}')
    p = Path(folder) / 'requirements.json' if direct else bound(folder, 'requirements.json')
    return read_json(p)


def citations(path, allowed, required=None, min_len=200):
    need(Path(path).is_file(), f'缺正文: {Path(path).name}')
    content = Path(path).read_text(encoding='utf-8')
    need(len(content.strip()) >= min_len, f'正文为空或过短: {Path(path).name}')
    refs = set(re.findall(r'\[@([a-zA-Z0-9_-]+)\]', content))
    need(refs, f'正文无任何证据引用: {Path(path).name}')
    need(refs <= allowed, f'正文引用了未知证据ID: {Path(path).name}')
    if required is not None:
        need(required <= refs, f'正文未覆盖必要证据: {Path(path).name}')
    return content


def headings(content, names):
    for h in names:
        need(re.search(r'^## ' + re.escape(h) + r'\s*$', content, re.M),
             f'缺实际二级标题: {h}')


def zero_mark(content, manifest):
    """零模型正文必须带合成标记（独立测试目录专用，真实任务禁止触发）。"""
    if manifest['mode'] == 'zero-model':
        need('合成测试数据，不用于技术决策' in content, '零模型正文缺合成标记')
    return content


# ---- 证据文件（facts/costs/来源/度量） ----

def evidence_file(folder, subject, manifest, from_bound=True,
                  require_domain_refs=False, json_name=None):
    name = json_name or f'{subject}-evidence.json'
    p = bound(folder, name) if from_bound else Path(folder) / name
    need(p.is_file(), f'缺证据文件: {name}')
    data = read_json(p)
    need(data['schema_version'] == 1 and data['subject'] == subject, f'{name}: subject/schema不符')
    need(data['mode'] == manifest['mode'], f'{name}: mode不符（禁止夹具冒充）')
    need(data['review_status'] == 'unverified', f'{name}: 执行者不得自行签收复核')
    facts = data['facts']
    need(isinstance(facts, list) and len(facts) >= 3, f'{name}: facts至少3条')
    ids = set()
    for f in facts:
        fid = f['id']
        need(isinstance(fid, str) and ID_RE.fullmatch(fid), f'{name}: 证据ID非法 {fid!r}')
        need(fid not in ids, f'{name}: 证据ID重复 {fid}')
        ids.add(fid)
        need(f['kind'] in ('capability', 'benchmark', 'case'), f'{name}: 证据类型非法')
        need(text(f['claim'], 30), f'{name}: 主张过短 {fid}')
        src = f['source']
        u = urlparse(src['url'])
        need(u.scheme == 'https' and bool(u.hostname), f'{name}: 来源须HTTPS {fid}')
        if manifest['mode'] == 'zero-model':
            need((u.hostname or '').endswith('.invalid') and f['basis'] == 'synthetic',
                 f'{name}: 零模型证据必须synthetic/.invalid {fid}')
        else:
            need('.' in (u.hostname or '') and
                 not (u.hostname or '').endswith(('.invalid', '.localhost')) and
                 u.hostname not in ('localhost', '127.0.0.1') and
                 f['basis'] in ('published', 'measured'),
                 f'{name}: 真实模式不接受测试主机/合成证据 {fid}')
        need(text(src['title'], 5) and text(src['locator'], 5), f'{name}: 来源缺标题/定位 {fid}')
        need(text(src.get('locator', ''), 5) and
             (re.search(r'\d', src['locator']) or '§' in src['locator'] or '章' in src['locator']),
             f'{name}: 定位须含版本/章节信息 {fid}')
        try:
            date.fromisoformat(src['accessed_on'])
        except (ValueError, TypeError):
            raise Invalid(f'{name}: 访问日期非法 {fid}')
        if f['kind'] == 'benchmark':
            m = f.get('metric') or {}
            need(money(m.get('value', '')) > 0, f'{name}: 性能数值须为正 {fid}')
            need(m['unit'] in ('QPS', 'TPS', 'ms', 'MB/s', 'GB/s'), f'{name}: 性能单位非法 {fid}')
            need(text(m.get('workload'), 20) and text(m.get('environment'), 20),
                 f'{name}: 性能缺负载/环境 {fid}')
    kinds = {f['kind'] for f in facts}
    need(kinds <= {'capability', 'benchmark', 'case'}, f'{name}: 证据类型越界')
    costs = data.get('costs') or []
    for row in costs:
        # 成本行是计算明细不是事实证据：ID 只做唯一性/冲突校验，
        # 不并入证据 ID 集（正文引用以 facts 为准，六节点同口径）
        need(text(row['id'], 3) and row['id'] not in ids, f'{name}: 成本行ID缺失/与证据ID冲突')
        need(text(row['item'], 5) and text(row['quantity_unit']), f'{name}: 成本项目/单位缺失')
        need(row['period'] == 'month' and row['currency'] == 'CNY', f'{name}: 需月度/CNY口径')
        need(row['source_id'] in {f['id'] for f in facts}, f'{name}: 成本来源未绑定证据')
        expected = (money(row['quantity']) * money(row['unit_price'])).quantize(
            Decimal('.01'), rounding=ROUND_HALF_UP)
        need(money(row['monthly_total']) == expected, f'{name}: 成本乘法不一致 {row["id"]}')
    need(isinstance(data['limitations'], list) and any(text(x, 20) for x in data['limitations']),
         f'{name}: 缺适用限制')
    if require_domain_refs:
        dr = data.get('domain_refs') or {}
        need(set(dr) == set(DOMAINS), f'{name}: domain_refs须覆盖三领域')
        return data, ids, dr
    return data, ids, None


ASPECT_SPEC = {
    'feature': (('核心机制', '适用与不适用场景', '接口与运维边界'), 'capability'),
    'performance': (('指标与来源', '适用条件', '不适用条件'), 'benchmark'),
    'case': (('案例与来源', '采用原因', '迁移限制'), 'case'),
    'cost': (('计费口径', '成本计算', '未计项目'), 'capability'),
}


def tech_aspect(stage, folder, manifest):
    tech, aspect = stage.rsplit('-', 1)
    need(tech in TECHS and aspect in ASPECTS, f'非法stage: {stage}')
    data, ids, _ = evidence_file(folder, stage, manifest, from_bound=False)
    if aspect == 'performance':
        need(any(f['kind'] == 'benchmark' for f in data['facts']),
             f'{stage}: 性能节点须含benchmark证据')
    if aspect == 'case':
        need(any(f['kind'] == 'case' for f in data['facts']),
             f'{stage}: 案例节点须含case证据')
    if aspect == 'cost':
        need(data.get('costs'), f'{stage}: 成本节点须含costs明细')
        for row in data['costs']:
            need(text(row.get('price_date', ''), 8) and text(row.get('region', ''), 2),
                 f'{stage}: 成本缺价格日期/地区')
    content = zero_mark(citations(Path(folder) / f'{stage}.md', ids, ids), manifest)
    headings(content, ASPECT_SPEC[aspect][0])
    return ids


def integration(folder, subject, upstream_expected, manifest, headings_names):
    """整合父通用检查（subject=文件基名，如 kafka-integration）：
    upstream哈希表与inputs快照逐一核对+正文引用全部上游证据。
    2026-11-08 可信上游增强：转交文件必须与 inputs/ 播种快照（平台
    绑定核对过的权威副本）逐字节一致——执行者改写 cwd 转交件而保留
    inputs 快照原样的绕过被拒绝（同型漏洞曾只在 root 修复）。"""
    name = f'{subject}.json'
    path = Path(folder) / name
    need(path.is_file(), f'缺整合清单: {name}')
    data = read_json(path)
    need(data['schema_version'] == 1 and data['subject'] == subject, f'{name}: subject不符')
    need(data['mode'] == manifest['mode'] and data['review_status'] == 'unverified',
         f'{name}: mode/review_status不符')
    up = data['upstream']
    need(set(up) == set(upstream_expected),
         f'{name}: upstream清单不符（缺 {sorted(set(upstream_expected) - set(up))[:6]}'
         f' 多 {sorted(set(up) - set(upstream_expected))[:6]}）')
    for fname, digest in up.items():
        src = bound(folder, fname)
        need(sha(src) == digest, f'{name}: upstream哈希不符 {fname}')
        delivered = Path(folder) / fname
        need(delivered.is_file(), f'{name}: 整合目录缺转交交付: {fname}')
        need(sha(delivered) == sha(src),
             f'{name}: 转交文件与上游播种快照不一致: {fname}')
    all_ids = set()
    for fname in up:
        if fname.endswith('-evidence.json'):
            all_ids |= {f['id'] for f in read_json(bound(folder, fname))['facts']}
    content = zero_mark(citations(Path(folder) / f'{subject}.md', all_ids), manifest)
    headings(content, headings_names)
    need(isinstance(data.get('conclusions'), list) and any(text(c, 30) for c in data['conclusions']),
         f'{name}: 缺结论')
    return all_ids, up


def domain_compare(folder, stage, manifest):
    domain = stage[:-len('-compare')]
    need(domain in DOMAINS, f'非法比较stage: {stage}')
    techs = DOMAIN_TECHS[domain]
    requirements = imports(folder, manifest)
    # 交付文件名以 structure 为准：{domain}-comparison.{md,json}
    # （stage 是 {domain}-compare，两者后缀不同）
    name = f'{domain}-comparison.json'
    data = read_json(Path(folder) / name)
    need(data['schema_version'] == 1 and data['subject'] == f'{domain}-comparison',
         f'{name}: subject不符')
    need(data['mode'] == manifest['mode'] and data['review_status'] == 'unverified',
         f'{name}: mode/review不符')
    expected_inputs = tuple(f'{t}-integration.json' for t in techs)
    need(set(data['input_sha256']) == set(expected_inputs),
         f'{name}: input_sha256须覆盖本领域全部技术整合清单')
    for fname in expected_inputs:
        need(data['input_sha256'][fname] == sha(bound(folder, fname)),
             f'{name}: 未引用实际上游版本 {fname}')
    wanted = {r['id'] for r in requirements['requirements']}
    criteria = data['criteria']
    need({c['requirement_id'] for c in criteria} == wanted and len(criteria) == len(wanted),
         f'{name}: 需求映射缺失/重复')
    tech_ids = {}
    # 收集本领域各技术的全部深度证据ID（技术整合快照中的深度证据文件）
    for t in techs:
        ids_t = set()
        for a in ASPECTS:
            _, ids_a, _ = evidence_file(folder, f'{t}-{a}', manifest)
            ids_t |= ids_a
        tech_ids[t] = ids_t
    for c in criteria:
        need(text(c['assessment'], 40), f'{name}: 比较说明过短')
        ev = c['evidence']
        need(set(ev) == set(techs), f'{name}: 证据键须为本领域全部技术')
        for t in techs:
            refs = ev[t]
            need(isinstance(refs, list) and refs, f'{name}: {c["requirement_id"]}/{t} 缺证据')
            need(set(refs) <= tech_ids[t], f'{name}: 引用了{t}不存在的深度证据')
    rec = data['recommendation']
    need(isinstance(rec.get('ranking'), list) and set(rec['ranking']) == set(techs),
         f'{name}: 缺排序或排序不含全部技术')
    need(text(rec.get('rationale'), 80), f'{name}: 推理论证过短')
    need(rec.get('status') == 'provisional', f'{name}: 未经独立复核只能给provisional推荐')
    content = zero_mark(citations(Path(folder) / f'{domain}-comparison.md', set().union(*tech_ids.values())), manifest)
    headings(content, ('逐项比较', '成本对比', '初步推荐'))
    return tech_ids


def cross_study(stage, folder, manifest):
    need(stage in CROSS_STUDIES, f'非法跨领域stage: {stage}')
    # 交付文件名与 subject 字段均带 cross- 前缀（structure/schema 口径）
    data, ids, dr = evidence_file(folder, f'cross-{stage}', manifest, from_bound=False,
                                  require_domain_refs=True,
                                  json_name=f'cross-{stage}-evidence.json')
    if stage == 'total-cost':
        costs = data.get('costs') or []
        need(costs, 'total-cost须含成本明细')
    # domain_refs 必须指向三领域快照中真实存在的深度证据ID
    for d, refs in dr.items():
        need(isinstance(refs, list) and refs, f'{stage}: domain_refs[{d}]为空')
        ids_d = set()
        for t in DOMAIN_TECHS[d]:
            for a in ASPECTS:
                _, ids_a, _ = evidence_file(folder, f'{t}-{a}', manifest)
                ids_d |= ids_a
        need(set(refs) <= ids_d, f'{stage}: domain_refs[{d}]引用了不存在的上游证据')
    content = zero_mark(citations(Path(folder) / f'cross-{stage}.md', set(ids) | set().union(*dr.values()) if dr else ids), manifest)
    headings(content, ('分析', '依据与来源', '限制'))
    return ids, dr


def research_integration(folder, manifest):
    """总研究父整合：upstream=三领域全sets+cross全set（94文件）。"""
    expected = set()
    for d in DOMAINS:
        expected |= set(domain_artifacts(d))
    expected |= set(cross_artifacts())
    all_ids, up = integration(folder, 'research-integration', expected, manifest,
                              ('整合结论', '证据索引'))
    # 每个领域/跨领域整合证据文件逐个校验并收集全部证据ID
    # （比较节点交付是 comparison.json，无独立 evidence.json——其引用的
    # 深度证据ID已在上面的深度收集里覆盖）
    all_ev = set()
    for d in DOMAINS:
        for t in DOMAIN_TECHS[d]:
            for a in ASPECTS:
                _, ids, _ = evidence_file(folder, f'{t}-{a}', manifest)
                all_ev |= ids
    for name in CROSS_STUDIES:
        _, ids_x, _ = evidence_file(folder, f'cross-{name}', manifest,
                                    json_name=f'cross-{name}-evidence.json')
        all_ev |= ids_x
    content = zero_mark(citations(Path(folder) / 'research-integration.md', all_ev), manifest)
    return all_ev, up


# 根节点自撰交付（无上游来源，不参与可信上游核对；其余 listed 交付
# 必须与可信上游逐字节一致）
ROOT_SELF_AUTHORED = ('final-report.md', 'source-index.json',
                      'delivery-manifest.json', 'review.json')


def _manifest_hash_of(files: list[dict]) -> str:
    """与 dag_agent.artifacts.manifest_hash_of 逐字节一致的快照清单摘要计算
    （纯标准库实现，使 frozen/checks.py 可脱离包路径独立运行）。"""
    def _src_tag(src: dict) -> str:
        if src.get("node") is not None:
            if "basis" in src:
                return (f":{src['node']}@{src['version']}"
                        f":{src.get('file', '')}:{src['basis']}")
            return (f":{src['node']}@{src['version']}"
                    f":{src.get('file', '')}")
        cands = sorted(src.get("candidates") or [],
                       key=lambda c: (c.get("node", ""),
                                      c.get("version", 0),
                                      c.get("file", "")))
        return f":{src.get('origin', 'unknown')}:" + ",".join(
            f"{c['node']}@{c['version']}:{c.get('file', '')}"
            for c in cands)

    lines = sorted(f"{f['rel_path']}:{f['sha256']}:{f.get('size', 0)}"
                   + (_src_tag(f["source"]) if f.get("source") else "")
                   for f in files)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def trusted_from_db(db_path, expected_mission=None, expected_attempt=None):
    """从平台任务库（SQLite，只读 mode=ro）构建可信上游清单（R1/A2 强化）。

    可信锚=平台持久化的绑定数据（attempts.input_binding_json →
    snapshots.files_json），执行者不可写。绑定对象精确化：
    - mission 显式核对（expected_mission 或单任务库）；
    - root attempt 选择：
      1) 若显式传入 expected_attempt（平台判卷注入或命令行指定），直接精确
         绑定该 attempt（状态须为 running 或 accepted），支持失败重试、崩溃
         重认领及已有历史 accepted 后的新版本判卷；
      2) 未传入 expected_attempt 时：若当前存在唯一 running 绑定且无 accepted
         绑定（如失败/取消后的正常重试判卷），绑定该唯一 running attempt；
         若无 running 绑定且存在唯一（或当前有效版本对应的）accepted 绑定，
         绑定该 accepted attempt；其余（多 running、running 与 accepted 并存
         且未指定 attempt、多 accepted 歧义、或全部为失败终止态）一律歧义拒绝；
    - 每个绑定逐项核对：snapshot 身份（node_id==source）、版本
      （artifact_version==binding）、有效性（valid==1）、manifest_hash
      （与快照列及 files_json 重算值双重一致）以及选定 files 范围（非空、
      全在快照内、仅导出选定范围文件）；
    - 同名文件跨来源哈希冲突=版本歧义，显式拒绝。
    返回 ({rel_path: sha256}, mission_id, attempt_id)。
    """
    p = Path(db_path).resolve()
    need(p.is_file(), f'可信库不存在: {p.name}')
    uri = p.as_uri() + '?mode=ro'
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        mids = [r[0] for r in con.execute("SELECT id FROM missions")]
        if expected_mission is not None:
            need(mids == [expected_mission],
                 f'可信库任务与验收对象不符（库={mids}，'
                 f'期望={expected_mission}）')
        need(len(mids) == 1,
             f'可信库含 {len(mids)} 个任务（歧义拒绝）')
        mid = mids[0]
        root_rows = con.execute(
            "SELECT id, accepted_artifact_version FROM nodes"
            " WHERE mission_id=? AND parent_id IS NULL",
            (mid,)).fetchall()
        need(len(root_rows) == 1,
             f'可信库根节点不唯一: {[r["id"] for r in root_rows]}')
        root_id = root_rows[0]["id"]
        root_acc_ver = root_rows[0]["accepted_artifact_version"]
        rows = con.execute(
            "SELECT id, status, input_binding_json FROM attempts"
            " WHERE node_id=? ORDER BY rowid DESC", (root_id,)).fetchall()
        bound = [(r["id"], r["status"], r["input_binding_json"])
                 for r in rows
                 if r["input_binding_json"]
                 and json.loads(r["input_binding_json"] or "[]")]
        need(bool(bound), '可信库无 root 输入绑定（未认领或绑定缺失）')
        if expected_attempt is not None:
            matched = [b for b in bound if str(b[0]) == str(expected_attempt)]
            need(len(matched) == 1,
                 f'指定的 root attempt 不存在或无输入绑定: {expected_attempt}')
            att_id, att_st, binding_json = matched[0]
            need(att_st in ('running', 'accepted'),
                 f'指定的 root attempt 状态不可作为验收锚: {att_id}({att_st})')
        else:
            running_b = [b for b in bound if b[1] == 'running']
            accepted_b = [b for b in bound if b[1] == 'accepted']
            if running_b:
                need(len(running_b) == 1 and not accepted_b,
                     f'root 含 {len(running_b)} 个 running 与 {len(accepted_b)} 个'
                     ' accepted 绑定 attempt（未指定 attempt 时验收对象歧义拒绝）')
                att_id, _st, binding_json = running_b[0]
            elif accepted_b:
                if len(accepted_b) == 1:
                    att_id, _st, binding_json = accepted_b[0]
                else:
                    cur_snap = (con.execute(
                        "SELECT attempt_id FROM snapshots WHERE node_id=?"
                        " AND artifact_version=? AND valid=1",
                        (root_id, root_acc_ver)).fetchall()
                        if root_acc_ver is not None else [])
                    cur_ids = {r["attempt_id"] for r in cur_snap}
                    cur_acc = [b for b in accepted_b if b[0] in cur_ids]
                    need(len(cur_acc) == 1,
                         f'root 有 {len(accepted_b)} 个 accepted 绑定 attempt（歧义）')
                    att_id, _st, binding_json = cur_acc[0]
            else:
                raise Invalid(
                    f'root 无 running 或 accepted 的有效绑定 attempt'
                    f'（历史终止 attempt 数={len(bound)}）——验收拒绝')
        trusted = {}
        for b in json.loads(binding_json or "[]"):
            snap = con.execute(
                "SELECT node_id, artifact_version, manifest_hash, files_json, valid"
                " FROM snapshots WHERE id=?",
                (b.get("snapshot_id"),)).fetchone()
            need(snap is not None,
                 f'可信快照缺失: {b.get("source")}'
                 f'(snap {b.get("snapshot_id")})')
            # 身份/版本逐项核对（R1 §4）
            need(str(snap["node_id"]) == str(b.get("source")),
                 f'快照来源不符: snap.node_id={snap["node_id"]} != '
                 f'binding.source={b.get("source")}')
            if b.get("artifact_version") is not None \
                    and snap["artifact_version"] is not None:
                need(int(snap["artifact_version"])
                     == int(b.get("artifact_version")),
                     f'快照版本不符: snap={snap["artifact_version"]} != '
                     f'binding={b.get("artifact_version")}')
            valid = snap["valid"]
            if valid is not None and int(valid) not in (0, 1):
                need(False, f'快照有效性标记非法: {valid}')
            if valid is not None and int(valid) == 0:
                need(False, f'快照已失效: {b.get("source")}'
                            f'(snap {b.get("snapshot_id")})')
            files_meta = json.loads(snap["files_json"] or "[]")
            need(isinstance(files_meta, list) and bool(files_meta),
                 f'快照文件清单为空: {b.get("source")}')
            b_mh = b.get("manifest_hash")
            need(bool(b_mh),
                 f'输入绑定缺少 manifest_hash: {b.get("source")}')
            need(str(snap["manifest_hash"] or "") == str(b_mh),
                 f'快照 manifest_hash 与绑定不符: snap={snap["manifest_hash"]}'
                 f' != binding={b_mh}')
            recomputed_mh = _manifest_hash_of(files_meta)
            need(recomputed_mh == str(b_mh),
                 f'快照内容重算 manifest_hash 不符: recomputed={recomputed_mh}'
                 f' != binding={b_mh}')
            snap_files = {f["rel_path"]: f["sha256"] for f in files_meta}
            b_files = b.get("files")
            need(isinstance(b_files, list) and len(b_files) > 0,
                 f'输入绑定缺少选定 files 范围: {b.get("source")}')
            missing_in_snap = [fn for fn in b_files if fn not in snap_files]
            need(not missing_in_snap,
                 f'绑定选定文件不在快照内: {b.get("source")} 缺 {missing_in_snap[:5]}')
            for name in b_files:
                f_sha = snap_files[name]
                if name in trusted and trusted[name] != f_sha:
                    raise Invalid(f'可信上游同名版本冲突: {name}')
                trusted[name] = f_sha
        return trusted, mid, att_id
    finally:
        con.close()


def export_trusted(db_path, out_json_path, out_digest_path):
    """平台侧导出可信清单 + 绑定摘要文件（R1：导出的"可信"锚不在
    generated_by 自报字符串，而在执行者可写产物之外的平台摘要文件——
    digest 文件记录 trusted JSON 的 SHA256 与 mission/attempt 身份，
    由平台/审计方置于冻结/受控位置；离线重放以 digest 为锚核对）。
    返回 {deliverables, mission_id, attempt_id}。"""
    trusted, mid, att_id = trusted_from_db(db_path)
    payload = {"schema_version": 2, "mission_id": mid,
               "root_attempt": att_id,
               "deliverables": trusted}
    jp, dp = Path(out_json_path), Path(out_digest_path)
    raw = json.dumps(payload, ensure_ascii=False, indent=1,
                     sort_keys=True).encode("utf-8")
    jp.write_bytes(raw)
    digest_rec = {"schema_version": 1, "kind": "dag-agent-trusted-export",
                  "trusted_json_sha256": hashlib.sha256(raw).hexdigest(),
                  "trusted_json_bytes": len(raw),
                  "mission_id": mid, "root_attempt": att_id,
                  "note": "digest 文件必须由平台/审计方生成并保存在待验"
                          "执行者不可写的位置（如 frozen/）"}
    dp.write_text(json.dumps(digest_rec, ensure_ascii=False, indent=1)
                  + "\n", encoding="utf-8")
    return {"mission_id": mid, "attempt_id": att_id,
            "json": str(jp), "digest": str(dp)}


def load_trusted(trusted_json_path=None, trusted_digest_path=None,
                 trusted_db_path=None, expected_mission=None,
                 expected_attempt=None):
    """加载可信上游清单（R1/A2 强化）。

    可信锚定义（generated_by/source/mission_id 等自报字符串**不构成**
    认证）：
    - --trusted-db：平台库只读（权威）；
    - --trusted + --trusted-digest：平台导出 JSON + 其 SHA256 摘要文件
      （摘要文件由平台生成、存于待验执行者不可写位置——等效冻结锚）；
      digest 核对=sha256(json bytes) 精确匹配 + mission/attempt 绑定
      记录与 JSON 内容一致；
    - 同时提供 DB 与 JSON：两者逐键核对一致（并集一致才通过），
      不一致显式拒绝——较弱的 JSON 不能覆盖权威 DB；
    - 都不提供 → None（调用方拒绝验收）。
    """
    json_trusted = None
    json_meta = None
    if trusted_json_path is not None:
        need(trusted_digest_path is not None,
             '使用 --trusted 导出清单时必须同时提供 --trusted-digest'
             '（平台生成的 SHA256 摘要文件；generated_by 自报字符串不'
             '构成可信锚）')
        p = Path(trusted_json_path)
        need(p.is_file(), f'可信清单文件不存在: {p.name}')
        raw = p.read_bytes()
        data = json.loads(raw.decode("utf-8"))
        need(data.get('schema_version') == 2, '可信清单 schema 错误'
             '（需 v2：含 mission_id/root_attempt 绑定）')
        d = data.get('deliverables')
        need(isinstance(d, dict) and d, '可信清单 deliverables 缺失或为空')
        dp = Path(trusted_digest_path)
        need(dp.is_file(), f'可信摘要文件不存在: {dp.name}')
        rec = json.loads(dp.read_text(encoding="utf-8"))
        need(rec.get('kind') == 'dag-agent-trusted-export',
             '摘要文件类型不符（非平台导出摘要）')
        need(rec.get('trusted_json_sha256') == hashlib.sha256(raw).hexdigest(),
             '可信清单与平台摘要不匹配（SHA256 不符）——拒绝')
        need(str(data.get('mission_id')) == str(rec.get('mission_id')),
             '可信清单 mission 与摘要记录不一致——拒绝')
        need(str(data.get('root_attempt')) == str(rec.get('root_attempt')),
             '可信清单 root_attempt 与摘要记录不一致——拒绝')
        if expected_mission is not None:
            need(str(data.get('mission_id')) == str(expected_mission),
                 f'可信清单 mission 与验收对象不符（{data.get("mission_id")}'
                 f' != {expected_mission}）——拒绝')
        if expected_attempt is not None:
            need(str(data.get('root_attempt')) == str(expected_attempt),
                 f'可信清单 root_attempt 与验收对象不符（{data.get("root_attempt")}'
                 f' != {expected_attempt}）——拒绝')
        json_trusted = dict(d)
        json_meta = {"mission_id": data.get('mission_id'),
                     "root_attempt": data.get('root_attempt')}
    db_trusted = None
    db_meta = None
    if trusted_db_path is not None:
        db_trusted, db_mid, db_att = trusted_from_db(
            trusted_db_path,
            expected_mission=expected_mission,
            expected_attempt=expected_attempt)
        db_meta = {"mission_id": db_mid, "root_attempt": db_att}
    if json_trusted is not None and db_trusted is not None:
        # 多来源：核对全部一致（并集），弱 JSON 不能覆盖权威 DB
        need(json_meta["mission_id"] == db_meta["mission_id"]
             and str(json_meta["root_attempt"]) == str(db_meta["root_attempt"]),
             f'可信来源身份冲突: json={json_meta} vs db={db_meta}——拒绝')
        keys = set(json_trusted) | set(db_trusted)
        conflicts = [k for k in sorted(keys)
                     if json_trusted.get(k) != db_trusted.get(k)]
        need(not conflicts,
             f'可信来源冲突（导出清单与平台库不一致，JSON 不覆盖 DB）: '
             f'{conflicts[:8]}')
        return db_trusted
    if json_trusted is not None:
        return json_trusted
    if db_trusted is not None:
        return db_trusted
    return None


def root_stage(folder, manifest, trusted=None):
    requirements = imports(folder, manifest, direct=True)
    expected_files = root_expected_deliverables()
    # delivery-manifest：覆盖全部非导入节点交付
    dm = read_json(Path(folder) / 'delivery-manifest.json')
    need(dm['schema_version'] == 1, 'manifest schema错误')
    listed = dm['deliverables']
    need(set(listed) == expected_files,
         f'delivery-manifest清单不符（缺 {sorted(expected_files - set(listed))[:8]}'
         f' 多 {sorted(set(listed) - expected_files)[:8]}）')
    # 路径越界防护（2026-11-08）：先验证全部键是安全的相对文件名，
    # 再用于路径拼接（'..' / 绝对路径一律拒绝）
    for fname in listed:
        fp = Path(fname)
        need(not fp.is_absolute() and '..' not in fp.parts
             and str(fname) == Path(str(fname)).as_posix(),
             f'交付路径非法或越界: {fname!r}')
    # 可信上游核对（2026-11-08 缺陷修复：自报哈希不再是转交件权威）：
    # 非 self-authored 的转交文件必须与可信清单逐字节一致；可信清单
    # 优先级 --trusted-db（平台库只读）> --trusted（平台导出 JSON）>
    # inputs/ 播种快照（平台绑定副本，仅真实运行现场可用）；干净重放
    # 无任何可信材料 → 明确拒绝，绝不退化为只验自报哈希。
    missing_visible = []
    upstream_checked = 0
    for fname, digest in listed.items():
        # 根部直查（根整合原样转交全部交付；干净重放目录无 inputs/ 结构）
        p = Path(folder) / fname
        if not p.is_file():
            missing_visible.append(fname)
            continue
        need(sha(p) == digest, f'交付哈希不符: {fname}')
        if fname in ROOT_SELF_AUTHORED:
            continue
        if trusted is not None:
            need(fname in trusted,
                 f'转交文件不在可信上游清单: {fname}')
            need(sha(p) == trusted[fname],
                 f'转交文件与可信上游不一致: {fname}')
            upstream_checked += 1
        else:
            # R1 §9：cwd/inputs 中任意存在的文件**不**自动认定为可信
            # 输入（执行者可写）——无可信锚（DB/导出+digest）即拒绝，
            # 不退化为播种快照兜底
            raise Invalid(
                '缺可信锚（--trusted-db 平台库 / --trusted+--trusted-digest '
                f'平台导出）——无法验证 {fname} 的上游一致性，拒绝验收')
    need(not missing_visible, f'根目录缺少交付: {missing_visible[:8]}')
    if trusted is not None:
        # 漏转交检测：可信上游清单中的文件必须全部出现在根交付
        # （自撰件除外；IMPORTS 4 件由 imports(direct=True) 单独核对
        # 哈希，不在 delivery-manifest 清单口径内）——防"少转交绕过
        # 一致性比对"
        not_delivered = sorted(set(trusted) - set(listed) - set(IMPORTS))
        need(not not_delivered,
             f'可信上游文件未全部转交: {not_delivered[:8]}')
    if upstream_checked == 0 and expected_files:
        # 全部交付都是自撰（无任何上游转交被核对）→ 结构性异常
        raise Invalid('根交付不含任何上游转交文件——整合链断裂，拒绝验收')
    # source-index：覆盖全部证据来源
    all_ids, sources = set(), []
    for t in TECHS:
        for a in ASPECTS:
            data, ids, _ = evidence_file(folder, f'{t}-{a}', manifest,
                                         from_bound=False)
            all_ids |= ids
            for f in data['facts']:
                sources.append((f['source']['url'], f['source']['locator']))
    for n in CROSS_STUDIES:
        data, ids, _ = evidence_file(folder, f'cross-{n}', manifest,
                                     from_bound=False,
                                     json_name=f'cross-{n}-evidence.json')
        all_ids |= ids
        for f in data['facts']:
            sources.append((f['source']['url'], f['source']['locator']))
    si = read_json(Path(folder) / 'source-index.json')
    need(si['schema_version'] == 1 and si['mode'] == manifest['mode'], 'source-index头不符')
    keys = [(s['url'], s['locator']) for s in si['sources']]
    need(len(keys) == len(set(keys)), 'source-index来源重复')
    need(set(keys) >= set(sources), 'source-index未覆盖全部证据来源')
    for s in si['sources']:
        need(text(s['title'], 5) and text(s['locator'], 3), 'source-index条目缺标题/定位')
        need(text(s['id'], 3), 'source-index条目缺ID')
    # 最终报告
    report = Path(folder) / 'final-report.md'
    content = zero_mark(citations(report, all_ids, min_len=500), manifest)
    headings(content, ('执行摘要', '需求匹配结论', '成本与风险', '实施建议', '限制与验证状态'))
    need('待独立复核' in content or 'content_review' in content,
         '最终报告须如实标注内容复核状态')
    review = read_json(Path(folder) / 'review.json')
    need(review == {'machine_scope': 'structure_consistency_traceability',
                    'content_review': 'pending', 'mode': manifest['mode']},
         '不得冒充人工/独立事实签收')


def category_of(stage):
    if stage == 'import':
        return 'import'
    if stage == 'root':
        return 'root'
    if stage in CROSS_STUDIES:
        return 'cross-study'
    if stage == 'cross-integration':
        return 'cross-integration'
    if stage.endswith('-compare'):
        d = stage[:-len('-compare')]
        if d in DOMAINS:
            return 'domain-compare'
    if stage.endswith('-integration'):
        base = stage[:-len('-integration')]
        if base in TECHS:
            return 'tech-integration'
        if base in DOMAINS:
            return 'domain-integration'
        if base == 'research':
            return 'research-integration'
    parts = stage.rsplit('-', 1)
    if len(parts) == 2 and parts[0] in TECHS and parts[1] in ASPECTS:
        return 'tech-aspect'
    raise Invalid(f'未知stage: {stage}')


def verify(stage, folder, manifest, trusted=None):
    folder = Path(folder)
    cat = category_of(stage)
    if cat == 'import':
        imports(folder, manifest, direct=True)
        return
    if cat == 'tech-aspect':
        tech_aspect(stage, folder, manifest)
        return
    if cat == 'tech-integration':
        tech = stage[:-len('-integration')]
        upstream = set()
        for a in ASPECTS:
            upstream |= set(x for x in (f'{tech}-{a}.md', f'{tech}-{a}-evidence.json'))
        integration(folder, stage, upstream, manifest, ('整合结论', '证据索引'))
        return
    if cat == 'domain-compare':
        domain_compare(folder, stage, manifest)
        return
    if cat == 'domain-integration':
        d = stage[:-len('-integration')]
        upstream = set()
        for t in DOMAIN_TECHS[d]:
            upstream |= set(tech_artifacts(t))
        upstream |= set(domain_compare_artifacts(d))
        integration(folder, stage, upstream, manifest, ('领域结论', '证据索引'))
        return
    if cat == 'cross-study':
        cross_study(stage, folder, manifest)
        return
    if cat == 'cross-integration':
        upstream = set()
        for n in CROSS_STUDIES:
            upstream |= set(cross_study_artifacts(n))
        integration(folder, stage, upstream, manifest, ('整合结论', '证据索引'))
        return
    if cat == 'research-integration':
        research_integration(folder, manifest)
        return
    if cat == 'root':
        root_stage(folder, manifest, trusted=trusted)
        return
    raise Invalid(f'未处理类别: {cat}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--trusted-db', type=Path, default=None,
                        help='平台任务库（只读）——root 转交文件的可信上游权威来源')
    parser.add_argument('--trusted', type=Path, default=None,
                        help='平台导出可信清单 JSON（须配 --trusted-digest）')
    parser.add_argument('--trusted-digest', type=Path, default=None,
                        help='平台生成的摘要文件（绑定 JSON 的 SHA256 与'
                        ' mission/attempt；须在待验执行者不可写位置）')
    parser.add_argument('--trusted-mission', default=None,
                        help='显式绑定验收 mission id（可选，缺省读平台环境变量或单任务库）')
    parser.add_argument('--trusted-attempt', default=None,
                        help='显式绑定验收 root attempt id（可选，缺省读平台环境变量）')
    args = parser.parse_args(argv)
    try:
        manifest = read_json(args.manifest)
        need(manifest['mode'] in ('zero-model', 'real'), 'manifest模式非法')
        need(set(manifest['imports']) == set(IMPORTS), 'manifest输入清单非法')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'考卷配置错误: {exc}')
        return 2
    try:
        # 可信上游清单仅 root 考卷需要（其余 stage 用 inputs/ 播种快照）；
        # 且 root 之外不读平台库（避免早期 stage 判卷时库尚无绑定误伤）
        if args.stage == 'root':
            import os as _os
            exp_mid = (getattr(args, 'trusted_mission', None)
                       or _os.environ.get('DAG_AGENT_VERIFY_MISSION_ID')
                       or None)
            exp_att = (getattr(args, 'trusted_attempt', None)
                       or _os.environ.get('DAG_AGENT_VERIFY_ATTEMPT_ID')
                       or None)
            trusted = load_trusted(args.trusted,
                                   getattr(args, 'trusted_digest', None),
                                   args.trusted_db
                                   or manifest.get('trusted_db'),
                                   expected_mission=exp_mid,
                                   expected_attempt=exp_att)
        else:
            trusted = None
        verify(args.stage, Path.cwd(), manifest, trusted=trusted)
    except (OSError, ValueError, KeyError, TypeError, AttributeError,
            InvalidOperation, Invalid) as exc:
        print(f'FAIL: {exc}')
        return 1
    print(f'PASS {args.stage}: 结构、一致性和追溯检查；事实复核仍待完成')
    return 0


if __name__ == '__main__':
    sys.exit(main())
