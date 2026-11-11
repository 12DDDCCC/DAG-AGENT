# -*- coding: utf-8 -*-
"""v3.2.1 发布扫描与严格导出回归（全部使用合成凭据）。"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCANNER = ROOT / "scripts" / "publication_scan.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, check=True,
    )


def _repo_with_file(rel: str, text: str, tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "offline@example.invalid")
    _git(repo, "config", "user.name", "Offline Fixture")
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(repo, "add", "--", rel)
    _git(repo, "commit", "-qm", "synthetic fixture")
    return repo


def _run_scan(repo: Path, out: Path):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [sys.executable, str(SCANNER), "--repo", str(repo),
         "--skip-history", "--out", str(out)],
        cwd=repo.parent, env=env, capture_output=True, text=True,
    )


def test_publication_scan_self_test_cli_is_standalone(tmp_path):
    out = tmp_path / "self-test-must-not-write-report.json"
    proc = subprocess.run(
        [sys.executable, str(SCANNER), "--self-test",
         "--repo", str(tmp_path), "--out", str(out)],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["all"] is True
    assert not out.exists()


def test_publication_scan_cli_fails_closed_and_allows_only_exact_fixture(tmp_path):
    secret = "C" * 49
    repo = _repo_with_file("config.json", json.dumps({"apiKey": secret}), tmp_path)
    out = tmp_path / "suspect.json"
    proc = _run_scan(repo, out)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert secret not in proc.stdout + proc.stderr + out.read_text(encoding="utf-8")
    assert json.loads(out.read_text(encoding="utf-8"))["ok"] is False

    allow_repo = _repo_with_file(
        "tests/v3/test_v56_cli_backends.py",
        'API_KEY="never-persist"\n',
        tmp_path / "allow-case",
    )
    allow_out = tmp_path / "allow.json"
    allowed = _run_scan(allow_repo, allow_out)
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    report = json.loads(allow_out.read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert not report["hits"]
    assert report["verified_synthetic_test_hits"]


def test_publication_scan_main_rejects_unreadable_and_timeout(tmp_path, monkeypatch):
    scanner = _load("publication_scan_main_gate", SCANNER)
    base = {
        "scope": {}, "hits": {},
        "skipped": {"unreadable": 0}, "history": {}, "errors": [],
    }
    monkeypatch.setattr(sys, "argv", [
        "publication_scan.py", "--repo", str(tmp_path),
        "--out", str(tmp_path / "report.json"), "--skip-history",
    ])

    for mutate in (
        lambda rep: rep["skipped"].__setitem__("unreadable", 1),
        lambda rep: rep["history"].__setitem__("timeout", True),
        lambda rep: rep["errors"].append("index_scan_timeout"),
    ):
        report = json.loads(json.dumps(base))
        mutate(report)
        monkeypatch.setattr(scanner, "scan_repo", lambda *a, _rep=report, **kw: _rep)
        assert scanner.main() == 1


def _minimal_export_source(tmp_path: Path) -> Path:
    src = tmp_path / "source"
    src.mkdir()
    (src / "pyproject.toml").write_bytes(
        b'[project]\nname = "dag-agent"\nversion = "3.2.1"\n',
    )
    (src / "README.md").write_bytes(b"public readme\n")
    (src / "LICENSE").write_bytes(b"MIT\n")
    (src / "mcp_server_v3.py").write_bytes(b"# entry\n")
    (src / "dag_agent").mkdir()
    (src / "dag_agent" / "core.py").write_bytes(b"VALUE = 1\n")
    _git(src, "init", "-q")
    _git(src, "config", "user.email", "offline@example.invalid")
    _git(src, "config", "user.name", "Offline Fixture")
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "release source")
    return src


def test_export_required_inputs_version_and_strict_source_bytes(tmp_path, monkeypatch):
    exporter = _load("export_release_gates", ROOT / "scripts" / "export_release_subset.py")
    monkeypatch.setattr(exporter, "INCLUDE_DIRS", ("dag_agent",))
    monkeypatch.setattr(exporter, "INCLUDE_OUTPUT_DOCS", ())
    monkeypatch.setattr(exporter, "REQUIRED_OUTPUT_DOCS", ())
    monkeypatch.setattr(
        exporter, "INCLUDE_ROOT_FILES",
        ("pyproject.toml", "README.md", "LICENSE", "mcp_server_v3.py"),
    )
    monkeypatch.setattr(
        exporter, "REQUIRED_ROOT_FILES", exporter.INCLUDE_ROOT_FILES,
    )
    monkeypatch.setattr(exporter, "REQUIRED_DIRS", ())

    src = _minimal_export_source(tmp_path)
    expected_commit = _git(src, "rev-parse", "HEAD").stdout.decode().strip()
    out = tmp_path / "release"
    manifest = exporter.export_release_subset(src, out, require_clean=True)
    assert manifest["version"] == "3.2.1"
    assert manifest["source_commit"] == expected_commit
    assert manifest["source_clean_verified"] is True
    assert manifest["file_count"] == len(manifest["files"]) == 5
    assert "release_dir" not in manifest

    verifier = _load("verify_release_gates", ROOT / "scripts" / "verify_release.py")
    verified = verifier.verify_release(
        out, source_repo=src, expected_commit=expected_commit)
    assert verified["file_count"] == 5
    with pytest.raises(RuntimeError, match="来源提交身份不符"):
        verifier.verify_release(out, source_repo=src, expected_commit="0" * 40)

    (src / "README.md").write_bytes(b"public readme\r\n")
    with pytest.raises(RuntimeError, match="逐字节|差异"):
        exporter.export_release_subset(src, tmp_path / "strict-reject", require_clean=True)

    normalized_only = exporter.export_release_subset(src, tmp_path / "normalized-only")
    assert normalized_only["source_clean_verified"] is False
    with pytest.raises(RuntimeError, match="逐字节|差异"):
        verifier.verify_release(tmp_path / "normalized-only", source_repo=src)

    (src / "README.md").write_bytes(b"public readme\n")
    (src / "dag_agent" / "core.py").unlink()
    with pytest.raises(RuntimeError, match="集合|缺失"):
        exporter.export_release_subset(src, tmp_path / "deleted-input", require_clean=True)


def test_export_missing_required_input_fails_before_writing(tmp_path, monkeypatch):
    exporter = _load("export_required_inputs", ROOT / "scripts" / "export_release_subset.py")
    src = _minimal_export_source(tmp_path)
    (src / "LICENSE").unlink()
    monkeypatch.setattr(exporter, "INCLUDE_DIRS", ())
    monkeypatch.setattr(exporter, "INCLUDE_OUTPUT_DOCS", ())
    monkeypatch.setattr(exporter, "REQUIRED_OUTPUT_DOCS", ())
    monkeypatch.setattr(exporter, "REQUIRED_DIRS", ())
    dest = tmp_path / "must-not-exist"
    with pytest.raises(ValueError, match="必需"):
        exporter.export_release_subset(src, dest)
    assert not dest.exists()


def test_public_docs_links_version_and_current_capability():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    full_guide = (ROOT / "techstack_full_v2" / "FULL_TASK_GUIDE.md").read_text(
        encoding="utf-8")
    user_guide = ROOT / "outputs" / "DAG-agent-技术预览版使用指南-2026-11-08.md"
    release_note = ROOT / "outputs" / "DAG-agent-v3.2.1-公开发布说明.md"

    assert "D:\\AI\\DAG-agent" not in readme
    assert "run_pytest_v3.py" not in readme
    assert "check_v32_" not in readme
    assert "dag-agent-full50 --mode zero-model" in readme
    assert "provider_pause_ack=True" in readme
    assert "44" not in full_guide
    assert "verify_final_report_complete.py" not in full_guide
    assert "50" in full_guide and "12" in full_guide

    project = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = project["version"]
    from dag_agent import __version__
    assert version == __version__ == "3.2.1"
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    assert "mcp>=1.0.0,<2.0.0" in project["dependencies"]
    assert (ROOT / "SECURITY.md").is_file()
    assert (ROOT / "CONTRIBUTING.md").is_file()
    assert (ROOT / "MANIFEST.in").is_file()

    for doc in (ROOT / "README.md", user_guide, release_note):
        text = doc.read_text(encoding="utf-8")
        for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", text):
            if "://" in target or target.startswith("#"):
                continue
            assert (doc.parent / target.split("#", 1)[0]).resolve().is_file(), (
                f"{doc.relative_to(ROOT)} 存在断链：{target}")

    for script in (
        ROOT / "scripts" / "verify_release.py",
        ROOT / "scripts" / "verify_installed.py",
    ):
        ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8")
    assert "windows-latest" in ci and "ubuntu-latest" in ci
    assert "release/*" in ci
    for workflow_text in (ci, release):
        assert "Create clean-checkout working directories" in workflow_text
        assert "mkdir -p work/" in workflow_text
        assert "PYTHONUTF8: '1'" in workflow_text
    assert "verify_installed.py" in ci
    assert "tags: ['v*']" in release
    assert "--require-clean" in release and "SHA256SUMS" in release
    assert "--expected-commit $tagCommit" in release
    assert '$env:GITHUB_REF_NAME -ne "v$($match.Groups[1].Value)"' in release
    assert "release-regular.xml" in release and "release-slow.xml" in release
    assert "Copy-Item outputs/DAG-agent-v3.2.1-公开发布验收报告.md" not in release
    assert "RELEASE-NOTES-v3.2.1.md" in release
    assert "gh release view $tag -R $repo" in release
    assert "gh release upload $tag $assets --clobber -R $repo" in release
    assert "gh release delete-asset $tag $asset.name -y -R $repo" in release
    assert release.count("gh release create") == 1


def test_publication_scan_includes_untracked_nonignored_files(tmp_path):
    repo = _repo_with_file("README.md", "safe\n", tmp_path)
    (repo / "untracked.json").write_text(
        json.dumps({"apiKey": "U" * 49}), encoding="utf-8")
    out = tmp_path / "untracked-scan.json"
    proc = _run_scan(repo, out)
    assert proc.returncode == 1
    report = json.loads(out.read_text(encoding="utf-8"))
    assert "untracked.json" in report["hits"]
    assert report["scope"]["worktree_files"] == 2


def test_public_fixture_sources_parse_under_python_311():
    for path in sorted((ROOT / "tests" / "fixtures").rglob("*.py")):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path),
                  feature_version=(3, 11))


def test_installed_mcp_checks_have_teeth():
    from dag_agent.acceptance import preflight

    verifier = _load("verify_installed_checks", ROOT / "scripts" / "verify_installed.py")
    for name in ("MCP_ROOT_CHECK", "MCP_LEAF_CHECK"):
        command = getattr(verifier, name)
        ok, why, _ = preflight(check_cmd=command)
        assert ok, f"{name} 空目录体检失败：{why}"
