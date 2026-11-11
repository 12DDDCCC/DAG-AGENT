"""v3.6 模块化接口视图与依赖管理（M1 受控零模型试点，2026-10-13）。

核心契约（基于 Codex 评审修订建议）：
1. 权威快照不变：完整、不可变的代码/数据/运行依赖，绑定 source, version, snapshot ID, manifest 与逐文件 SHA。
2. 接口展示视图：结构化接口门面 (interfaces/<module>/api.py 与 API.md)，提供签名、schema、单位、空值、错误码与样例。
3. 依赖目录隔离：运行依赖置于 deps/<module>/v<version>/，不宣称物理黑盒隔离（同进程/目录可读），作为确定性组织方式。
4. 确定性合并与冲突：同名同哈希确定性复用并留痕全部来源；同名异哈希显式抛出 ConflictError，严禁静默覆盖。
5. 不可变依赖防篡改：整合或执行过程中若下游篡改 deps/ 依赖文件，提交前/重放核验立即拦截。
6. 自包含重放：交付包完整捆绑依赖与整合入口，支持在干净独立临时目录下 100% 独立重放与验收。
7. A/B 计量入口：结构化记录 input/output/cache、cost、tools、disk、wall、failures，缺项标记为 'unknown'，拒绝凭空估算。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sys
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

from .artifacts import manifest_hash_of


class ModularError(Exception):
    """模块化接口视图异常基类。"""
    pass


class ConflictError(ModularError):
    """同名异哈希冲突异常：拒绝静默覆盖。"""
    pass


class IntegrityError(ModularError):
    """依赖完整性损坏异常：文件缺失或内容被篡改。"""
    pass


@dataclass
class InterfaceContract:
    """接口展示契约（门面模型），软预算规范。"""
    module_name: str
    version: int
    snapshot_id: int
    source_node: str
    manifest_hash: str
    entry_symbol: str
    signature: str
    docstring: str = ""
    inputs_schema: dict = field(default_factory=dict)
    outputs_schema: dict = field(default_factory=dict)
    units: dict = field(default_factory=dict)
    null_semantics: dict = field(default_factory=dict)
    error_codes: list[str] = field(default_factory=list)
    side_effects: str = "只读计算/无写盘/无网络"
    examples: list[dict] = field(default_factory=list)
    selftest_cmd: str = ""
    deps_rel_path: str = ""
    api_code: str = ""

    def to_markdown(self) -> str:
        """生成 Markdown 格式的契约文档，长度适中，完整保留语义。"""
        lines = [
            f"# Interface Contract: {self.module_name} (v{self.version})",
            "",
            f"- **Source Node**: `{self.source_node}`",
            f"- **Snapshot ID**: `{self.snapshot_id}`",
            f"- **Manifest Hash**: `{self.manifest_hash}`",
            f"- **Dependencies Root**: `{self.deps_rel_path}`",
            "",
            "## 1. Entry & Signature",
            "```python",
            self.signature,
            "```",
            self.docstring.strip() if self.docstring else "",
            "",
            "## 2. Schemas & Business Semantics",
            f"- **Inputs**: `{json.dumps(self.inputs_schema, ensure_ascii=False)}`",
            f"- **Outputs**: `{json.dumps(self.outputs_schema, ensure_ascii=False)}`",
            f"- **Units**: `{json.dumps(self.units, ensure_ascii=False)}`",
            f"- **Null Semantics**: `{json.dumps(self.null_semantics, ensure_ascii=False)}`",
            f"- **Side Effects**: {self.side_effects}",
            "",
            "## 3. Error Codes",
            ", ".join(f"`{c}`" for c in self.error_codes) if self.error_codes else "None",
            "",
            "## 4. Examples",
        ]
        if self.examples:
            for i, ex in enumerate(self.examples, 1):
                lines.append(f"### Example {i}")
                lines.append("```json")
                lines.append(json.dumps(ex, ensure_ascii=False, indent=2))
                lines.append("```")
        else:
            lines.append("No examples provided.")

        if self.selftest_cmd:
            lines.extend([
                "",
                "## 5. Self-test Command",
                "```bash",
                self.selftest_cmd,
                "```"
            ])
        return "\n".join(lines) + "\n"


def sha256_file(p: Path) -> str:
    """计算单个文件的 SHA256 摘要。"""
    return hashlib.sha256(p.read_bytes()).hexdigest()


def seed_interface_view(
    store,
    snapshots: list[Any],
    dest_root: Path,
    contracts: Optional[dict[str, InterfaceContract]] = None,
    dedup: bool = True
) -> dict:
    """按接口优先展示视图播种：
    - dest_root/interfaces/<module>/api.py 与 API.md
    - dest_root/deps/<module>/v<version>/...
    - 统一合并与去重：同名同哈希复用并留痕来源；同名异哈希显式冲突。
    - 生成 view_manifest.json 记录全量视图哈希与来源绑定。
    - 严格核验快照清单一致性与契约三元组，拒绝伪造与错绑。
    """
    dest_root = Path(dest_root).resolve()
    interfaces_root = dest_root / "interfaces"
    deps_root = dest_root / "deps"
    interfaces_root.mkdir(parents=True, exist_ok=True)
    deps_root.mkdir(parents=True, exist_ok=True)

    contracts = contracts or {}
    view_files: list[dict] = []
    seen_files: dict[str, dict] = {}  # rel_path -> {sha, bytes, sources: [str]}
    contract_mismatches: list[str] = []

    # 1. 逐快照验证并复制不可变依赖到 deps/
    for snap in snapshots:
        node_id = str(snap.node_id)
        version = int(snap.artifact_version)
        snap_id = int(snap.id)
        src_root = store._version_dir(node_id, version)
        files = json.loads(snap.files_json or "[]")

        # 快照自身元数据完整性与权威一致性核验（通用契约）
        files_json_parsed = json.loads(snap.files_json or "[]")
        expected_snap_hash = manifest_hash_of(files_json_parsed) if files_json_parsed else ""
        if getattr(snap, "manifest_hash", "") != expected_snap_hash:
            contract_mismatches.append(
                f"快照 manifest_hash 与清单计算不符: {node_id} (声明={getattr(snap, 'manifest_hash', '')[:8]}, 清单计算={expected_snap_hash[:8]})"
            )
        repo = getattr(store, "repo", None)
        if repo is not None:
            try:
                db_snap = repo.get_snapshot(snap_id)
                if db_snap is None:
                    contract_mismatches.append(f"快照 ID 在任务库中不存在: {snap_id}")
                elif db_snap.manifest_hash != getattr(snap, "manifest_hash", ""):
                    contract_mismatches.append(f"快照 manifest_hash 与任务库权威记录不符: {snap_id}")
                elif str(db_snap.node_id) != node_id:
                    contract_mismatches.append(f"快照 node_id ({node_id}) 与任务库权威记录 ({db_snap.node_id}) 不符")
            except Exception:
                pass

        # 契约三元组与快照一致性核验记录
        if node_id in contracts:
            con = contracts[node_id]
            if con.source_node != node_id:
                contract_mismatches.append(f"契约 source_node ({con.source_node}) 与快照 ({node_id}) 不匹配")
            if con.version != version:
                contract_mismatches.append(f"契约版本 ({con.version}) 与快照版本 ({version}) 不匹配")
            if con.snapshot_id != snap_id:
                contract_mismatches.append(f"契约 snapshot_id ({con.snapshot_id}) 与快照 ID ({snap_id}) 不匹配")
            if con.manifest_hash != getattr(snap, "manifest_hash", ""):
                contract_mismatches.append(f"契约 manifest_hash 与快照不匹配")

        # 依赖模块子目录
        mod_deps_dir = deps_root / node_id / f"v{version}"
        mod_deps_dir.mkdir(parents=True, exist_ok=True)

        for f in files:
            rel_path = f["rel_path"]
            expected_sha = f["sha256"]
            src_file = src_root / rel_path

            if not src_file.is_file():
                raise IntegrityError(f"快照文件缺失: {node_id}:{rel_path}")
            actual_sha = sha256_file(src_file)
            if actual_sha != expected_sha:
                raise IntegrityError(f"快照文件摘要不符: {node_id}:{rel_path}")

            # 复制到 deps/<node_id>/v<version>/<rel_path>
            target_dep_file = mod_deps_dir / rel_path
            target_dep_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, target_dep_file)

            dep_view_rel = f"deps/{node_id}/v{version}/{rel_path}"
            view_files.append({
                "rel_path": dep_view_rel,
                "role": "dependency",
                "source_node": node_id,
                "version": version,
                "snapshot_id": snap_id,
                "sha256": actual_sha,
                "size": src_file.stat().st_size,
                "snap_item": f
            })

            # 冲突与去重核验
            if rel_path in seen_files:
                prior = seen_files[rel_path]
                if prior["sha"] != actual_sha:
                    raise ConflictError(
                        f"同名异哈希冲突: '{rel_path}' 在来源 {prior['sources']} "
                        f"(sha={prior['sha'][:8]}) 与 {node_id}@v{version} "
                        f"(sha={actual_sha[:8]}) 冲突！"
                    )
                prior["sources"].append(f"{node_id}@v{version}")
            else:
                seen_files[rel_path] = {
                    "sha": actual_sha,
                    "bytes": src_file.stat().st_size,
                    "sources": [f"{node_id}@v{version}"]
                }

    # 2. 为声明了契约的模块生成 interfaces/<module>/
    seen_facades: dict[str, str] = {}
    for node_id, contract in contracts.items():
        # 路径越界逃逸校验（禁止 ../ 或绝对路径逃逸出 interfaces_root）
        mod_dir = (interfaces_root / contract.module_name).resolve()
        if not mod_dir.is_relative_to(interfaces_root) or mod_dir == interfaces_root:
            contract_mismatches.append(f"非法的模块名/路径越界逃逸: {contract.module_name}")
            continue

        # 同名门面覆盖冲突校验
        if contract.module_name in seen_facades:
            prior_node = seen_facades[contract.module_name]
            if prior_node != node_id:
                raise ConflictError(
                    f"同名门面冲突: 模块 '{contract.module_name}' 已由节点 {prior_node} 提供，节点 {node_id} 试图覆盖！"
                )
        seen_facades[contract.module_name] = node_id

        mod_dir.mkdir(parents=True, exist_ok=True)

        # 写入 API.md
        doc_path = mod_dir / "API.md"
        doc_content = contract.to_markdown()
        doc_path.write_text(doc_content, encoding="utf-8")
        doc_sha = sha256_file(doc_path)

        view_files.append({
            "rel_path": f"interfaces/{contract.module_name}/API.md",
            "role": "interface_doc",
            "source_node": node_id,
            "version": contract.version,
            "snapshot_id": contract.snapshot_id,
            "sha256": doc_sha,
            "size": len(doc_content.encode("utf-8"))
        })

        # 写入 api.py 门面（若有）
        if contract.api_code:
            api_path = mod_dir / "api.py"
            api_path.write_text(contract.api_code, encoding="utf-8")
            api_sha = sha256_file(api_path)
            view_files.append({
                "rel_path": f"interfaces/{contract.module_name}/api.py",
                "role": "interface_facade",
                "source_node": node_id,
                "version": contract.version,
                "snapshot_id": contract.snapshot_id,
                "sha256": api_sha,
                "size": len(contract.api_code.encode("utf-8"))
            })

    # 3. 写入 view_manifest.json
    view_files.sort(key=lambda x: (x["rel_path"], str(x.get("source_node", ""))))
    manifest_bytes = json.dumps(view_files, ensure_ascii=False, indent=2).encode("utf-8")
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()

    manifest_data = {
        "view_mode": "interface_first",
        "manifest_hash": manifest_hash,
        "files_count": len(view_files),
        "dedup_sources": {k: v["sources"] for k, v in seen_files.items()},
        "contract_mismatches": contract_mismatches,
        "files": view_files
    }

    manifest_path = dest_root / "view_manifest.json"
    manifest_path.write_text(json.dumps(manifest_data, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_data


def verify_immutable_deps(dest_root: Path, view_manifest: dict) -> tuple[bool, list[str]]:
    """防篡改核验：
    1. 验证 view_manifest 自身完整性与哈希一致性（manifest_hash 为必需字段）；
    2. 验证 dest_root 下所有门面、文档与依赖文件未被下游修改、篡改或删除；
    3. 验证门面契约文档声明的 Manifest Hash 与当前实际依赖文件完全一致（防联合篡改）；
    4. 扫描 deps/ 与 interfaces/ 目录，拦截未在清单声明的注入文件；
    5. 核验契约三元组与快照身份一致性。
    """
    dest_root = Path(dest_root).resolve()
    mismatches = []
    files = view_manifest.get("files", [])

    # 1. 验证清单自身的完整性（必需字段与内容一致性）
    expected_mhash = view_manifest.get("manifest_hash")
    if not expected_mhash:
        mismatches.append("view_manifest 缺失必需的 manifest_hash 字段")
    else:
        sorted_files = sorted(files, key=lambda x: (x["rel_path"], str(x.get("source_node", ""))))
        computed_mhash = hashlib.sha256(
            json.dumps(sorted_files, ensure_ascii=False, indent=2).encode("utf-8")
        ).hexdigest()
        if computed_mhash != expected_mhash:
            mismatches.append(f"view_manifest 清单自身哈希不匹配: 记录 {expected_mhash[:8]} != 重新计算 {computed_mhash[:8]}")

    # 契约核验错误
    if view_manifest.get("contract_mismatches"):
        mismatches.extend(view_manifest["contract_mismatches"])

    manifest_paths = set()
    # 2. 验证所有记录的文件（门面和依赖）
    for item in files:
        rel = item["rel_path"]
        manifest_paths.add(rel)
        expected_sha = item["sha256"]
        target = dest_root / rel
        if not target.is_file():
            mismatches.append(f"文件缺失: {rel}")
        else:
            actual_sha = sha256_file(target)
            if actual_sha != expected_sha:
                mismatches.append(f"文件被篡改: {rel} (预期 {expected_sha[:8]}, 实际 {actual_sha[:8]})")

    # 3. 跨核验：检查门面声明的权威快照哈希与实际依赖文件是否吻合（防下游联合重写 manifest 与依赖）
    interfaces_dir = dest_root / "interfaces"
    if interfaces_dir.is_dir():
        import re
        for api_md in interfaces_dir.rglob("API.md"):
            try:
                md_text = api_md.read_text(encoding="utf-8")
                m_node = re.search(r"\- \*\*Source Node\*\*: `([^`]+)`", md_text)
                m_hash = re.search(r"\- \*\*Manifest Hash\*\*: `([0-9a-fA-F]+)`", md_text)
                m_ver = re.search(r"# Interface Contract: [^(]+\(v(\d+)\)", md_text)
                if m_node and m_hash:
                    src_node = m_node.group(1)
                    decl_hash = m_hash.group(1)
                    ver = m_ver.group(1) if m_ver else "1"
                    dep_prefix = f"deps/{src_node}/v{ver}/"
                    node_dep_files = [f for f in files if f.get("role") == "dependency" and f.get("rel_path", "").startswith(dep_prefix)]
                    if node_dep_files:
                        snap_items = []
                        for df in node_dep_files:
                            pure_rel = df["rel_path"][len(dep_prefix):]
                            actual_p = dest_root / df["rel_path"]
                            act_sha = sha256_file(actual_p) if actual_p.is_file() else df.get("sha256", "")
                            act_size = actual_p.stat().st_size if actual_p.is_file() else df.get("size", 0)
                            base_item = dict(df.get("snap_item") or {})
                            base_item["rel_path"] = pure_rel
                            base_item["sha256"] = act_sha
                            base_item["size"] = act_size
                            snap_items.append(base_item)
                        act_snap_hash = manifest_hash_of(snap_items)
                        if act_snap_hash != decl_hash:
                            mismatches.append(
                                f"依赖快照实际计算摘要 ({act_snap_hash[:8]}) 与门面声明 ({decl_hash[:8]}) 不符，依赖已被篡改！"
                            )
            except Exception:
                pass

    # 4. 扫描 deps/ 和 interfaces/，拦截未在清单中的非法注入文件
    for d in ("deps", "interfaces"):
        sub_root = dest_root / d
        if sub_root.is_dir():
            for p in sub_root.rglob("*"):
                if p.is_file():
                    rel_posix = p.relative_to(dest_root).as_posix()
                    if "__pycache__" not in p.parts and rel_posix not in manifest_paths:
                        mismatches.append(f"未声明的注入文件: {rel_posix}")

    return (len(mismatches) == 0, mismatches)


def bundle_for_standalone_replay(
    dest_root: Path,
    output_dir: Path,
    integration_files: Optional[list[str]] = None,
    fail_closed: bool = True
) -> Path:
    """将整合产物与运行依赖捆绑为自包含的独立交付包，支持在干净独立临时目录下 100% 独立重放与验收。
    保证：
    1. 在独立 staging 临时目录中组装，避免污染或半成品泄露；
    2. 校验依赖完整性与防篡改，若有损坏 fail-closed 拒发布；
    3. 校验 integration_files 与依赖之间无覆盖冲突（同名同哈希复用，同名异哈希抛 ConflictError）；
    4. 原子发布到 output_dir。
    """
    dest_root = Path(dest_root).resolve()
    output_dir = Path(output_dir).resolve()
    staging_dir = output_dir.parent / f".staging_{uuid.uuid4().hex[:8]}"
    staging_dir.mkdir(parents=True, exist_ok=True)

    try:
        manifest_file = dest_root / "view_manifest.json"
        if not manifest_file.is_file():
            if fail_closed:
                raise IntegrityError(f"缺少 view_manifest.json 清单，无法发布: {dest_root}")
            view_meta = {}
        else:
            view_meta = json.loads(manifest_file.read_text(encoding="utf-8"))

        # 依赖与视图完整性自检
        if view_meta and fail_closed:
            ok, errs = verify_immutable_deps(dest_root, view_meta)
            if not ok:
                raise IntegrityError(f"依赖完整性损坏或被篡改，拒绝打包发布: {errs}")

        seen_staging: dict[str, str] = {}  # rel_path -> sha256

        # 1. 复制所有 interfaces 门面与文档，以及运行依赖
        for item in view_meta.get("files", []):
            rel = item["rel_path"]
            src_p = dest_root / rel
            if not src_p.is_file():
                if fail_closed:
                    raise IntegrityError(f"清单声明的文件在源视图中缺失: {rel}")
                continue

            # 复制原路径结构
            tgt_p = staging_dir / rel
            tgt_p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_p, tgt_p)
            seen_staging[rel] = sha256_file(src_p)

            # 若是依赖文件，也在根目录平铺一份（兼容不同 import 风格）
            if item.get("role") == "dependency":
                parts = Path(rel).parts
                if len(parts) >= 4 and parts[0] == "deps":
                    pure_rel = Path(*parts[3:])
                else:
                    pure_rel = Path(rel)
                flat_p = staging_dir / pure_rel
                flat_rel_str = str(pure_rel).replace("\\", "/")
                src_sha = sha256_file(src_p)
                if flat_rel_str in seen_staging:
                    if seen_staging[flat_rel_str] != src_sha:
                        raise ConflictError(f"平铺依赖冲突: '{flat_rel_str}' 已存在且内容不同")
                else:
                    flat_p.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_p, flat_p)
                    seen_staging[flat_rel_str] = src_sha

        # 2. 复制整合文件
        if integration_files:
            for f in integration_files:
                src = dest_root / f
                if not src.is_file():
                    if fail_closed:
                        raise IntegrityError(f"指定的整合文件缺失: {f}")
                    continue
                f_rel_str = str(Path(f)).replace("\\", "/")
                src_sha = sha256_file(src)
                if f_rel_str in seen_staging:
                    if seen_staging[f_rel_str] != src_sha:
                        raise ConflictError(f"整合文件试图覆盖冻结依赖且内容不同: '{f_rel_str}'")
                else:
                    tgt = staging_dir / f
                    tgt.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, tgt)
                    seen_staging[f_rel_str] = src_sha
        else:
            for p in dest_root.rglob("*"):
                if p.is_file():
                    rel = p.relative_to(dest_root)
                    if not any(part in ("deps", "interfaces", ".git", "__pycache__") for part in rel.parts) and rel.name != "view_manifest.json":
                        p_rel_str = str(rel).replace("\\", "/")
                        src_sha = sha256_file(p)
                        if p_rel_str in seen_staging:
                            if seen_staging[p_rel_str] != src_sha:
                                raise ConflictError(f"整合文件试图覆盖冻结依赖且内容不同: '{p_rel_str}'")
                        else:
                            tgt = staging_dir / rel
                            tgt.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(p, tgt)
                            seen_staging[p_rel_str] = src_sha

        # 3. 原子发布到 output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        for sp in staging_dir.rglob("*"):
            if sp.is_file():
                rel = sp.relative_to(staging_dir)
                out_p = output_dir / rel
                out_p.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(sp, out_p)
        return output_dir
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def prepare_ab_receipt(
    condition: str,
    target: str,
    input_tokens: Any = "unknown",
    output_tokens: Any = "unknown",
    cache_read_tokens: Any = "unknown",
    cache_creation_tokens: Any = "unknown",
    reported_cost: Any = "unknown",
    tool_uses: Any = "unknown",
    disk_read_files: Any = "unknown",
    disk_read_bytes: Any = "unknown",
    wall_time_s: Any = "unknown",
    failures: int = 0,
    rescues: int = 0,
    verdict: str = "pass",
    note: str = ""
) -> dict:
    """规范化 A/B 结构化计量条目：
    A=全量视图，B=接口优先＋完整依赖；未实测字段严格标记为 'unknown'，拒绝凭空估算。
    """
    assert condition in ("A", "B"), "condition 必须为 'A' 或 'B'"
    receipt = {
        "condition": condition,
        "condition_name": "full_view" if condition == "A" else "interface_first_with_full_deps",
        "target": target,
        "tokens": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_creation_tokens": cache_creation_tokens,
        },
        "cost": {
            "reported_amount": reported_cost,
            "currency": "USD" if reported_cost != "unknown" else "unknown"
        },
        "tool_uses": tool_uses,
        "disk_io": {
            "read_files": disk_read_files,
            "read_bytes": disk_read_bytes
        },
        "performance": {
            "wall_time_s": wall_time_s
        },
        "reliability": {
            "failures": failures,
            "rescues": rescues,
            "verdict": verdict
        },
        "note": note or "未实测字段严格标记为 unknown，不作无依据比例推断"
    }
    return receipt
