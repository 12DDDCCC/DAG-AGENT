"""v3.2 第4步·任务模板库：类别默认值 + 异构根考卷。

口径（任务书第4步）：
- timeout / 考卷条数上限 / 粒度参考 / 重试≤2 / 节点数熔断可配置；
- P95×1.5 仅在有足够真实历史数据时使用——当前默认值来源为本机实测
  （出游场景 $0.4-1.5/次、分钟级；v3 重建 13 次执行调用），非统计推断，
  各字段 provenance 标注来源；
- 根考卷异构：根用 check_composite（整体形态：存在+规模+结构小节），
  子节点用 test/单文件 script——不同层级不同实现，不复制子考卷求和。
"""
from __future__ import annotations

from pathlib import Path

_CHECKERS = Path(__file__).resolve().parent / "checkers"

# A2 整合考卷规范（现存问题总账 2026-10-03）：整合节点考卷必须
# ①逐件断言来源交付完整性（每来源至少一件非空交付、每件脚本可编译）；
# ②拼装产物可启动时点名入口冒烟（最小合法输入的退出码+产物断言）。
# 只数文件个数的弱整合考卷禁止——P_delivery 缺 main.py 曾漏到 ROOT 才炸。
# 参照实现：outputs/多agent协同验收项目/checks/mod_checks.py integration。
INTEGRATION_EXAMPLE = {
    "action": "cmd",
    "cmd": 'python "<考卷包入口>" integration <可启动入口名，无则省略>',
}


def _q(s: str) -> str:
    return '"' + str(s).replace('"', "'") + '"'


def composite_cmd(artifact: str, min_chars: int,
                  sections: tuple[str, ...] = ()) -> str:
    """根节点异构组合检查命令（check_composite）。"""
    parts = [_q(str(_CHECKERS / "check_composite.py")), _q(artifact),
             str(min_chars)] + [_q(s) for s in sections]
    return "python " + " ".join(parts)


TEMPLATES: dict[str, dict] = {
    "research": {
        "label": "研究报告（并行调研+整合）",
        "leaf": "多个研究子节点并行产出分报告，根整合为总报告",
        "leaf_acceptance_example": [
            {"action": "script", "path": "<项目内报告检查脚本>",
             "args": "<report.md>"},
        ],
        "root_artifact": "final_report.md",
        "root_min_chars": 3000,
        "root_sections": ("背景", "发现", "建议"),
        "timeout_s": 2700,            # 45min（实测研究类单次分钟级，2×余量）
        "max_acceptance_items": 5,
        "statement_chars": 2000,       # 与生长闸门⑤一致
        "artifact_lines_hint": "单文件 300–3000 字",
        "max_retries": 2,             # 协议03§6：首试+最多两次重试
        "node_cap": 200,              # 熔断器：防分解失控（审计 §4.10 建议）
        "integration_acceptance_example": INTEGRATION_EXAMPLE,
        "provenance": "默认值来自本机实测（出游场景/研究轮），非P95统计",
    },
    "code": {
        "label": "代码功能（模块并行+集成）",
        "leaf": "子节点各交付模块+pytest；根跑异构组合验收",
        "leaf_acceptance_example": [
            {"action": "test", "args": "tests/ -q"},
        ],
        "root_artifact": "集成说明.md",
        "root_min_chars": 200,
        "root_sections": ("模块", "接口", "验证"),
        "timeout_s": 1800,            # 30min（实测 code 类 $0.4-1.5/次）
        "max_acceptance_items": 5,
        "statement_chars": 2000,
        "artifact_lines_hint": "单模块 ≤500 行、单文件职责唯一",
        "max_retries": 2,
        "node_cap": 200,
        "integration_acceptance_example": INTEGRATION_EXAMPLE,
        "provenance": "默认值来自本机实测（v3 重建 13 次执行调用），非P95统计",
    },
    "docs": {
        "label": "文档（分章并行+统稿）",
        "leaf": "子节点各写一章；根统稿并验整体结构",
        "leaf_acceptance_example": [
            {"action": "scan", "dir": ".", "banned": ["TODO", "占位"]},
        ],
        "root_artifact": "doc.md",
        "root_min_chars": 1500,
        "root_sections": ("概述", "正文", "结论"),
        "timeout_s": 1200,            # 20min
        "max_acceptance_items": 4,
        "statement_chars": 1500,
        "artifact_lines_hint": "单章 200–2000 字",
        "max_retries": 2,
        "node_cap": 150,
        "integration_acceptance_example": INTEGRATION_EXAMPLE,
        "provenance": "默认值来自本机实测（文档类场景），非P95统计",
    },
    "data": {
        "label": "数据处理（管道分段+产物校验）",
        "leaf": "子节点各一段管道（清洗/转换/汇总）；根验最终数据集",
        "leaf_acceptance_example": [
            {"action": "cmd", "cmd": "python validate_stage.py"},
        ],
        "root_artifact": "summary.json",
        "root_min_chars": 50,
        "root_sections": ("rows",),
        "timeout_s": 1800,
        "max_acceptance_items": 5,
        "statement_chars": 2000,
        "artifact_lines_hint": "单段管道一个脚本、输入输出显式声明",
        "max_retries": 2,
        "node_cap": 200,
        "integration_acceptance_example": INTEGRATION_EXAMPLE,
        "provenance": "默认值来自本机实测（数据场景脚本分钟级），非P95统计",
    },
}


def root_check_for(template: str) -> str:
    """模板的根异构组合检查命令（根考卷与子考卷异构的落地形态）。"""
    t = TEMPLATES[template]
    return composite_cmd(t["root_artifact"], t["root_min_chars"],
                         tuple(t["root_sections"]))


def scheduler_defaults(template: str) -> dict:
    """模板配套调度默认（run 入口直接可用）。"""
    t = TEMPLATES[template]
    return {"timeout_s": t["timeout_s"], "max_retries": t["max_retries"],
            "node_cap": t["node_cap"], "max_acceptance_items":
                t["max_acceptance_items"],
            "provenance": t["provenance"]}
