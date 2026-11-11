"""v3.1 受限枚举验收清单（五只手）+ 考卷体检三关。

设计对应（设计覆盖度审计 §3.2/§3.1）：
- 五动作枚举：出题方只能在 cmd/test/scan/script/http 五种"执行规格的方式"里组合，
  灵活性全部放在参数与脚本（图灵完备的规格数据），判卷端只认退出码。
- 体检三关：考卷上考场前必须 ①能编译（语法+白名单）②能跑起（空目录执行非 ERROR）
  ③咬得动坏产物（空目录执行必须 FAIL——空目录通过=废卷/永绿断言）。
  关②与关③共用同一次空目录执行：ERROR→坏卷；PASS→废卷；FAIL→有牙齿。
"""
from __future__ import annotations

import json
import shlex
import tempfile
from pathlib import Path

from .assessment import WHITELIST, run_check3

# 五只手：平台内置的"执行规格的方式"（固定枚举，出题不可发明新动作）
ACTIONS = ("cmd", "test", "scan", "script", "http")

_CHECKERS_DIR = Path(__file__).resolve().parent / "checkers"

# 每动作必填参数（受限语法：少一个键即拒）
_REQUIRED_PARAMS = {
    "cmd": ("cmd",),
    "test": ("args",),
    "scan": ("dir", "banned"),
    "script": ("path",),
    "http": ("url", "expect_status"),
}


class AcceptanceError(ValueError):
    """受限语法违规：出题方只能在枚举内组合。"""


def validate(items: list[dict]) -> tuple[bool, str]:
    """受限语法校验。返回 (ok, why)。"""
    if not isinstance(items, list) or not items:
        return False, "验收清单须为非空列表"
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            return False, f"条目{i}须为对象"
        action = it.get("action")
        if action not in ACTIONS:
            return False, f"条目{i}动作 {action!r} 不在枚举 {ACTIONS}"
        for k in _REQUIRED_PARAMS[action]:
            if not it.get(k):
                return False, f"条目{i}({action}) 缺必填参数 {k}"
        if action == "scan" and not isinstance(it.get("banned"), (list, tuple)):
            return False, f"条目{i}(scan) banned 须为词表"
        if action == "http":
            try:
                int(it["expect_status"])
            except (TypeError, ValueError):
                return False, f"条目{i}(http) expect_status 须为整数"
    return True, "ok"


def _q(s: str) -> str:
    return '"' + str(s).replace('"', "'") + '"'


def compile_item(item: dict) -> str:
    """把结构化条目编译成白名单命令行（python 入口）。"""
    action = item["action"]
    if action == "cmd":
        return str(item["cmd"])
    if action == "test":
        return f"python -m pytest {item['args']}"
    if action == "scan":
        words = " ".join(_q(w) for w in item["banned"])
        return (f'python "{_CHECKERS_DIR / "check_scan.py"}" '
                f'{_q(item["dir"])} {words}')
    if action == "script":
        args = item.get("args", "")
        return f'python "{item["path"]}" {args}'.strip()
    if action == "http":
        extra = f' {_q(item["contains"])}' if item.get("contains") else ""
        return (f'python "{_CHECKERS_DIR / "check_http.py"}" '
                f'{_q(item["url"])} {int(item["expect_status"])}{extra}')
    raise AcceptanceError(f"未知动作：{action}")


def compile_all(items: list[dict]) -> list[str]:
    ok, why = validate(items)
    if not ok:
        raise AcceptanceError(why)
    return [compile_item(it) for it in items]


