# -*- coding: utf-8 -*-
"""act2_faults.py — 幕二：八类受控故障的可判定验证（零模型回归映射 + 真实MCP路径）。

SPEC 第二幕：先零模型证明全部故障路径，再以有界真实会话验证运行控制。
本脚本输出每类故障的注入点/期望结果/实测结果 JSON（G4/G7 证据）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = Path(__file__).resolve().parent
EVID = PKG.parent / "多agent协同验收运行证据"
PY = sys.executable
T = lambda n: f"tests/v3/{n}"  # noqa: E731

# SPEC 八类故障 → 已冻结的行为回归（负例转回归=修复轮已完成）
FAULTS = [
    ("F1 业务FAIL不发布+OR切支", "向首选路线提交错误汇总→失败不发布；"
     "阈值切支后赢家满足同一强功能考卷",
     ["test_v32_step3_groups.py::test_a_fails_b_wins_unblocks_integration",
      "test_v32_step3_groups.py::test_scheduler_switches_route_on_threshold"]),
    ("F2 考卷ERROR→blocked→维修恢复", "受控环境故障使检查器 error：blocked、"
     "不耗产物失败额度；维修版本+1 后恢复；ERROR 不默认切支",
     ["test_v32_step3_groups.py::test_error_does_not_switch_route",
      "test_v32_step2_revision.py::test_repair_during_attempt_rejects_old_submit"]),
    ("F3 输入损坏/缺件拒绝", "篡改 accepted 副本或删件：播种拒绝、error+blocked、"
     "不改绑不静默（三场景 S1 动态复现）",
     ["__scenario_S1__"]),
    ("F4 并发重复提交", "同 attempt 竞态：一次正式接收；重复请求不删赢家工件、"
     "不新增矛盾证据",
     ["test_v32_r_fixes.py::test_concurrent_publish_keeps_winner_files"]),
    ("F5 发布准备后崩溃", "P3/P4 边界崩溃：重启后孤儿被幂等替换，可完成新合法"
     "提交，不因孤儿版本永久 blocked",
     ["test_v32_r_fixes.py::test_prepared_crash_recovers"]),
    ("F6 worker异常", "后端抛错：执行位结算释放、节点可恢复重派、无 protected "
     "僵尸",
     ["test_v32_r_fixes.py::test_worker_exception_releases_slot"]),
    ("F7 暂停到全空闲+两轮恢复", "全暂停→run 退出；resume 后 kick 自动续跑到"
     "闭合；两轮 pause/resume 均真实生效；并发1 调 C 先派 C（干预测试）",
     ["test_v32_r_fixes.py::test_pause_all_resume_kicks",
      "test_v32_r_fixes.py::test_receipt_failure_retry_and_repeatable_resume",
      "test_v32_step4_runtime.py::test_run_manager_single_scheduler_and_live_intervention"]),
    ("F8 入口唯一与投影版本", "旧 run 收编 RunManager 不产生第二调度器；recover "
     "活任务拒绝；board 走版本化、旧渲染后版本可恢复且单调",
     ["test_v32_r_fixes.py::test_projection_version_restored_after_plain_render",
      "test_v32_step4_runtime.py::test_restart_recovery_reaps_and_old_submit_rejected",
      "test_v32_step4_runtime.py::test_projection_versioned_guard_and_rebuild"]),
]


def run_pytest(test: str) -> dict:
    import tempfile
    import shutil
    td = tempfile.mkdtemp(prefix="act2_")
    r = subprocess.run([PY, "-m", "pytest", f"tests/v3/{test}", "-q",
                        f"--basetemp={td}"], capture_output=True, text=True,
                       cwd=str(ROOT))
    shutil.rmtree(td, ignore_errors=True)
    tail = (r.stdout or "").strip().splitlines()
    return {"exit": r.returncode, "summary": tail[-1] if tail else ""}


def main() -> int:
    EVID.mkdir(parents=True, exist_ok=True)
    out = {"act": "act2", "mode": "零模型受控故障 + 真实MCP入口冒烟", "faults": []}
    ok_all = True
    for name, spec, tests in FAULTS:
        entry = {"fault": name, "expected": spec, "tests": []}
        ok = True
        for t in tests:
            if t == "__scenario_S1__":
                r = subprocess.run([PY, "check_v32_scenarios.py"],
                                   capture_output=True, text=True, cwd=str(ROOT))
                entry["tests"].append({"case": "check_v32_scenarios(S1/S2/S3)",
                                       "exit": r.returncode})
                ok = ok and r.returncode == 0
                continue
            res = run_pytest(t)
            entry["tests"].append({"case": t, **res})
            ok = ok and res["exit"] == 0
        entry["pass"] = ok
        ok_all = ok_all and ok
        out["faults"].append(entry)
    # 真实 MCP 入口冒烟（27 工具/握手/调用——G7 的真连接部分）
    r = subprocess.run([PY, "check_v32_mcp_smoke.py"],
                       capture_output=True, text=True, cwd=str(ROOT))
    try:
        smoke = json.loads(r.stdout)
    except Exception:
        smoke = {"ok": False, "raw": r.stdout[-200:]}
    out["mcp_smoke"] = smoke
    ok_all = ok_all and smoke.get("ok")
    out["result"] = "ACT2_OK" if ok_all else "ACT2_FAILED"
    (EVID / "act2_faults.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"result": out["result"],
                      "faults": [(f["fault"], f["pass"]) for f in out["faults"]]},
                     ensure_ascii=False))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
