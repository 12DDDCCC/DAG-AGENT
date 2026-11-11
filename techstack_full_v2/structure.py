# -*- coding: utf-8 -*-
"""50节点任务权威结构（单一事实来源）：模板与检查器共用本模块。

结构（任务书 2026-11-06 §3 冻结，总计 50 = 3+4+7+3+28+5）：
  深度0 root(1)
  深度1 import(2)/research(3)          初始创建
  深度2 domain-db(4)/domain-mq(5)/domain-cache(6)/cross(7)
                                       research 一次拆分；cross deps 前三领域
  深度3 7 技术 + 3 比较                领域拆分；比较 deps 同领域技术
  深度4 28 = 7技术×(特性/性能/案例/成本) 技术拆分；四个孩子独立
  深度3 5 跨领域研究                    cross 拆分；可并行
拆分次数 = 1(research)+3(领域)+7(技术)+1(cross) = 12
"""
from __future__ import annotations

TECHS = ("postgresql", "clickhouse", "mongodb",
         "kafka", "rabbitmq", "redis", "memcached")
ASPECTS = ("feature", "performance", "case", "cost")
DOMAINS = ("database", "message-queue", "cache")
DOMAIN_TECHS = {
    "database": ("postgresql", "clickhouse", "mongodb"),
    "message-queue": ("kafka", "rabbitmq"),
    "cache": ("redis", "memcached"),
}
CROSS_STUDIES = ("arch-dataflow", "consistency-recovery",
                 "total-cost", "deployment-ops", "migration-roadmap")

EXPECTED_NODES = 50
EXPECTED_SPLITS = 12

# ---- 模板注册 ID（SPLIT_TEMPLATES 用；执行者提案只能选这些） ----
TPL_DOMAINS = "full-domains-v1"        # research → 4 领域
TPL_DOMAIN = "full-domain-v1"          # 领域 → 技术×n + 比较（params.domain）
TPL_TECH = "full-tech-v1"              # 技术 → 4 深度（params.technology）
TPL_CROSS = "full-cross-v1"            # cross → 5 研究
TEMPLATE_IDS = (TPL_DOMAINS, TPL_DOMAIN, TPL_TECH, TPL_CROSS)

# ---- 各类节点交付物 ----
IMPORTS = ("requirements.json", "proposal-protocol.md",
           "deliverable-schema.md", "input-provenance.json")


def tech_children(tech: str) -> tuple[str, ...]:
    """技术节点的四个深度孩子 local_id。"""
    return tuple(f"{tech}-{a}" for a in ASPECTS)


def aspect_artifacts(tech: str, aspect: str) -> tuple[str, str]:
    """深度孩子的两个交付文件。"""
    return (f"{tech}-{aspect}.md", f"{tech}-{aspect}-evidence.json")


def tech_artifacts(tech: str) -> tuple[str, ...]:
    """技术父整合交付：4 深度交付原样汇集 + 整合正文/清单。"""
    out: list[str] = []
    for a in ASPECTS:
        out.extend(aspect_artifacts(tech, a))
    out.extend((f"{tech}-integration.md", f"{tech}-integration.json"))
    return tuple(out)


def domain_compare_artifacts(domain: str) -> tuple[str, str]:
    return (f"{domain}-comparison.md", f"{domain}-comparison.json")


def domain_artifacts(domain: str) -> tuple[str, ...]:
    """领域父整合交付：本领域技术深度交付原样 + 比较 + 领域整合正文/清单。"""
    out: list[str] = []
    for t in DOMAIN_TECHS[domain]:
        out.extend(tech_artifacts(t))
    out.extend(domain_compare_artifacts(domain))
    out.extend((f"{domain}-integration.md", f"{domain}-integration.json"))
    return tuple(out)


def cross_study_artifacts(name: str) -> tuple[str, str]:
    return (f"cross-{name}.md", f"cross-{name}-evidence.json")


