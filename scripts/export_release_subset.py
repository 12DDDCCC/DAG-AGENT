# -*- coding: utf-8 -*-
"""提取干净的对外公开发布子集（源码 + 测试 + 文档 + pyproject.toml，剥离 work/ 历史运行大目录）。

特性：
- 严格目标路径安全校验：严拒源码根、源码祖先目录、受保护的源码组成子目录；
- 默认只允许向空目录或新建目录导出；非空目录覆盖须显式 --overwrite 并验证已有的有效清单；
- 暂存目录构建机制：成功校验后再安全替换发布，失败回滚不残留半成品；
- 严格字面量凭据扫描与精确 SHA256 豁免防碰撞；
- 包含 LICENSE 与 soak_runner 等自包含测试必要入口。

用法：
    python scripts/export_release_subset.py [--out release_subset/dag-agent-v3.2.0] [--overwrite]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import tomllib
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.publication_scan import (  # noqa: E402
    BINARY_EXT, partition_suspect_hits, scan_text,
)

INCLUDE_DIRS = (
    "dag_agent",
    "techstack_full_v2",
    "techstack_research_v2",
    "tests",
    "scripts",
    "协议",
    ".github",
)

INCLUDE_ROOT_FILES = (
    "pyproject.toml",
    "pytest.ini",
    ".gitignore",
    ".gitattributes",
    "mcp_server_v3.py",
    "mcp_server.py",
    "README.md",
    "DESIGN.md",
    "PROTOCOL.md",
    "AUDITOR.md",
    "CHANGELOG-v3.2.1.md",
    "agent.md",
    "LICENSE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "MANIFEST.in",
    "soak_runner.py",
)

INCLUDE_OUTPUT_DOCS = (
    "outputs/DAG-agent-技术预览版使用指南-2026-11-08.md",
    "outputs/DAG-agent-v3.2.1-公开发布说明.md",
)
REQUIRED_OUTPUT_DOCS = INCLUDE_OUTPUT_DOCS

REQUIRED_ROOT_FILES = (
    "pyproject.toml",
    "README.md",
    "LICENSE",
    "mcp_server_v3.py",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "MANIFEST.in",
)
REQUIRED_DIRS = ("dag_agent", "techstack_full_v2", "tests", "scripts", "协议", ".github")
EXPORTER_VERSION = 2

EXCLUDED_ROOTS = (
    "work",
    "missions",
    "data",
    "out",
    "probe",
    "soak_home",
    "test_output",
    "release_subset",
    "记忆",
)

FORBIDDEN_SUFFIXES = (
    ".pyc",
    ".pyo",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".db-wal",
    ".db-shm",
    ".lock",
)

FORBIDDEN_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".git",
    ".selftest-tmp",
}

def _should_skip(rel_parts: tuple[str, ...], file_name: str) -> bool:
    if any(part in FORBIDDEN_DIR_NAMES for part in rel_parts):
        return True
    if file_name.startswith(".env") or file_name == ".run.lock" or file_name == "publication_scan_v2.json":
        return True
    lower = file_name.lower()
    if any(lower.endswith(sfx) for sfx in FORBIDDEN_SUFFIXES):
        return True
    return False


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rmtree_force(path: Path) -> None:
    def _onerror(func, p, _):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass

    if path.exists():
        shutil.rmtree(path, onerror=_onerror)


PROTECTED_SOURCE_DIRS = {
    "dag_agent",
    "techstack_full_v2",
    "techstack_research_v2",
    "tests",
    "scripts",
    "协议",
    ".github",
}


def _validate_dest_path(src_root: Path, dest_dir: Path, overwrite: bool = False) -> None:
    """严格目标路径安全检查，防止任何对源码、上级目录或非发布目录的误删。"""
    src_real = Path(os.path.realpath(src_root)).resolve()
    dest_real = Path(os.path.realpath(dest_dir)).resolve()
    src_norm = os.path.normcase(str(src_real))
    dest_norm = os.path.normcase(str(dest_real))

    # 1. 拒绝目标与源码根相同
    if dest_norm == src_norm:
        raise ValueError(f"目标目录不能与源码根目录相同: {dest_dir}")

    # 2. 拒绝目标为源码根的祖先目录（如 D:\AI、D:\ 等）
    for p in src_real.parents:
        if dest_norm == os.path.normcase(str(p.resolve())):
            raise ValueError(f"目标目录不能为源码根目录的祖先目录: {dest_dir}")

    # 3. 拒绝目标位于受保护的源码组成子目录（dag_agent, tests, scripts 等）
    if dest_norm.startswith(src_norm + os.sep) or dest_norm.startswith(src_norm + "/"):
        rel_to_src = dest_real.relative_to(src_real)
        top_comp = rel_to_src.parts[0]
        if top_comp in PROTECTED_SOURCE_DIRS:
            raise ValueError(f"目标目录不能位于受保护的源码组成目录: {dest_dir}")

    # 4. 检查已有目录：非空目录默认拒绝，防止意外删除用户文件
    if dest_real.exists() and any(dest_real.iterdir()):
        if not overwrite:
            raise ValueError(
                f"目标目录已存在且非空：{dest_dir}。默认拒绝导出；若需覆盖既有发布，请指定 overwrite=True"
            )
        # 若开启 overwrite，必须强校验目标确属结构完整、哈希齐备的既有 dag-agent 导出产物
        manifest_file = dest_real / "RELEASE_MANIFEST.json"
        if not manifest_file.is_file():
            raise ValueError(
                f"拒绝覆盖非受控目录（未找到有效的 RELEASE_MANIFEST.json）：{dest_dir}"
            )
        try:
            mf = json.loads(manifest_file.read_text(encoding="utf-8"))
        except Exception as e:
            raise ValueError(
                f"拒绝覆盖损坏清单目录（无法解析 RELEASE_MANIFEST.json）：{dest_dir} ({e})"
            )
        # 强校验：拒绝 name-only 残缺清单，必须包含完整版本、文件字典与计数一致性
        if not isinstance(mf, dict) or mf.get("name") != "dag-agent":
            raise ValueError(f"拒绝覆盖未知清单目录（name != 'dag-agent'）：{dest_dir}")
        if not mf.get("version"):
            raise ValueError(f"拒绝覆盖不完整清单目录（缺少 version 字段）：{dest_dir}")
        files = mf.get("files")
        if not isinstance(files, dict) or not files:
            raise ValueError(f"拒绝覆盖残缺清单目录（缺少或空的 files 字段）：{dest_dir}")
        file_count = mf.get("file_count")
        if not isinstance(file_count, int) or file_count != len(files):
            raise ValueError(f"拒绝覆盖不一致清单目录（file_count != len(files)）：{dest_dir}")
        # 全量校验：逐条验证相对路径安全、字段类型/取值、文件存在、实际大小与 SHA256
        listed: set[str] = set()
        for rel_f, meta in files.items():
            if not isinstance(rel_f, str) or not rel_f or "\\" in rel_f or rel_f.startswith("/"):
                raise ValueError(f"拒绝覆盖：清单含非法相对路径 {rel_f!r}：{dest_dir}")
            parts = rel_f.split("/")
            if any(p in ("", ".", "..") for p in parts) or ":" in parts[0]:
                raise ValueError(f"拒绝覆盖：清单含越界/非规范路径 {rel_f!r}：{dest_dir}")
            if rel_f == "RELEASE_MANIFEST.json":
                raise ValueError(f"拒绝覆盖：清单不得登记自身：{dest_dir}")
            if not isinstance(meta, dict):
                raise ValueError(f"拒绝覆盖：{rel_f} 元数据不是对象：{dest_dir}")
            sha = meta.get("sha256")
            size = meta.get("bytes")
            if not (isinstance(sha, str) and len(sha) == 64
                    and all(c in "0123456789abcdef" for c in sha)):
                raise ValueError(f"拒绝覆盖：{rel_f} 的 sha256 非法：{dest_dir}")
            if not (isinstance(size, int) and not isinstance(size, bool) and size >= 0):
                raise ValueError(f"拒绝覆盖：{rel_f} 的 bytes 非法：{dest_dir}")
            target = dest_real.joinpath(*parts)
            try:
                target_real = Path(os.path.realpath(target))
                target_real.relative_to(dest_real)
            except ValueError:
                raise ValueError(f"拒绝覆盖：{rel_f} 解析后越出目标目录：{dest_dir}")
            if target.is_symlink() or not target.is_file():
                raise ValueError(f"拒绝覆盖：清单文件缺失或为链接 {rel_f}：{dest_dir}")
            raw = target.read_bytes()
            if len(raw) != size:
                raise ValueError(f"拒绝覆盖：{rel_f} 实际大小与清单不符：{dest_dir}")
            if _sha256_bytes(raw) != sha:
                raise ValueError(f"拒绝覆盖：{rel_f} 实际 SHA256 与清单不符：{dest_dir}")
            listed.add(os.path.normcase(rel_f))
        # 额外文件一律拒绝（不是纯净导出目录，可能含用户文件）
        for p in dest_real.rglob("*"):
            if p.is_dir() and not p.is_symlink():
                continue
            rel = p.relative_to(dest_real).as_posix()
            if rel == "RELEASE_MANIFEST.json":
                continue
            if os.path.normcase(rel) not in listed:
                raise ValueError(f"拒绝覆盖：目录含清单外文件 {rel}：{dest_dir}")


def _project_version(src_root: Path) -> str:
    data = tomllib.loads((src_root / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def _current_commit(src_root: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=src_root, capture_output=True,
    )
    return proc.stdout.decode().strip() if proc.returncode == 0 else "unknown"


def _git_file_bytes(src_root: Path, commit: str, rel_path: str) -> bytes:
    proc = subprocess.run(
        ["git", "show", f"{commit}:{rel_path}"], cwd=src_root,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"来源提交缺少发布文件：{rel_path}")
    return proc.stdout


def _head_release_files(src_root: Path, commit: str) -> set[str]:
    paths = [*INCLUDE_ROOT_FILES, *INCLUDE_OUTPUT_DOCS, *INCLUDE_DIRS]
    proc = subprocess.run(
        ["git", "ls-tree", "-r", "-z", "--name-only", commit, "--", *paths],
        cwd=src_root, capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError("无法读取来源提交的发布文件集合")
    result = set()
    for raw_rel in proc.stdout.decode("utf-8", errors="strict").split("\0"):
        if not raw_rel:
            continue
        rel = PurePosixPath(raw_rel)
        if not _should_skip(rel.parts, rel.name):
            result.add(raw_rel)
    return result


def export_release_subset(
    src_root: Path, dest_dir: Path, overwrite: bool = False,
    require_clean: bool = False,
) -> dict:
    """提取发布子集至目标目录，保证原子性、无污染与凭据安全。"""
    src_root = Path(src_root).resolve()
    dest_dir = Path(dest_dir).resolve()

    # 严格校验目标路径安全
    _validate_dest_path(src_root, dest_dir, overwrite=overwrite)
    missing = [rel for rel in REQUIRED_ROOT_FILES if not (src_root / rel).is_file()]
    missing += [rel + "/" for rel in REQUIRED_DIRS if not (src_root / rel).is_dir()]
    missing += [rel for rel in REQUIRED_OUTPUT_DOCS if not (src_root / rel).is_file()]
    if missing:
        raise ValueError(f"发布来源缺少必需文件或目录：{missing}")
    version = _project_version(src_root)
    source_commit = _current_commit(src_root)
    if require_clean and source_commit == "unknown":
        raise RuntimeError("严格发布导出要求来源位于 Git 仓库并有 HEAD 提交")

    # 采用临时暂存目录构建，确保构建失败不影响目标目录
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = dest_dir.parent / f".tmp-export-{os.getpid()}-{int(time.time() * 1000)}"
    if staging_dir.exists():
        _rmtree_force(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)

    copied_files: dict[str, dict] = {}
    suspect_hits: dict[str, list[dict]] = {}
    verified_synthetic_hits: dict[str, list[dict]] = {}
    total_bytes = 0

    try:
        def _copy_one(src_path: Path, rel_str: str) -> None:
            nonlocal total_bytes
            if require_clean and src_path.is_symlink():
                raise RuntimeError(f"严格发布不接受工作区符号链接：{rel_str}")
            dst_path = staging_dir / rel_str
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            raw = src_path.read_bytes()
            if require_clean:
                source_raw = _git_file_bytes(src_root, source_commit, rel_str)
                if raw != source_raw:
                    raise RuntimeError(
                        f"发布文件与来源提交存在原始字节差异（含换行）：{rel_str}")
            dst_path.write_bytes(raw)
            size = len(raw)
            total_bytes += size
            copied_files[rel_str] = {
                "sha256": _sha256_bytes(raw),
                "bytes": size,
            }
            if not rel_str.endswith(tuple(BINARY_EXT)):
                text = raw.decode("utf-8", errors="replace")
                real = [h for h in scan_text(text) if h["class"] == "suspect_real"]
                if real:
                    unverified, synthetic = partition_suspect_hits(rel_str, real)
                    if unverified:
                        suspect_hits[rel_str] = unverified
                    if synthetic:
                        verified_synthetic_hits[rel_str] = synthetic

        for rf in INCLUDE_ROOT_FILES:
            sp = src_root / rf
            if sp.is_file():
                _copy_one(sp, rf)

        for doc_rel in INCLUDE_OUTPUT_DOCS:
            sp = src_root / doc_rel
            if sp.is_file():
                _copy_one(sp, doc_rel)

        for dname in INCLUDE_DIRS:
            sdir = src_root / dname
            if not sdir.is_dir():
                continue
            for p in sorted(sdir.rglob("*")):
                if not p.is_file():
                    continue
                rel = p.relative_to(src_root)
                if _should_skip(rel.parts, p.name):
                    continue
                _copy_one(p, rel.as_posix())

        if require_clean:
            expected_files = _head_release_files(src_root, source_commit)
            actual_files = set(copied_files)
            if actual_files != expected_files:
                missing_files = sorted(expected_files - actual_files)
                extra_files = sorted(actual_files - expected_files)
                raise RuntimeError(
                    f"发布文件集合与来源提交存在差异：缺失={missing_files}，额外={extra_files}")

        manifest = {
            "name": "dag-agent",
            "version": version,
            "source_commit": source_commit,
            "source_clean_verified": require_clean,
            "source_mode": "git-head-strict" if require_clean else "worktree-unverified",
            "exporter_version": EXPORTER_VERSION,
            "file_count": len(copied_files),
            "total_bytes": total_bytes,
            "suspect_real_hits_count": len(suspect_hits),
            "suspect_real_hits": suspect_hits,
            "verified_synthetic_test_hits": verified_synthetic_hits,
            "excluded_roots": list(EXCLUDED_ROOTS),
            "files": copied_files,
        }
        manifest_path = staging_dir / "RELEASE_MANIFEST.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        if suspect_hits:
            raise RuntimeError(f"导出发布子集发现疑似真实凭据命中: {list(suspect_hits.keys())}")

        # 校验成功后，将暂存目录事务性替换到正式目标目录
        # 若目标目录已存在，先重命名至备份目录，替换成功后再清理；若替换失败则自动回滚恢复
        backup_dir = None
        if dest_dir.exists():
            backup_dir = dest_dir.parent / f".tmp-release-backup-{dest_dir.name}-{os.getpid()}-{int(time.time() * 1000)}"
            if backup_dir.exists():
                _rmtree_force(backup_dir)
            try:
                dest_dir.rename(backup_dir)
            except OSError:
                shutil.move(str(dest_dir), str(backup_dir))

        try:
            try:
                staging_dir.rename(dest_dir)
            except OSError:
                # 跨设备或重命名失败回退
                shutil.move(str(staging_dir), str(dest_dir))
        except Exception:
            # 替换失败，全力回滚恢复旧发布目录
            if backup_dir is not None and backup_dir.exists():
                if dest_dir.exists():
                    _rmtree_force(dest_dir)
                try:
                    backup_dir.rename(dest_dir)
                except OSError:
                    shutil.move(str(backup_dir), str(dest_dir))
            raise

        # 替换成功，安全清理临时备份
        if backup_dir is not None and backup_dir.exists():
            _rmtree_force(backup_dir)

        return manifest

    except Exception:
        _rmtree_force(staging_dir)
        raise


def main() -> int:
    ap = argparse.ArgumentParser(description="导出干净的 DAG-agent 发布子集")
    ap.add_argument(
        "--out",
        default=str(ROOT / "release_subset" / "dag-agent-v3.2.1"),
        help="导出目录路径（默认 release_subset/dag-agent-v3.2.0）",
    )
    ap.add_argument(
        "--overwrite",
        action="store_true",
        help="允许覆盖经验证的既有发布子集目录",
    )
    ap.add_argument(
        "--require-clean",
        action="store_true",
        help="要求全部发布文件与 HEAD 原始字节一致（含换行），并记录来源提交",
    )
    args = ap.parse_args()
    manifest = export_release_subset(
        ROOT, Path(args.out), overwrite=args.overwrite,
        require_clean=args.require_clean,
    )
    summary = {
        "ok": manifest["suspect_real_hits_count"] == 0,
        "release_dir": str(Path(args.out).resolve()),
        "version": manifest["version"],
        "source_commit": manifest["source_commit"],
        "source_clean_verified": manifest["source_clean_verified"],
        "file_count": manifest["file_count"],
        "total_bytes": manifest["total_bytes"],
        "suspect_real_hits_count": manifest["suspect_real_hits_count"],
        "excluded_roots": manifest["excluded_roots"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
