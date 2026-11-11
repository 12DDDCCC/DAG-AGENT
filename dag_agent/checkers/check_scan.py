# -*- coding: utf-8 -*-
"""check_scan.py — 禁用词扫描器（scan 动作的执行体）。零写入。

用法: check_scan.py <目录> <词1> [词2 ...]
命中任一禁用词（.py/.md/.txt/.json/.js/.ts 等文本文件，逐词子串匹配）→ exit 1。
"""
import sys
from pathlib import Path

TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".js", ".ts", ".tsx", ".jsx",
                 ".yml", ".yaml", ".toml", ".ini", ".cfg", ".html", ".css",
                 ".sh", ".bat", ".sql"}
IGNORE_DIRS = {"__pycache__", ".git", "node_modules", ".pytest_cache", ".dag_tmp"}


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: check_scan.py <目录> <词...>")
        return 2
    root = Path(sys.argv[1])
    banned = sys.argv[2:]
    if not root.is_dir():
        print(f"FAIL: 扫描目录不存在: {root}")
        return 1
    hits: list[str] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if set(p.parts) & IGNORE_DIRS:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for w in banned:
            if w in text:
                hits.append(f"{p.relative_to(root)}: {w}")
    if hits:
        print(f"FAIL: 禁用词命中 {len(hits)} 处：{hits[:10]}")
        return 1
    print(f"scan ok: {root} 无禁用词 {banned}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
