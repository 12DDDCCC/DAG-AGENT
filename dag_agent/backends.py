"""Serializable CLI selection. Secrets belong in external configuration files."""
from __future__ import annotations

import math

BACKEND_OPTIONS = {
    "claude-cli": {"cli_path", "model", "env_file", "timeout"},
    "zcode-cli": {"cli_path", "node_path", "provider_config", "timeout", "permission_mode"},
    "pi-cli": {"cli_path", "model", "env_file", "timeout", "allowed_tools", "thinking"},
}


def normalize_backend(name=None, options=None):
    name = name or "claude-cli"
    if name not in BACKEND_OPTIONS:
        raise ValueError(f"未知执行器 {name!r}；可选：{', '.join(BACKEND_OPTIONS)}")
    if options is not None and not isinstance(options, dict):
        raise ValueError("backend_options 必须为字典")
    options = dict(options or {})
    unknown = set(options) - BACKEND_OPTIONS[name]
    if unknown:
        raise ValueError(f"{name} 不支持这些配置字段：{', '.join(sorted(unknown))}")
    for key, value in options.items():
        if key == "timeout":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("执行器 timeout 必须是正有限数")
        elif not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} 必须是非空字符串；不能传入凭据对象")
        if key == "permission_mode" and value not in {"edit", "build", "guarded", "yolo"}:
            raise ValueError("不支持的ZCode权限模式")
    return name, options


def create_backend(name=None, options=None):
    name, options = normalize_backend(name, options)
    if name == "claude-cli":
        from .cc_backend import ClaudeCliBackend
        from pathlib import Path
        if "env_file" in options:
            options["env_file"] = Path(options["env_file"])
        return ClaudeCliBackend(**options)
    if name == "pi-cli":
        from .pi_backend import PiBackend
        from pathlib import Path
        if "env_file" in options:
            options["env_file"] = Path(options["env_file"])
        return PiBackend(**options)
    from .zcode_backend import ZCodeCliBackend
    return ZCodeCliBackend(**options)
