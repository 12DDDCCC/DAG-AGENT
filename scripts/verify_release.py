# -*- coding: utf-8 -*-
"""严格核验发布目录：完整清单、原始字节及可选来源提交一致性。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.export_release_subset import (  # noqa: E402
    _git_file_bytes, _head_release_files, _validate_dest_path,
)


def verify_release(
    release_dir: Path, source_repo: Path | None = None,
    expected_commit: str | None = None,
) -> dict:
    release_dir = Path(release_dir).resolve()
    if expected_commit is not None and source_repo is None:
        raise ValueError("--expected-commit 必须搭配 --source-repo")
    _validate_dest_path(release_dir.parent, release_dir, overwrite=True)
    manifest = json.loads(
        (release_dir / "RELEASE_MANIFEST.json").read_text(encoding="utf-8"))
    if "release_dir" in manifest:
        raise RuntimeError("清单不得包含生成机器的绝对 release_dir")

    if source_repo is not None:
        source_repo = Path(source_repo).resolve()
        commit = manifest.get("source_commit", "")
        if not isinstance(commit, str) or len(commit) != 40:
            raise RuntimeError("严格来源核验要求清单记录完整 source_commit")
        if expected_commit is not None and commit != expected_commit:
            raise RuntimeError(
                f"来源提交身份不符：manifest={commit}，expected={expected_commit}")
        expected_files = _head_release_files(source_repo, commit)
        if set(manifest["files"]) != expected_files:
            raise RuntimeError("发布文件集合与来源提交存在双向差异")
        for rel_path in sorted(manifest["files"]):
            source_raw = _git_file_bytes(source_repo, commit, rel_path)
            release_raw = (release_dir / rel_path).read_bytes()
            if source_raw != release_raw:
                raise RuntimeError(
                    f"发布文件与来源提交存在原始字节差异（含换行）：{rel_path}")

    return {
        "ok": True,
        "version": manifest["version"],
        "source_commit": manifest.get("source_commit"),
        "source_verified": source_repo is not None,
        "file_count": manifest["file_count"],
        "total_bytes": manifest["total_bytes"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("--source-repo", type=Path)
    parser.add_argument("--expected-commit")
    args = parser.parse_args()
    try:
        result = verify_release(
            args.release_dir, source_repo=args.source_repo,
            expected_commit=args.expected_commit,
        )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