def cross_artifacts() -> tuple[str, ...]:
    """跨领域父整合交付：5 研究原样 + 整合正文/清单。"""
    out: list[str] = []
    for name in CROSS_STUDIES:
        out.extend(cross_study_artifacts(name))
    out.extend(("cross-integration.md", "cross-integration.json"))
    return tuple(out)


ROOT_FILES = ("final-report.md", "source-index.json",
              "delivery-manifest.json", "review.json")


# ---- 拆分后每个孩子的 spec（模板 generate 的权威依据） ----

def domains_split_children() -> list[dict]:
    """research 的 4 孩子；cross deps 引用更早声明的三个领域 local_id。"""
    ch = [{"local_id": d, "title": TITLES_DOMAIN[d], "template_id": TPL_DOMAIN,
           "template_params": {"domain": d}, "depends_on": []}
          for d in DOMAINS]
    ch.append({"local_id": "cross", "title": TITLES_DOMAIN["cross"],
               "template_id": TPL_CROSS, "template_params": {},
               "depends_on": list(DOMAINS)})
    return ch


def domain_split_children(domain: str) -> list[dict]:
    """领域 → 全部技术孩子在前 + 比较孩子（deps=本领域技术）。"""
    ch = [{"local_id": t, "title": f"{t} 技术研究", "template_id": TPL_TECH,
           "template_params": {"technology": t}, "depends_on": []}
          for t in DOMAIN_TECHS[domain]]
    ch.append({"local_id": f"{domain}-compare",
               "title": f"{domain} 领域技术比较",
               "template_id": TPL_DOMAIN, "template_params": {"domain": domain},
               "depends_on": [t for t in DOMAIN_TECHS[domain]]})
    # 比较孩子本身是叶子（不拆分）：用 compare 模板参数标识
    ch[-1]["template_params"]["leaf"] = "compare"
    return ch


def tech_split_children(tech: str) -> list[dict]:
    """技术 → 四个独立深度孩子（无 deps，可并行）。"""
    return [{"local_id": f"{tech}-{a}",
             "title": f"{tech} {ASPECT_TITLE[a]}",
             "template_id": TPL_TECH,
             "template_params": {"technology": tech, "leaf": a},
             "depends_on": []}
            for a in ASPECTS]


def cross_split_children() -> list[dict]:
    """跨领域 → 五个独立研究（可并行）。"""
    return [{"local_id": n, "title": f"跨领域：{ASPECT_TITLE[n]}",
             "template_id": TPL_CROSS, "template_params": {"leaf": n},
             "depends_on": []}
            for n in CROSS_STUDIES]


ASPECT_TITLE = {
    "feature": "特性与边界研究", "performance": "性能条件研究",
    "case": "案例研究", "cost": "成本核算研究",
    "arch-dataflow": "架构与数据流", "consistency-recovery": "一致性与恢复",
    "total-cost": "总成本核算", "deployment-ops": "部署与运维",
    "migration-roadmap": "迁移风险与路线图",
    "database": "数据库领域研究", "message-queue": "消息队列领域研究",
    "cache": "缓存领域研究", "cross": "跨领域整合研究",
}
TITLES_DOMAIN = {d: f"{ASPECT_TITLE[d]}" for d in DOMAINS}
TITLES_DOMAIN["cross"] = ASPECT_TITLE["cross"]


def leaf_kind(params: dict) -> str | None:
    """孩子是否为叶子（不继续拆分）：compare / aspect / cross-study。"""
    leaf = (params or {}).get("leaf")
    if leaf == "compare" or leaf in ASPECTS or leaf in CROSS_STUDIES:
        return leaf
    return None


def root_expected_deliverables() -> set:
    """根 delivery-manifest 必须覆盖的全部非导入节点交付文件。"""
    files: set = set()
    for t in TECHS:
        files |= set(tech_artifacts(t))
    for d in DOMAINS:
        files |= set(domain_compare_artifacts(d))
        files |= {f'{d}-integration.md', f'{d}-integration.json'}
    for n in CROSS_STUDIES:
        files |= set(cross_study_artifacts(n))
    files |= {'cross-integration.md', 'cross-integration.json',
              'research-integration.md', 'research-integration.json'}
    return files
