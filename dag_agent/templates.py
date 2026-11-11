"""templates.py — mission模板库：常见任务类型的预置分解（陈述骨架+通用可判定检查）。

设计：
- 模板children的checks统一指向 create_from_template 自动写入任务目录的
  check_deliverable.py（通用交付物校验器：断言文件存在且含指定关键节），
  使模板开箱即有机器可判定验收；具体关键词由模板预置，可按需后续演进。
- 不做万能模板：每类模板只编码该类任务真实的公共骨架。
"""
from __future__ import annotations

from pathlib import Path

_VALIDATOR = '''# -*- coding: utf-8 -*-
"""check_deliverable.py — 通用交付物校验（零写入）：argv=文件路径 关键词[关键词...]"""
import sys
from pathlib import Path

if len(sys.argv) < 3:
    print("用法: check_deliverable.py <文件路径> <关键词...>")
    sys.exit(1)
target = Path(__file__).resolve().parent / sys.argv[1]
if not target.is_file():
    print(f"FAIL: 交付物不存在: {sys.argv[1]}")
    sys.exit(1)
text = target.read_text(encoding="utf-8", errors="replace")
missing = [k for k in sys.argv[2:] if k not in text]
if missing:
    print(f"FAIL: 缺关键节 {missing}")
    sys.exit(1)
print(f"deliverable ok: {sys.argv[1]} 含全部{len(sys.argv) - 2}个关键节")
'''

# 每类模板：root(标题/陈述) + children骨架（check=对workspace产物的关键节断言）
TEMPLATES: dict[str, dict] = {
    "research": {
        "root_title": "调研报告交付",
        "root_statement": "调研报告成稿，事实经核对，引用可溯",
        "children": [
            {"title": "资料收集与初稿", "statement": "workspace/draft.md收集资料并成初稿",
             "check_file": "workspace/draft.md", "keywords": ["资料"]},
            {"title": "事实核对", "statement": "workspace/facts_check.md记录逐条核对结论与一手信源",
             "check_file": "workspace/facts_check.md", "keywords": ["核对"]},
            {"title": "报告成稿", "statement": "workspace/report.md为最终报告，含结论与信息源",
             "check_file": "workspace/report.md", "keywords": ["结论", "信息源"]},
        ],
    },
    "code-feature": {
        "root_title": "功能交付",
        "root_statement": "功能实现+测试通过+文档更新",
        "children": [
            {"title": "实现", "statement": "workspace/impl_notes.md记录实现要点",
             "check_file": "workspace/impl_notes.md", "keywords": ["实现"]},
            {"title": "测试", "statement": "workspace/test_evidence.log记录测试输出（含passed）",
             "check_file": "workspace/test_evidence.log", "keywords": ["passed"]},
            {"title": "文档", "statement": "workspace/docs.md更新用法说明",
             "check_file": "workspace/docs.md", "keywords": ["用法"]},
        ],
    },
    "audit": {
        "root_title": "审计交付",
        "root_statement": "独立盲读回证词+汇总审计结论",
        "children": [
            {"title": "盲读回证词", "statement": "workspace/readback.txt为独立agent盲写证词",
             "check_file": "workspace/readback.txt", "keywords": ["边界"]},
            {"title": "审计结论", "statement": "workspace/audit_report.md汇总差距与处置",
             "check_file": "workspace/audit_report.md", "keywords": ["差距"]},
        ],
    },
    "docs": {
        "root_title": "文档交付",
        "root_statement": "手册/说明文档成稿，含用法示例，命令经实测",
        "children": [
            {"title": "起草", "statement": "workspace/doc_draft.md完成文档草稿",
             "check_file": "workspace/doc_draft.md", "keywords": ["用法"]},
            {"title": "命令实测", "statement": "workspace/cmd_evidence.log记录文档中每条命令的实测输出（含PASS）",
             "check_file": "workspace/cmd_evidence.log", "keywords": ["PASS"]},
            {"title": "定稿", "statement": "workspace/final.md为定稿，含用法与实测说明",
             "check_file": "workspace/final.md", "keywords": ["用法", "实测"]},
        ],
    },
    "release": {
        "root_title": "版本发布",
        "root_statement": "测试全绿证据+发布说明+提交记录齐备",
        "children": [
            {"title": "测试证据归档", "statement": "workspace/test_evidence.log含全量测试输出且含passed",
             "check_file": "workspace/test_evidence.log", "keywords": ["passed"]},
            {"title": "发布说明", "statement": "workspace/release_notes.md列出变更清单与已知边界",
             "check_file": "workspace/release_notes.md", "keywords": ["变更", "边界"]},
        ],
    },
    "index": {
        "root_title": "归档索引交付",
        "root_statement": "为目标目录生成README索引，条目与真实子目录一致（机器校验）",
        "children": [
            {"title": "扫描生成索引", "statement": "workspace/index_draft.md覆盖目标目录全部子目录",
             "check_file": "workspace/index_draft.md", "keywords": ["索引"]},
            {"title": "落位与一致性校验", "statement": "目标目录README.md落位且条目数一致（由一致性检查脚本复核）",
             "check_file": "workspace/index_final.md", "keywords": ["一致"]},
        ],
    },
}


def create_from_template(store, mid: str, name: str, brief: str,
                         template_id: str) -> dict:
    """按模板创建mission：写校验器→建根→按骨架分解→返回前沿。"""
    if template_id not in TEMPLATES:
        raise ValueError(
            f"未知模板 '{template_id}'。可用: {sorted(TEMPLATES)}")
    tpl = TEMPLATES[template_id]
    st = store.create_mission(mid, name, brief,
                              tpl["root_title"], tpl["root_statement"])
    mid_dir = store.mission_dir(mid)
    (mid_dir / "check_deliverable.py").write_text(_VALIDATOR, encoding="utf-8")
    children = []
    for ch in tpl["children"]:
        cmd = ["python", "check_deliverable.py", ch["check_file"], *ch["keywords"]]
        children.append({"title": ch["title"], "statement": ch["statement"],
                         "checks": [{"name": f"deliverable_{ch['check_file'].replace('/', '_')}",
                                     "command": cmd}]})
    r = store.decompose(mid, st["root"], children)
    return {"template": template_id, "children": r["children"],
            "status": r["status"]}
