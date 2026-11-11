"""A3 简报规范校验器（现存问题总账 2026-10-03）：派发前结构化校验。

背景：真机协同 3 次失败均与宿主简报质量高度相关（自足化后一次通过；
归因以简报为主变量，为经验判断而非唯一原因）。
五要素【齐全才放行】：输入、交付清单、接口签名、样例、自测入口——
以段标记 ``【输入】`` 等结构化书写，每段须有非空内容。
告警（不硬拒）：全篇无绝对路径特征、篇幅超 800 字（本机经验预算）。
"""
from __future__ import annotations

import re

# 五要素段标记（顺序即推荐书写顺序）
SECTIONS: tuple[str, ...] = ("输入", "交付清单", "接口签名", "样例", "自测入口")

# 测试桩后端豁免表（A3 派发硬闸门）：这些名字只出现在测试基建
# （零模型桩），不代表真实执行器派发，缺段降为告警事件不硬拒；
# 真实后端（claude-cli/host/zcode-agent 等）一律硬拦。
# null=MCP real=False 本地干跑后端（零交付，考卷裁决 fail）
STUB_BACKENDS: frozenset[str] = frozenset(
    {"stub", "fake", "spy", "shim", "null", "zcode-agent-test"})

_MAX_CHARS = 800

# 绝对路径特征：Windows 盘符 / UNC（\\\\） / 常见 POSIX 绝对路径
_ABS_PATH = re.compile(r"[A-Za-z]:[\\/]|\\\\|/(?:home|Users|tmp|var)/")


def check_briefing(text: str) -> dict:
    """结构化校验执行简报。返回 {ok, missing, warnings, sections}。

    ok=True 才放行派发；missing 非空=缺要素（硬拒）；warnings=经验告警。
    """
    text = text or ""
    found: dict[str, str] = {}
    marks = [(m.start(), m.group(1)) for m in
             re.finditer(r"【([^】]{1,12})】", text)]
    for i, (pos, name) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        found[name] = text[pos + len(name) + 2:end].strip()
    missing = [s for s in SECTIONS if not found.get(s)]
    warnings: list[str] = []
    if not _ABS_PATH.search(text):
        warnings.append("简报未含绝对路径（本机经验：自足化绝对路径后一次通过）")
    if len(text) > _MAX_CHARS:
        warnings.append(f"简报 {len(text)} 字超经验预算 {_MAX_CHARS}"
                        "（长规格建议引用绑定的规范文件）")
    return {"ok": not missing, "missing": missing, "warnings": warnings,
            "sections": {s: bool(found.get(s)) for s in SECTIONS}}
