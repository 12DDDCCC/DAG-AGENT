"""受信验收器：检查交付文件包含全部关键词（只读，零写入）。

用法：python check_file_contains.py <file> <keyword> [keyword ...]
cwd=尝试交付目录；文件缺失/任一关键词缺失 → exit 1。
"""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: python check_file_contains.py <file> <keyword> [keyword ...]")
        return 2
    target = Path(sys.argv[1])
    keywords = sys.argv[2:]
    if not target.is_file():
        print(f"FAIL: 交付文件缺失：{target}")
        return 1
    text = target.read_text(encoding="utf-8", errors="replace")
    missing = [k for k in keywords if k not in text]
    if missing:
        print(f"FAIL: 关键词缺失：{missing}")
        return 1
    print(f"PASS: {target} 含全部关键词 {keywords}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
