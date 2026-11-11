"""Functional judge only. A PASS here is not a multi-agent collaboration PASS.

CLI: python judge.py --bundle PATH --evidence PATH
     python judge.py --self-check
Candidates run as trusted local subprocesses, not in an OS security sandbox.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from oracle import InputError, ORDER_COLUMNS, REFUND_COLUMNS, expected_summary

BASE = Path(__file__).resolve().parent
WORK = BASE.parents[1] / "work" / "multiagent_benchmark_judge"
ERROR_CODES = ("order_id_duplicate", "refund_id_duplicate", "unknown_order",
               "refund_on_cancelled", "currency_mismatch", "amount_invalid",
               "refund_exceeds_order", "status_invalid", "columns_missing",
               "currency_invalid", "order_id_empty", "refund_id_empty", "region_empty")

# B4 变体负例（2026-10-04）：金额尾随换行应拒为 amount_invalid
# （SPEC：amount_cents 只能是 ASCII 非负十进制整数）
NEGATIVE_VARIANTS = (("amount_trailing_newline_order", "amount_invalid"),
                     ("amount_trailing_newline_refund", "amount_invalid"))


def _json_text(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _sources(bundle: Path) -> dict[str, str | None]:
    return {p.relative_to(bundle).as_posix(): _hash(p)
            for p in sorted(bundle.rglob("*.py")) if "__pycache__" not in p.parts}


def _judge_files() -> dict[str, str | None]:
    paths = [BASE / "judge.py", BASE / "oracle.py"]
    paths.extend(p for p in (BASE / "fixtures").rglob("*") if p.is_file())
    return {p.relative_to(BASE).as_posix(): _hash(p) for p in sorted(paths)}


def _csv(path: Path, fields: tuple, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _basic() -> tuple[list[dict], list[dict]]:
    orders = [dict(zip(ORDER_COLUMNS, row)) for row in [
        ("a", "north", "CNY", "100", "paid"),
        ("b", "south", "USD", "200", "paid"),
        ("c", "east", "CNY", "50", "cancelled"),
    ]]
    refunds = [dict(zip(REFUND_COLUMNS, ("r", "a", "CNY", "20")))]
    return orders, refunds


def _negative(code: str, target: Path) -> None:
    orders, refunds = _basic()
    order_fields = ORDER_COLUMNS
    if code == "order_id_duplicate":
        orders.append(dict(orders[0]))
    elif code == "refund_id_duplicate":
        refunds.append(dict(refunds[0]))
    elif code == "unknown_order":
        refunds[0]["order_id"] = "nonexistent"
    elif code == "refund_on_cancelled":
        refunds[0]["order_id"] = "c"
    elif code == "currency_mismatch":
        refunds[0]["currency"] = "USD"
    elif code == "amount_invalid":
        orders[1]["amount_cents"] = "2.5"
    elif code == "amount_trailing_newline_order":
        orders[1]["amount_cents"] = "8000\n"
    elif code == "amount_trailing_newline_refund":
        refunds[0]["amount_cents"] = "20\n"
    elif code == "refund_exceeds_order":
        refunds.append(dict(zip(REFUND_COLUMNS, ("r2", "a", "CNY", "81"))))
    elif code == "status_invalid":
        orders[1]["status"] = "pending"
    elif code == "columns_missing":
        order_fields = tuple(f for f in ORDER_COLUMNS if f != "region")
        orders = [{k: v for k, v in row.items() if k != "region"} for row in orders]
    elif code == "currency_invalid":
        orders[1]["currency"] = "EUR"
    elif code == "order_id_empty":
        orders[1]["order_id"] = ""
    elif code == "refund_id_empty":
        refunds[0]["refund_id"] = ""
    elif code == "region_empty":
        orders[1]["region"] = ""
    else:
        raise ValueError(code)
    _csv(target / "orders.csv", order_fields, orders)
    _csv(target / "refunds.csv", REFUND_COLUMNS, refunds)


def _random_valid(target: Path, seed: int, variant: int) -> None:
    rng = random.Random(seed)
    orders, refunds = [], []
    for index in range(17 + variant * 3):
        identity = f"o-{seed:x}-{index}"
        region = rng.choice(["north", "east", "south", "西南,一区"])
        currency = rng.choice(["CNY", "USD"])
        amount = 0 if index == 0 else rng.randrange(1, 10**7)
        if variant == 4 and index == 1:
            amount = 10**25 + 37  # Check exact integer handling beyond SQLite int64.
        status = "cancelled" if index % 7 == 6 else "paid"
        amount_text = str(amount).zfill(8) if index % 5 == 2 else str(amount)
        orders.append(dict(zip(ORDER_COLUMNS, (identity, region, currency, amount_text, status))))
        if status == "paid" and amount and index % 4:
            remaining = amount
            for refund_index in range(rng.randrange(1, 4)):
                value = remaining if index % 4 == 1 and refund_index == 0 else rng.randrange(remaining + 1)
                refunds.append(dict(zip(REFUND_COLUMNS, (
                    f"r-{seed:x}-{index}-{refund_index}", identity, currency, str(value)))))
                remaining -= value
    rng.shuffle(orders)
    rng.shuffle(refunds)
    _csv(target / "orders.csv", ORDER_COLUMNS, orders)
    _csv(target / "refunds.csv", REFUND_COLUMNS, refunds)


def _strict_load(text: str) -> object:
    def pairs(items: list[tuple]) -> dict:
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"Non-JSON constant: {value}")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=reject_constant)


def _freeze_check() -> dict:
    manifest = json.loads((BASE / "fixtures" / "freeze_manifest.json").read_text(encoding="utf-8"))
    for relative, digest in manifest["files_sha256"].items():
        if _hash(BASE / "fixtures" / relative) != digest:
            raise ValueError(f"Frozen fixture changed: {relative}")
    first, second = [], []
    for version, results in (("v1", first), ("v2", second)):
        folder = BASE / "fixtures" / version
        expected = _strict_load((folder / "expected.json").read_text(encoding="utf-8"))
        actual = expected_summary(folder / "orders.csv", folder / "refunds.csv")
        if _json_text(expected) != _json_text(actual):
            raise ValueError(f"Oracle differs from frozen answer: {version}")
        results.append(expected)
    if first == second:
        raise ValueError("v1/v2 expected summaries must differ")
    return manifest


def self_check() -> dict:
    _freeze_check()
    WORK.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="selfcheck-", dir=WORK) as temporary:
        folder = Path(temporary)
        for code in ERROR_CODES + tuple(name for name, _ in NEGATIVE_VARIANTS):
            _negative(code, folder)
            try:
                expected_summary(folder / "orders.csv", folder / "refunds.csv")
            except InputError as exc:
                want = dict(NEGATIVE_VARIANTS).get(code, code)
                if exc.code != want:
                    raise ValueError(f"Negative {code}: got {exc.code}") from exc
            else:
                raise ValueError(f"Negative {code} was accepted")
        for variant in range(5):
            _random_valid(folder, 102030 + variant, variant)
            expected_summary(folder / "orders.csv", folder / "refunds.csv")
    return {"scope": "functional_judge_self_check", "verdict": "PASS",
            "fixture_checks": 2, "negative_checks": len(ERROR_CODES) + len(NEGATIVE_VARIANTS), "random_oracle_checks": 5,
            "collaboration_verified": False,
            "meaning": "Only judge/fixture internal consistency was checked; no candidate ran"}


def _run_case(bundle: Path, baseline: dict, judge_baseline: dict, target: Path, name: str,
              expected: dict | None, error_code: str | None, seed: int | None) -> dict:
    orders, refunds, output = target / "orders.csv", target / "refunds.csv", target / "summary.json"
    input_hashes = {"orders.csv": _hash(orders), "refunds.csv": _hash(refunds)}
    result = {"name": name, "kind": "invalid" if error_code else "valid", "seed": seed,
              "expected_error": error_code, "inputs_sha256": input_hashes,
              "passed": False, "failures": []}
    command = [sys.executable, "-B", str(bundle / "main.py"), "--orders", str(orders),
               "--refunds", str(refunds), "--out", str(output)]
    started = time.monotonic()
    try:
        process = subprocess.run(command, cwd=target, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=15)
        result.update(returncode=process.returncode, stdout=process.stdout[:6000], stderr=process.stderr[:3000])
        if error_code:
            if process.returncode != 2:
                result["failures"].append("invalid_input_exit_code_must_be_2")
            if output.exists():
                result["failures"].append("invalid_input_created_output")
            try:
                response = _strict_load(process.stdout)
                if not isinstance(response, dict) or response.get("error") != error_code:
                    result["failures"].append("wrong_error_json")
                elif set(response) - {"error", "message"} or ("message" in response and not isinstance(response["message"], str)):
                    result["failures"].append("invalid_error_json_schema")
            except (ValueError, TypeError):
                result["failures"].append("stdout_is_not_one_json_object")
        else:
            if process.returncode != 0:
                result["failures"].append("valid_input_exit_code_must_be_0")
            try:
                actual = _strict_load(output.read_text(encoding="utf-8"))
                result["actual"] = actual
                result["expected"] = expected
                if _json_text(actual) != _json_text(expected):
                    result["failures"].append("summary_json_mismatch")
            except (OSError, ValueError, TypeError) as exc:
                result["failures"].append(f"missing_or_invalid_output:{type(exc).__name__}")
    except subprocess.TimeoutExpired:
        result["failures"].append("candidate_timeout_15s")
    except OSError as exc:
        result["failures"].append(f"candidate_launch_failed:{type(exc).__name__}")
    finally:
        result["duration_seconds"] = round(time.monotonic() - started, 4)
        result["inputs_unchanged"] = input_hashes == {"orders.csv": _hash(orders), "refunds.csv": _hash(refunds)}
        result["source_unchanged"] = baseline == _sources(bundle)
        result["judge_unchanged"] = judge_baseline == _judge_files()
        if not result["inputs_unchanged"]:
            result["failures"].append("candidate_modified_input")
        if not result["source_unchanged"]:
            result["failures"].append("candidate_modified_source")
        if not result["judge_unchanged"]:
            result["failures"].append("candidate_modified_judge_or_fixtures")
    result["passed"] = not result["failures"]
    return result


def judge(bundle: Path, evidence: Path) -> tuple[dict, int]:
    report = {"schema_version": 1, "scope": "functional_only", "verdict": "FAIL",
              "collaboration_verified": False, "cases": [],
              "limitation": "Local subprocess tests are not an OS sandbox or collaboration/provenance audit"}
    if not (bundle / "main.py").is_file():
        report["judge_error"] = "Candidate bundle/main.py is missing"
    else:
        try:
            _freeze_check()
            report["fixture_manifest_sha256"] = _hash(BASE / "fixtures" / "freeze_manifest.json")
            baseline = _sources(bundle)
            judge_baseline = _judge_files()
            report["candidate_source_sha256"] = baseline
            WORK.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="judge-", dir=WORK) as temporary:
                root = Path(temporary)
                for version in ("v1", "v2"):
                    target = root / version
                    target.mkdir()
                    for filename in ("orders.csv", "refunds.csv"):
                        (target / filename).write_bytes((BASE / "fixtures" / version / filename).read_bytes())
                    expected = _strict_load((BASE / "fixtures" / version / "expected.json").read_text(encoding="utf-8"))
                    report["cases"].append(_run_case(bundle, baseline, judge_baseline, target, version, expected, None, None))
                for variant in range(5):
                    seed = secrets.randbits(64)
                    target = root / f"random-{variant + 1}"
                    target.mkdir()
                    _random_valid(target, seed, variant)
                    expected = expected_summary(target / "orders.csv", target / "refunds.csv")
                    report["cases"].append(_run_case(bundle, baseline, judge_baseline, target, target.name, expected, None, seed))
                for code in ERROR_CODES + tuple(name for name, _ in NEGATIVE_VARIANTS):
                    target = root / code
                    target.mkdir()
                    _negative(code, target)
                    expected_code = dict(NEGATIVE_VARIANTS).get(code, code)
                    report["cases"].append(_run_case(bundle, baseline, judge_baseline, target, code, None, expected_code, None))
            report["passed_cases"] = sum(case["passed"] for case in report["cases"])
            report["total_cases"] = len(report["cases"])
            expected_total = (2 + 5 + len(ERROR_CODES) + len(NEGATIVE_VARIANTS))
            if (report["total_cases"] == expected_total
                    and report["passed_cases"] == expected_total):
                report["verdict"] = "PASS"
        except (OSError, ValueError, sqlite3.Error) as exc:
            report["judge_error"] = f"{type(exc).__name__}: {exc}"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report, 0 if report["verdict"] == "PASS" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        try:
            print(json.dumps(self_check(), ensure_ascii=False))
            return 0
        except (OSError, ValueError) as exc:
            print(json.dumps({"scope": "functional_judge_self_check", "verdict": "FAIL", "error": str(exc)}, ensure_ascii=False))
            return 1
    if args.bundle is None or args.evidence is None:
        parser.error("--bundle and --evidence are required unless --self-check is used")
    report, code = judge(args.bundle.resolve(), args.evidence.resolve())
    print(json.dumps({"scope": report["scope"], "verdict": report["verdict"],
                      "passed_cases": report.get("passed_cases", 0), "total_cases": report.get("total_cases", 0),
                      "evidence": str(args.evidence.resolve()), "collaboration_verified": False}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
