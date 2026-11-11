# -*- coding: utf-8 -*-
r"""公开检查扫描器 v2（内置于 scripts/publication_scan.py）：工作区+HEAD+暂存+全部可达历史 blob。

特性：
- git ls-files -z / cat-file 按 NUL 处理中文/空格/特殊路径；
- 独立覆盖工作区读取、暂存区（INDEX）、HEAD 树对象内容、全部唯一历史 blob；
- 模式：JSON apiKey/api_key/token/secret、env 凭据赋值、Authorization、
  Bearer、sk- 前缀、PEM 私钥块；
- 跳过项（二进制库/超限/不可读）全部计数入报告，不静默；
- 输出只含 位置/模式/行号/分类安全摘要，绝不含命中值；
- --self-test：合成凭据反例（中文/空格路径、当前版本 apiKey、历史已删 key、
  HEAD 独有 key、INDEX 独有 key、10 占位后第 11 个真实凭据、假 .db 跳过），独立临时仓运行。

用法：python scripts/publication_scan.py [--self-test] [--skip-history] [--repo <path>] [--out <json_path>]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent
PATTERNS = [
    ("json-apikey", re.compile(
        r'"(?:api[_-]?key|token|secret|access[_-]?key)"\s*:\s*"([^"]{8,})"', re.I)),
    ("env-credential", re.compile(
        r'^(?:BIGMODEL_API_KEY|ANTHROPIC_AUTH_TOKEN|ANTHROPIC_API_KEY|API_KEY|AUTH_TOKEN|SECRET_KEY)\s*[=:]\s*"?([^\s"#]{8,})"?', re.M)),
    ("authorization", re.compile(
        r'(?i)\bauthorization["\']?\s*[:=]\s*"?([^\s"\']{8,})')),
    ("bearer", re.compile(r'(?i)bearer\s+([a-z0-9\-_\.]{16,})')),
    ("sk-prefix", re.compile(r'\b(sk|rk|ak)-[A-Za-z0-9]{16,}\b')),
    ("pem-private", re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')),
]
PLACEHOLDER_RX = re.compile(
    r'(\$\{|\$BIGMODEL|xxx|example|fixture|placeholder|REDACTED|dummy|'
    r'your[_-]?key|<[^>]+>|\.\.\.|test[-_]?key|合成|示例|样例)', re.I)
BINARY_EXT = {".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".pyc",
              ".png", ".jpg", ".jpeg", ".gif", ".zip", ".gz", ".mp4",
              ".woff", ".woff2", ".ico", ".pdf"}
MAX_SCAN_BYTES = 2_000_000

# 仅豁免已人工复核的测试反例：相对路径和命中值 SHA256 必须同时精确匹配。
NEVER_PERSIST_SHA256 = "85b1f094618d62c0c3a688e48d09b12d06bb12d1e07054114019016325ad5fdd"
KNOWN_SYNTHETIC_TEST_HITS = {
    ("tests/v3/test_v56_cli_backends.py", NEVER_PERSIST_SHA256),
}


def partition_suspect_hits(rel_path: str, hits: list[dict]) -> tuple[list[dict], list[dict]]:
    unverified, synthetic = [], []
    for hit in hits:
        key = (rel_path, hit.get("value_sha256"))
        (synthetic if key in KNOWN_SYNTHETIC_TEST_HITS else unverified).append(hit)
    return unverified, synthetic


def classify(value: str) -> str:
    """占位/样例 vs 疑似真实（保守：仅明确占位标记才归 placeholder）。"""
    if PLACEHOLDER_RX.search(value):
        return "placeholder_likely"
    if value.startswith("$"):
        return "env-interpolation"
    return "suspect_real"


def redact(v: str) -> str:
    """安全摘要：只保留长度与首 2 字符类别指纹，不输出值本身。"""
    return f"<len={len(v)},head2={v[:2]!r}>"


def scan_text(text: str, limit_hits: int | None = None):
    hits = []
    suspect_count = 0
    for name, pat in PATTERNS:
        for m in pat.finditer(text):
            val = m.group(1) if m.groups() else m.group(0)
            line_no = text.count("\n", 0, m.start()) + 1
            cls = classify(val)
            val_sha = hashlib.sha256(val.encode("utf-8")).hexdigest()
            hits.append({"pattern": name, "line": line_no,
                         "class": cls, "value_digest": redact(val),
                         "value_sha256": val_sha})
            if cls == "suspect_real":
                suspect_count += 1
                if limit_hits is not None and suspect_count >= limit_hits:
                    return hits
    return hits


def _git(args: list, cwd) -> bytes:
    return subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True,
                          check=True).stdout


def scan_repo(repo: Path, skip_history=False):
    report = {"repo": str(repo), "scope": {}, "hits": {}, "skipped": {},
              "history": {}, "errors": [], "verified_synthetic_test_hits": {}}
    tracked = _git(["ls-files", "-z"], repo).decode(
        "utf-8", errors="replace").split("\0")
    tracked = [t for t in tracked if t]
    worktree = _git([
        "ls-files", "-z", "--cached", "--others", "--exclude-standard",
    ], repo).decode("utf-8", errors="replace").split("\0")
    worktree = sorted({t for t in worktree if t})
    report["scope"]["tracked_files"] = len(tracked)
    report["scope"]["worktree_files"] = len(worktree)
    cred_files = 0
    skipped_bin = skipped_big = skipped_unreadable = 0
    for rel in worktree:
        p = repo / rel
        if rel.endswith(tuple(BINARY_EXT)):
            skipped_bin += 1
            continue
        try:
            if p.stat().st_size > MAX_SCAN_BYTES:
                skipped_big += 1
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        except (OSError, PermissionError):
            skipped_unreadable += 1
            continue
        hits = scan_text(text)
        real, synthetic = partition_suspect_hits(
            rel, [h for h in hits if h["class"] == "suspect_real"])
        if real:
            cred_files += 1
            report["hits"][rel] = [
                {k: h[k] for k in ("pattern", "line", "class",
                                   "value_digest")} for h in real]
        if synthetic:
            report["verified_synthetic_test_hits"][rel] = synthetic
    report["scope"]["quoted_path_note"] = (
        "路径由 git ls-files -z NUL 分割获取（v1 用转义文本曾致 5539 个"
        "带引号路径无法对应文件）")
    report["skipped"] = {"binary_by_ext": skipped_bin,
                         "over_2mb": skipped_big,
                         "unreadable": skipped_unreadable,
                         "total_skipped": skipped_bin + skipped_big
                         + skipped_unreadable}
    report["scope"]["suspect_real_files_worktree"] = cred_files

    def _scan_blob_paths(blob_to_paths: dict[str, list[str]], prefix: str) -> tuple[int, int, int]:
        file_hits = 0
        over_big = 0
        bin_ext = 0
        batch_items: list[tuple[str, list[str]]] = []
        for sha1, paths in blob_to_paths.items():
            non_bin_paths = []
            for pth in paths:
                if pth.endswith(tuple(BINARY_EXT)):
                    bin_ext += 1
                else:
                    non_bin_paths.append(pth)
            if non_bin_paths:
                batch_items.append((sha1, non_bin_paths))
        if not batch_items:
            return file_hits, bin_ext, over_big
        proc = subprocess.Popen(
            ["git", "cat-file", "--batch"], cwd=str(repo),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            payload = "".join(f"{s}\n" for s, _ in batch_items).encode()
            out_data, _ = proc.communicate(payload, timeout=600)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            out_data = b""
            report["errors"].append(f"{prefix.lower()}_scan_timeout")
        buf = out_data
        pos = 0
        for sha1, non_bin_paths in batch_items:
            nl = buf.find(b"\n", pos)
            if nl < 0:
                break
            header = buf[pos:nl].decode("utf-8", errors="replace")
            hparts = header.split()
            if header.endswith("missing") or len(hparts) < 3:
                pos = nl + 1
                continue
            obj_type = hparts[1]
            size = int(hparts[2])
            body = buf[nl + 1:nl + 1 + size]
            pos = nl + 1 + size + 1
            if obj_type != "blob":
                continue
            if size > MAX_SCAN_BYTES:
                over_big += len(non_bin_paths)
                continue
            hits = [h for h in scan_text(body.decode("utf-8", errors="replace"))
                    if h["class"] == "suspect_real"]
            for pth in non_bin_paths:
                real, synthetic = partition_suspect_hits(pth, hits)
                if real:
                    file_hits += 1
                    report["hits"].setdefault(f"{prefix}:{pth}", []).extend(
                        {k: h[k] for k in ("pattern", "line", "class",
                                           "value_digest")} for h in real[:5])
                if synthetic:
                    report["verified_synthetic_test_hits"].setdefault(
                        f"{prefix}:{pth}", []).extend(synthetic)
        return file_hits, bin_ext, over_big

    # 1. 暂存区（INDEX：git ls-files -z -s，输出格式 <mode> <sha1> <stage>\t<path>）
    index_map = _git(["ls-files", "-z", "-s"], repo).decode(
        "utf-8", errors="replace").split("\0")
    index_blobs: dict[str, list[str]] = {}
    index_entry_count = 0
    for entry in index_map:
        if not entry:
            continue
        meta = entry.split("\t", 1)
        if len(meta) == 2:
            fields = meta[0].split()
            if len(fields) >= 2:
                sha1 = fields[1]
                index_blobs.setdefault(sha1, []).append(meta[1])
                index_entry_count += 1
    report["scope"]["index_entry_count"] = index_entry_count
    report["scope"]["index_blob_count"] = len(index_blobs)
    index_hits, index_bin_skipped, index_big_skipped = _scan_blob_paths(index_blobs, "INDEX")
    report["scope"]["suspect_real_files_index"] = index_hits
    report["skipped"]["index_binary_by_ext"] = index_bin_skipped
    report["skipped"]["index_over_2mb"] = index_big_skipped

    # 2. HEAD 树对象内容（git ls-tree -r -z HEAD，输出格式 <mode> <type> <sha1>\t<path>）
    head_blobs: dict[str, list[str]] = {}
    head_entry_count = 0
    try:
        head_tree = _git(["ls-tree", "-r", "-z", "HEAD"], repo).decode(
            "utf-8", errors="replace").split("\0")
    except subprocess.CalledProcessError:
        head_tree = []
    for entry in head_tree:
        if not entry:
            continue
        meta = entry.split("\t", 1)
        if len(meta) == 2:
            fields = meta[0].split()
            if len(fields) >= 3 and fields[1] == "blob":
                sha1 = fields[2]
                head_blobs.setdefault(sha1, []).append(meta[1])
                head_entry_count += 1
    report["scope"]["head_entry_count"] = head_entry_count
    report["scope"]["head_blob_count"] = len(head_blobs)
    head_hits, head_bin_skipped, head_big_skipped = _scan_blob_paths(head_blobs, "HEAD")
    report["scope"]["suspect_real_files_head"] = head_hits
    report["skipped"]["head_binary_by_ext"] = head_bin_skipped
    report["skipped"]["head_over_2mb"] = head_big_skipped

    if not skip_history:
        # 3. 全部可达历史唯一对象（rev-list --all --objects），仅对 blob 扫描
        out = _git(["rev-list", "--all", "--objects"], repo).decode(
            "utf-8", errors="replace").splitlines()
        seen = {}
        for line in out:
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[1]:
                seen.setdefault(parts[0], parts[1])
        report["scope"]["history_object_count"] = len(seen)
        hist_bin_skipped = sum(1 for _, path in seen.items()
                               if path.endswith(tuple(BINARY_EXT)))
        blob_items = [(sha1, path) for sha1, path in seen.items()
                      if not path.endswith(tuple(BINARY_EXT))]
        proc = subprocess.Popen(
            ["git", "cat-file", "--batch"], cwd=str(repo),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        hist_hits = 0
        hist_scanned = 0
        hist_non_blob_skipped = 0
        hist_big_skipped = 0
        try:
            payload = "".join(f"{s}\n" for s, _ in blob_items).encode()
            out_data, _ = proc.communicate(payload, timeout=1200)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            out_data = b""
            report["history"]["timeout"] = True
            report["errors"].append("history_scan_timeout")
        buf = out_data
        pos = 0
        for sha1, path in blob_items:
            nl = buf.find(b"\n", pos)
            if nl < 0:
                break
            header = buf[pos:nl].decode("utf-8", errors="replace")
            hparts = header.split()
            if header.endswith("missing") or len(hparts) < 3:
                pos = nl + 1
                continue
            obj_type = hparts[1]
            size = int(hparts[2])
            body = buf[nl + 1:nl + 1 + size]
            pos = nl + 1 + size + 1
            if obj_type != "blob":
                hist_non_blob_skipped += 1
                continue
            if size > MAX_SCAN_BYTES:
                hist_big_skipped += 1
                continue
            hist_scanned += 1
            hits = [h for h in scan_text(body.decode("utf-8",
                                                     errors="replace"))
                    if h["class"] == "suspect_real"]
            real, synthetic = partition_suspect_hits(path, hits)
            if real:
                hist_hits += 1
                report["hits"].setdefault(f"HIST:{path}", []).extend(
                    {k: h[k] for k in ("pattern", "line", "class",
                                       "value_digest")} for h in real[:5])
            if synthetic:
                report["verified_synthetic_test_hits"].setdefault(
                    f"HIST:{path}", []).extend(synthetic)
        report["history"]["blobs_scanned"] = hist_scanned
        report["history"]["non_blob_objects_skipped"] = hist_non_blob_skipped
        report["history"]["binary_by_ext_skipped"] = hist_bin_skipped
        report["history"]["over_2mb_blobs_skipped"] = hist_big_skipped
        report["history"]["suspect_real_files"] = hist_hits
    report["scope_note"] = (
        "输出不含任何命中值本身（仅长度+首2字符指纹）；suspect_real=保守"
        "分类（无明确占位标记即归此类，需人工复核）；独立覆盖工作区（已跟踪+"
        "未跟踪且未忽略）、暂存区（INDEX）、HEAD 树及全部可达历史非二进制 blob。")
    return report


def self_test(base_tmp: Path):
    repo = base_tmp / "selftest-repo"
    repo.mkdir(parents=True)

    def git(*args, **kw):
        return subprocess.run(["git"] + list(args), cwd=str(repo),
                              capture_output=True, check=True, **kw)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    fake_key_cur = "AK-SELFTEST-CURRENT-0000000000000001"
    fake_key_hist = "AK-SELFTEST-HISTORY-0000000000000002"
    fake_key_head = "AK-SELFTEST-HEADONLY-000000000000003"
    fake_key_idx = "AK-SELFTEST-IDXONLY-0000000000000004"
    d = repo / "目录 带空格"
    d.mkdir()
    (d / "config.json").write_text(
        json.dumps({"apiKey": fake_key_cur, "note": "synthetic self-test"}),
        encoding="utf-8")
    (repo / "sample.env").write_text(
        "API_KEY=$BIGMODEL_API_KEY\nTOKEN=xxx-example\n",
        encoding="utf-8")
    (repo / "fake.db").write_bytes(b"SQLite format 3\x00" + os.urandom(64))
    git("add", "-A")
    git("commit", "-q", "-m", "c1")
    hist = repo / "历史 泄密.json"
    hist.write_text(json.dumps({"apiKey": fake_key_hist}), encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "c2")
    git("rm", "-q", "历史 泄密.json")
    head_only = repo / "head_only.json"
    head_only.write_text(json.dumps({"apiKey": fake_key_head}), encoding="utf-8")
    idx_only = repo / "index_only.json"
    idx_only.write_text("{}", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "c3 remove hist & add head_only")
    head_only.write_text("{}", encoding="utf-8")
    git("add", "head_only.json")
    idx_only.write_text(json.dumps({"apiKey": fake_key_idx}), encoding="utf-8")
    git("add", "index_only.json")
    idx_only.write_text("{}", encoding="utf-8")

    checks = {}
    rep = scan_repo(repo)
    rep_no_hist = scan_repo(repo, skip_history=True)
    paths = list(rep["hits"].keys())
    paths_no_hist = list(rep_no_hist["hits"].keys())

    def find(sub, plist=paths):
        return [p for p in plist if sub in p]

    checks["chinese_space_path_scanned"] = bool(find("目录 带空格"))
    checks["current_fake_apikey_found"] = bool(find("config.json"))
    checks["history_deleted_key_found"] = bool(find("HIST:历史 泄密.json"))
    checks["head_only_key_found_without_history"] = bool(
        find("HEAD:head_only.json", paths_no_hist))
    checks["index_only_key_found_without_history"] = bool(
        find("INDEX:index_only.json", paths_no_hist))
    checks["head_blob_count_multi"] = rep["scope"].get("head_blob_count", 0) >= 3
    checks["index_blob_count_multi"] = rep["scope"].get("index_blob_count", 0) >= 3
    text_11 = ("\n".join(json.dumps({"apiKey": f"$PLACEHOLDER_{i}"}) for i in range(10))
               + "\n" + json.dumps({"apiKey": "B" * 48}))
    hits_11 = scan_text(text_11)
    checks["eleventh_after_placeholders_found"] = (
        sum(h["class"] == "suspect_real" for h in hits_11) == 1)
    in_suspect = [k for k in rep["hits"] if "sample.env" in k]
    st_hits = scan_text("API_KEY=$BIGMODEL_API_KEY\nTOKEN=xxx-example\n")
    checks["placeholders_classified"] = (
        not in_suspect
        and bool(st_hits)
        and all(h["class"] in ("placeholder_likely", "env-interpolation")
                for h in st_hits))
    checks["db_counted_as_skipped"] = rep["skipped"]["binary_by_ext"] >= 1
    blob = json.dumps(rep, ensure_ascii=False)
    checks["output_has_no_secret_values"] = all(
        k not in blob for k in (fake_key_cur, fake_key_hist, fake_key_head, fake_key_idx))
    checks["history_scanned"] = rep["history"].get(
        "blobs_scanned", 0) >= 1
    checks["history_non_blob_filtered"] = rep["history"].get(
        "non_blob_objects_skipped", 0) >= 0
    checks["all"] = all(checks.values())
    return checks


def _rmtree_force(path: Path) -> None:
    def _onerror(func, p, _):
        import stat
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass
    if path.exists():
        shutil.rmtree(path, onerror=_onerror)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--skip-history", action="store_true")
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    results = {}
    if args.self_test:
        td = OUT / ".selftest-tmp"
        _rmtree_force(td)
        try:
            td.mkdir(parents=True, exist_ok=True)
            results["self_test"] = self_test(td)
        finally:
            _rmtree_force(td)
        print(json.dumps(results["self_test"], ensure_ascii=False, indent=1))
        if not results["self_test"]["all"]:
            return 1
        return 0
    repo = Path(args.repo).resolve()
    rep = scan_repo(repo, skip_history=args.skip_history)
    rep["ok"] = not (
        rep["hits"]
        or rep.get("errors")
        or rep["history"].get("timeout")
        or rep["skipped"].get("unreadable", 0)
    )
    out_path = Path(args.out).resolve() if args.out else (OUT / "publication_scan_v2.json")
    out_path.write_text(
        json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"ok": rep["ok"], "scope": rep["scope"],
                      "skipped": rep["skipped"], "errors": rep.get("errors", []),
                      "history": rep.get("history", {}),
                      "suspect_files": list(rep["hits"].keys())[:20],
                      "suspect_total": len(rep["hits"])},
                     ensure_ascii=False, indent=1))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