def _exam_manifest(entry: Path, seen: set | None = None) -> list[str]:
    """R2+ 考卷包闭包：入口脚本 + 本地导入闭包（entry 目录与上级 *.py）+
    同目录资源文件（非 .py，如 data_ref.txt）。返回排序摘要行。"""
    import re as _re
    seen = seen if seen is not None else set()
    entry = entry.resolve()
    if entry in seen or not entry.is_file():
        return [f"missing:{entry}"]
    seen.add(entry)
    lines = [f"entry:{entry.name}:{sha256_path(entry)}"]
    # 同目录资源（配置/指针，≤20 件）。B1 修正（五轮验收实测）：排除
    # 数据库三件套（*.db / -shm / -wal）——账本 WAL 是滚动文件，混入
    # 摘要会让隐式摘要随事务提交漂移（digest3≠digest2 假漂移）；且
    # 考卷读任务库本就是作弊面，永远不该是"考卷资源"。
    res = []
    if entry.parent.is_dir():
        from dag_agent.repository import is_active_ledger_file
        cand_res = []
        for f in sorted(entry.parent.iterdir()):
            if (f.is_file() and f.suffix != ".py" and f.suffix != ".pyc"
                    and not f.name.startswith(".")):
                if is_active_ledger_file(f):
                    continue
                cand_res.append(f)
        if len(cand_res) > 20:
            raise AcceptanceError(
                f"考卷同目录资源文件过多（{len(cand_res)} 件 > 20）：请显式声明 "
                "exam_files 考卷清单——静默截断会造成未锁资源漂移")
        for f in cand_res:
            try:
                res.append(f"res:{f.name}:{sha256_path(f)}")
            except OSError:
                pass
    lines += res
    # 本地导入闭包：入口目录 + 上级目录的可解析模块（含 judge/oracle 型伴生脚本）。
    # 多模块语句（import loader, validation, reconcile）逐名解析——旧正则只取
    # 首名，validation/reconcile 从未入闭包（A1 伴生缺口一并修复）
    src = entry.read_text(encoding="utf-8", errors="replace")
    mods: set[str] = set()
    for m in _re.finditer(r"^\s*import\s+([^\n#]+)", src, _re.M):
        for name in m.group(1).split(","):
            name = _re.sub(r"\s+as\s+\w+\s*$", "", name.strip()).strip()
            if name:
                mods.add(name)
    for m in _re.finditer(r"^\s*from\s+([A-Za-z_][A-Za-z0-9_.]*)", src, _re.M):
        mods.add(m.group(1))
    # from pkg import member[, member2]：成员也是包内模块候选
    # （mypkg/util.py 型——只抓包名会漏成员文件，A1 包闭包缺口）
    for m in _re.finditer(
            r"^\s*from\s+([A-Za-z_][A-Za-z0-9_.]*)\s+import\s+([^\n#]+)",
            src, _re.M):
        for member in m.group(2).split(","):
            member = _re.sub(r"\s+as\s+\w+\s*$", "", member.strip()).strip()
            if member and "." not in member:
                mods.add(f"{m.group(1)}.{member}")
    # 相对导入（A1 补）：from . import helper / from .mod import x /
    # from .. import y——名字按"当前文件同级"解析，交给下方 stems 扫描
    # （当前扫描覆盖 entry 同级与上级两级，恰好对应 . 与 .. ）
    for m in _re.finditer(
            r"^\s*from\s+(\.+)([A-Za-z_][A-Za-z0-9_.]*)?\s+import\s+([^\n#]+)",
            src, _re.M):
        mod = m.group(2) or ""
        if mod:
            mods.add(mod)
        for member in m.group(3).split(","):
            member = _re.sub(r"\s+as\s+\w+\s*$", "", member.strip()).strip()
            if member and "." not in member:
                mods.add(f"{mod}.{member}" if mod else member)
    # 子进程伴生脚本：源码中引用的 "<name>.py" 字面量（judge.py 经 subprocess
    # 执行、不经 import，旧闭包漏锁——改永绿 grader 摘要不变的缺口，A1 修复）
    refs = set(_re.findall(r"([A-Za-z0-9_\-]+\.py)\b", src))
    out = lines
    for base in (entry.parent, entry.parent.parent):
        if not base.is_dir():
            continue
        stems = set()
        for name in mods:
            stems.add(name.split(".")[0])
            stems.add(name.split(".")[-1])
        for f in sorted(base.glob("*.py")):
            if (f.stem in stems or f.name in refs) and f.resolve() not in seen:
                out += _exam_manifest(f, seen)
        # 包内导入（A1 补）：from pkg import x / import pkg.mod →
        # pkg/__init__.py 与逐级模块文件入闭包（目录包此前整体漏锁）
        for name in sorted(mods):
            cur = base
            for seg in name.split("."):
                for cand in (cur / f"{seg}.py", cur / seg / "__init__.py"):
                    if cand.is_file() and cand.resolve() not in seen:
                        out += _exam_manifest(cand, seen)
                cur = cur / seg
    return sorted(out)


def _py_tokens(cmd: str) -> list[Path]:
    """从命令文本提取实际存在的本地文件入口（含 pytest 选择器
    test.py::test_name 形态——token 本身不以 .py 结尾，取 :: 前缀）。
    B1（Codex 五轮）：**不再按扩展名猜**——凡实际存在的本地文件都算
    考卷入口（.pyw/无扩展名脚本旧实现漏锁：永久改卷摘要不变，BAD
    机器闭合）；目录形态（python -m pytest <目录>）仍展开其下全部 .py
    （目录包考卷按 .py 全扫，语义不变）；超过 60 件**显式拒绝**（静默
    截断=第 61 件起漏锁）。多入口全收。
    兼容性：.py 入口的 manifest 集合与旧行为逐字节一致（digest 不变）；
    仅新增 .pyw/无扩展名入口入锁。"""
    import shlex as _shlex
    try:
        # posix=False：Windows 路径反斜杠不被当转义吃掉（裸 C:\Users 在
        # POSIX 模式会变 C:Users → 目录/文件全部失配漏锁）；引号手动剥
        toks = _shlex.split(cmd or "", posix=False)
    except ValueError:
        return []
    out: list[Path] = []
    for t in toks:
        t = t.strip('"').strip("'")
        cand = t.split("::", 1)[0]        # pytest 文件选择器
        # 引号外游离的 ::selector 会剥成空串——Path("") 即当前工作目录，
        # 不得当目录入口（否则误锁整个 cwd）
        if not cand or cand in (".", ".."):
            continue
        p = Path(cand)
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            inner = sorted(p.rglob("*.py"))
            if len(inner) > 60:
                raise AcceptanceError(
                    f"考卷目录过大（{len(inner)} 件 .py > 60）：请拆分测试"
                    "目录或显式声明考卷包——静默截断会造成未锁部分漂移")
            out += inner
    return out


