"""cli.py — DAG-agent命令行全功能入口（agent无MCP宿主时用CLI等价操作）。

用法示例：
    python -m dag_agent.cli create demo "示例任务" "简报" "交付" "总陈述"
    python -m dag_agent.cli add-node demo --title "子任务" --statement "…" --deps n0 \
        --check readme_exists --command python check_readme.py
    python -m dag_agent.cli frontier demo
    python -m dag_agent.cli submit demo n1 --summary "做完了"
    python -m dag_agent.cli status demo
    python -m dag_agent.cli board demo
    python -m dag_agent.cli block demo n2 --reason "等外部key"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dag_agent.store import DagError, MissionStore


def _dump(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _parse_checks(args) -> list[dict] | None:
    """--check 名称 --command python 脚本.py 可重复出现，配对组成checks。"""
    if not args.check and not args.command:
        return None
    if len(args.check) != len(args.command):
        raise DagError("--check 与 --command 数量必须一一配对")
    return [{"name": n, "command": c} for n, c in zip(args.check, args.command)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="dag-agent",
        description="v1 历史兼容 CLI；无 checks 时可仅凭 summary 通过，v3 fail-closed 请使用 dag-agent-mcp。",
    )
    ap.add_argument("--home", default=None,
                    help="missions根目录（默认 ~/.dag-agent/v1/missions）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create")
    p.add_argument("mid"); p.add_argument("name"); p.add_argument("brief")
    p.add_argument("root_title"); p.add_argument("root_statement")
    p.add_argument("--check", action="append", default=[])
    p.add_argument("--command", action="append", nargs="+", default=[])

    p = sub.add_parser("add-node")
    p.add_argument("mid"); p.add_argument("--title", required=True)
    p.add_argument("--statement", required=True)
    p.add_argument("--deps", nargs="*", default=[])
    p.add_argument("--kind", choices=["task", "aggregate"], default="task")
    p.add_argument("--check", action="append", default=[])
    p.add_argument("--command", action="append", nargs="+", default=[])

    p = sub.add_parser("decompose")
    p.add_argument("mid"); p.add_argument("parent")
    p.add_argument("--child", action="append", required=True,
                   help="JSON: {title,statement,checks?}")

    p = sub.add_parser("frontier"); p.add_argument("mid")
    p = sub.add_parser("status"); p.add_argument("mid")
    p = sub.add_parser("board"); p.add_argument("mid")
    p = sub.add_parser("events"); p.add_argument("mid")
    p.add_argument("--tail", type=int, default=20)

    p = sub.add_parser("submit"); p.add_argument("mid"); p.add_argument("node")
    p.add_argument("--summary", required=True)
    p = sub.add_parser("block"); p.add_argument("mid"); p.add_argument("node")
    p.add_argument("--reason", required=True)
    p = sub.add_parser("unblock"); p.add_argument("mid"); p.add_argument("node")
    p = sub.add_parser("retry"); p.add_argument("mid")
    p.add_argument("node", nargs="?", default=None, help="缺省=根节点")
    p = sub.add_parser("readback"); p.add_argument("mid"); p.add_argument("node")
    p.add_argument("--text", required=True); p.add_argument("--model", default="")
    p = sub.add_parser("audit-prepare"); p.add_argument("mid"); p.add_argument("node")
    p = sub.add_parser("create-from-template")
    p.add_argument("mid"); p.add_argument("name"); p.add_argument("brief")
    p.add_argument("template")
    p = sub.add_parser("templates")

    args = ap.parse_args(argv)
    store = MissionStore(Path(args.home)) if args.home else MissionStore()
    try:
        if args.cmd == "create":
            _dump(store.create_mission(
                args.mid, args.name, args.brief, args.root_title,
                args.root_statement, _parse_checks(args)))
        elif args.cmd == "add-node":
            _dump(store.add_node(args.mid, args.title, args.statement,
                                 args.deps, _parse_checks(args), args.kind))
        elif args.cmd == "decompose":
            children = [json.loads(c) for c in args.child]
            _dump(store.decompose(args.mid, args.parent, children))
        elif args.cmd == "frontier":
            _dump(store.frontier(args.mid))
        elif args.cmd == "status":
            _dump(store.status(args.mid))
        elif args.cmd == "board":
            _dump({"board": str(store.board_path(args.mid)),
                   "content": store.board_path(args.mid).read_text(encoding="utf-8")})
        elif args.cmd == "events":
            _dump(store.read_events(args.mid, args.tail))
        elif args.cmd == "submit":
            _dump(store.submit(args.mid, args.node, args.summary))
        elif args.cmd == "block":
            _dump(store.block(args.mid, args.node, args.reason))
        elif args.cmd == "unblock":
            _dump(store.unblock(args.mid, args.node))
        elif args.cmd == "retry":
            _dump(store.retry(args.mid, getattr(args, "node", None)))
        elif args.cmd == "readback":
            _dump(store.set_readback(args.mid, args.node, args.text,
                                     args.model))
        elif args.cmd == "audit-prepare":
            _dump(store.audit_prepare(args.mid, args.node))
        elif args.cmd == "create-from-template":
            from dag_agent.templates import create_from_template
            _dump(create_from_template(store, args.mid, args.name, args.brief,
                                       args.template))
        elif args.cmd == "templates":
            from dag_agent.templates import TEMPLATES
            _dump({k: {"root": v["root_title"],
                       "children": [c["title"] for c in v["children"]]}
                   for k, v in TEMPLATES.items()})
        return 0
    except DagError as exc:
        _dump({"ok": False, "rejected": True, "reason": str(exc)})
        return 2
    except Exception as exc:  # noqa: BLE001  结构化输出而非裸traceback
        import traceback
        _dump({"ok": False, "error": f"{type(exc).__name__}: {exc}",
               "trace": traceback.format_exc().strip().splitlines()[-4:]})
        return 2


if __name__ == "__main__":
    sys.exit(main())
