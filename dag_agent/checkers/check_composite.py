"""v3.2 模板配套·根节点异构组合检查器（第4步"根考卷异构"落地件）。

用法：python check_composite.py <产物文件> <最小字符数> [必需小节关键词…]
退出码：0=过；1=产物不合格；2=用法/环境故障（考卷侧）。
与子节点考卷（pytest/单文件断言）异构：按"整体交付形态"验，不复制子考卷求和，
不以关键词齐备替代功能——必须同时满足 存在性+规模+结构小节。
"""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: check_composite.py <产物文件> <最小字符数> [小节关键词…]",
              file=sys.stderr)
        return 2
    target = Path(sys.argv[1])
    try:
        min_chars = int(sys.argv[2])
    except ValueError:
        print(f"最小字符数须为整数：{sys.argv[2]}", file=sys.stderr)
        return 2
    sections = sys.argv[3:]
    if not target.is_file():
        print(f"产物缺失：{target}", file=sys.stderr)
        return 1
    text = target.read_text(encoding="utf-8", errors="replace")
    if len(text) < min_chars:
        print(f"产物规模不足：{len(text)} < {min_chars} 字符", file=sys.stderr)
        return 1
    missing = [s for s in sections if s not in text]
    if missing:
        print(f"缺少必需小节：{missing}", file=sys.stderr)
        return 1
    print(f"composite-ok: {len(text)} 字符，小节 {len(sections)}/{len(sections)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