def compute_check_digest(check_cmd: str | None = None,
                         acceptance: list[dict] | None = None,
                         exam_files: tuple[str, ...] = ()) -> str:
    """R2+ 考卷包内容摘要：命令文本 + 入口脚本包闭包（入口/导入/资源）。
    只锁命令文本不算锁考卷——改入口脚本、其本地导入或同目录资源
    （如 data_ref 指针）都会改变本摘要；P0/P4 双核验拒收。
    T6：exam_files=考卷包显式清单（出题方声明）——显式模式下按清单冻结
    （清单即权威：入口/伴生/资源须由出题方列入清单，不再做扫描闭包，
    终结"追 Python 生态宽容性"的猫鼠游戏）；清单缺失文件=出题错误，
    AcceptanceError 硬拒。清单为空时摘要口径与旧版完全一致（扫描闭包
    默认，兼容既有指纹）。两种模式互不干扰。"""
    import hashlib
    parts: list[str] = []
    entries: list[Path] = []
    if exam_files:
        # T6 显式模式：命令文本 + acceptance 结构 + 清单文件内容哈希
        parts.append(check_cmd or "")
        if acceptance is not None:
            parts.append(json.dumps(acceptance, ensure_ascii=False,
                                    sort_keys=True))
        for rel in exam_files:
            p = Path(rel)
            if not p.is_file():
                raise AcceptanceError(f"考卷包显式清单文件缺失：{rel}")
            parts.append(f"exam:{p.as_posix()}:{sha256_path(p)}")
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    if acceptance is not None:
        parts.append(json.dumps(acceptance, ensure_ascii=False, sort_keys=True))
        for a in acceptance:
            if a.get("action") == "script" and a.get("path"):
                entries.append(Path(a["path"]))
            # A1 补：每条编译后的命令文本也提取 .py 入口——结构化 cmd 的
            # python 入口、test 的目标文件、scan/http 的平台 checker 此前
            # 漏锁（改入口内容摘要不变，BAD 提交被放行）
            try:
                cmd = compile_item(a)
            except AcceptanceError:
                cmd = ""
            for p in _py_tokens(cmd):
                if p not in entries:
                    entries.append(p)
    else:
        parts.append(check_cmd or "")
        # 普通命令考卷：python "<入口.py>" … → 全部 .py 入口进包
        # （含 pytest 选择器/目录形态；只锁首个的旧行为漏锁其余入口）
        for p in _py_tokens(check_cmd or ""):
            if p not in entries:
                entries.append(p)
    for e in entries:
        parts += _exam_manifest(e)
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# 平台自身可信件（scan/http 动作编译出的内置 checker）——覆盖校验豁免，
# 出题方无须把平台组件列入业务清单（边界与"解释器/标准库另行明确"同理）
_PLATFORM_EXEMPT = _CHECKERS_DIR.resolve()


def _is_platform_checker(p: Path) -> bool:
    """精确目录组件判断（Codex t-fix-review：子串包含式豁免须收紧）。"""
    try:
        p.resolve().relative_to(_PLATFORM_EXEMPT)
        return True
    except (ValueError, OSError):
        return False


