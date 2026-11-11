"""DAG-agent：把"探讨定的任务"变成可验证生长的DAG，节点过程在Obsidian可见。

模块：
- store:  任务状态机（节点/依赖/前沿/自动闭合/事件日志）
- verify: 检查运行器（子进程+超时+零写入守卫）
- render: Obsidian看板渲染（mermaid DAG + 明细 + 事件日志）
- cli:    命令行全功能入口
根目录 mcp_server.py: FastMCP 工具暴露（stdio）。
"""
__version__ = "3.2.1"