def business_entry_paths(check_cmd: str | None = None,
                         acceptance: list[dict] | None = None) -> list[Path]:
    """提取考卷命令实际引用的全部本地文件系统入口（F1 重写：
    不按扩展名猜——Python 可执行 .pyw/无扩展名脚本，旧实现在 .pyw 上
    提取为空集合被当覆盖成功，BAD accepted）。

    语义："清单即权威"——命令文本中出现的每个实际存在的本地文件或
    目录 token 都是考卷包成员，必须被显式清单覆盖（目录展开为其下
    全部 .py）；平台 checker 目录内件豁免；-c 等无本地引用的纯计算
    考卷返回空（由 verify 决定接受）。
    """
    import shlex as _shlex
    entries: list[Path] = []
    cmds: list[str] = []
    if acceptance is not None:
        for a in acceptance:
            if a.get("action") == "script" and a.get("path"):
                entries.append(Path(a["path"]).resolve())
            try:
                cmds.append(compile_item(a))
            except AcceptanceError:
                pass
    else:
        cmds.append(check_cmd or "")
    for cmd in cmds:
        try:
            toks = _shlex.split(cmd or "", posix=False)
        except ValueError:
            continue
        for t in toks:
            t = t.strip('"').strip("'")
            cand = t.split("::", 1)[0]
            if not cand or cand in (".", ".."):
                continue
            p = Path(cand)
            if not (p.is_file() or p.is_dir()):
                continue
            if _is_platform_checker(p):
                continue
            if p.is_dir():
                inner = sorted(p.rglob("*.py"))
                if len(inner) > 60:
                    raise AcceptanceError(
                        f"考卷目录过大（{len(inner)} 件 .py > 60）：请拆分或"
                        "显式声明考卷包——静默截断会造成未锁部分漂移")
                entries.extend(x.resolve() for x in inner)
            else:
                entries.append(p.resolve())
    return entries


def verify_exam_files_cover(check_cmd: str | None = None,
                            acceptance: list[dict] | None = None,
                            exam_files: list[str] | tuple = ())         -> tuple[bool, list[str]]:
    """T6' 显式清单覆盖校验（F1 后口径）：考卷命令引用的全部本地文件
    系统入口必须在清单内——覆盖不了就拒绝（不能解析≠空覆盖成功；
    语义不再依赖扩展名）。纯计算考卷（无本地引用）返回 ok。"""
    want = {Path(f).resolve() for f in exam_files}
    missing = []
    for e in business_entry_paths(check_cmd, acceptance):
        if e not in want:
            missing.append(str(e))
    return (not missing), missing


def sha256_path(p: Path) -> str:
    import hashlib
    return hashlib.sha256(p.read_bytes()).hexdigest()


def env_note() -> str:
    """v3.2 环境指纹：python 版本 + 平台。白名单/快照/提示词约束不是 OS 沙箱，
    隔离边界以本指纹如实记录为限。"""
    import platform
    import sys
    return f"python={sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro};os={platform.system()}"


def run_acceptance(cmds: list[str], cwd: Path,
                   timeout: int = 120) -> tuple[str, str]:
    """逐条执行验收命令（三分聚合）：
    任一 error → error（考卷故障）；否则任一 fail → fail；全过 → pass。
    """
    evidences: list[str] = []
    verdict = "pass"
    for cmd in cmds:
        v, ev = run_check3(cmd, cwd, timeout=timeout)
        evidences.append(f"[{v}] {cmd[:80]} :: {ev[:200]}")
        if v == "error":
            return "error", "\n".join(evidences)
        if v == "fail":
            verdict = "fail"
    return verdict, "\n".join(evidences)


# ---- 考卷体检三关 ----

def preflight(check_cmd: str | None = None,
              acceptance: list[dict] | None = None,
              timeout: int = 30) -> tuple[bool, str, dict]:
    """考卷锁定前体检。返回 (ok, why, detail)。

    关1 语法：shlex 可解析 + 入口在白名单（acceptance 走 validate+compile）。
    关2 能跑起：空目录执行 → 非 ERROR。
    关3 有牙齿：同一次执行 → 必须 FAIL（空目录都过=废卷）。
    """
    detail: dict = {}
    cmds: list[str]
    if acceptance is not None:
        ok, why = validate(acceptance)
        if not ok:
            detail["gate1_syntax"] = False
            return False, f"关1(语法)：{why}", detail
        try:
            cmds = compile_all(acceptance)
        except AcceptanceError as e:
            detail["gate1_syntax"] = False
            return False, f"关1(语法)：{e}", detail
    else:
        cmds = [check_cmd or ""]
    # 关1：逐条静态检查
    for c in cmds:
        try:
            parts = shlex.split(c)
        except ValueError as e:
            detail["gate1_syntax"] = False
            return False, f"关1(语法)：命令解析失败 {e}", detail
        if not parts or Path(parts[0]).name not in WHITELIST:
            detail["gate1_syntax"] = False
            return False, f"关1(语法)：入口不在白名单 {parts[:1]}", detail
    detail["gate1_syntax"] = True
    # 关2+关3：空目录一次执行定两关
    with tempfile.TemporaryDirectory(prefix="preflight_") as td:
        fixture = Path(td)
        verdict, evidence = run_acceptance(cmds, fixture, timeout=timeout)
    detail["gate2_runnable"] = verdict != "error"
    detail["gate3_bites"] = verdict == "fail"
    if verdict == "error":
        return False, f"关2(能跑起)：考卷/环境故障——{evidence[:200]}", detail
    if verdict == "pass":
        return False, "关3(有牙齿)：空目录全绿=废卷（永绿断言），打回重写", detail
    return True, "体检通过：能编译、能跑起、咬得动坏产物", detail
