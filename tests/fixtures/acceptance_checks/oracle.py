"""Independent CSV validation and SQLite aggregation for the reconciliation benchmark.

This is a judge-side oracle, not a candidate bundle/main.py implementation.
Only Python's standard library is required. Monetary values use arbitrary-precision
integers; SQLite stores them as text and uses a Python big-integer aggregate.
"""
from __future__ import annotations

import csv
import re
import sqlite3
from pathlib import Path

ORDER_COLUMNS = ("order_id", "region", "currency", "amount_cents", "status")
REFUND_COLUMNS = ("refund_id", "order_id", "currency", "amount_cents")
AMOUNT_RE = re.compile(r"[0-9]+\Z")


class InputError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


def _read(path: Path, required: tuple[str, ...]) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, strict=True)
        fields = reader.fieldnames or []
        if len(fields) != len(set(fields)) or not set(required).issubset(fields):
            raise InputError("columns_missing", f"Required columns: {required}")
        rows = []
        try:
            for row in reader:
                if None in row or any(row.get(name) is None for name in required):
                    raise InputError("columns_missing", "Malformed CSV row")
                rows.append(row)
        except csv.Error as exc:
            raise InputError("columns_missing", str(exc)) from exc
        return rows


def _amount(value: str) -> int:
    if not isinstance(value, str) or not AMOUNT_RE.fullmatch(value):
        raise InputError("amount_invalid", "amount_cents must contain ASCII digits only")
    return int(value)


class _BigSum:
    def __init__(self) -> None:
        self.total = 0

    def step(self, value: str) -> None:
        self.total += int(value)

    def finalize(self) -> str:
        return str(self.total)


def expected_summary(orders_path: str | Path, refunds_path: str | Path) -> dict:
    orders = _read(Path(orders_path), ORDER_COLUMNS)
    refunds = _read(Path(refunds_path), REFUND_COLUMNS)
    by_id: dict[str, dict] = {}
    for row in orders:
        identity = row["order_id"]
        if not identity or not identity.strip():
            raise InputError("order_id_empty")
        if identity in by_id:
            raise InputError("order_id_duplicate")
        if not row["region"] or not row["region"].strip():
            raise InputError("region_empty")
        if row["currency"] not in {"CNY", "USD"}:
            raise InputError("currency_invalid")
        if row["status"] not in {"paid", "cancelled"}:
            raise InputError("status_invalid")
        amount = _amount(row["amount_cents"])
        by_id[identity] = dict(row, numeric_amount=amount)

    refund_ids = set()
    refunds_per_order: dict[str, int] = {}
    for row in refunds:
        identity = row["refund_id"]
        if not identity or not identity.strip():
            raise InputError("refund_id_empty")
        if identity in refund_ids:
            raise InputError("refund_id_duplicate")
        refund_ids.add(identity)
        amount = _amount(row["amount_cents"])
        order = by_id.get(row["order_id"])
        if order is None:
            raise InputError("unknown_order")
        if order["status"] != "paid":
            raise InputError("refund_on_cancelled")
        if row["currency"] != order["currency"]:
            raise InputError("currency_mismatch")
        total = refunds_per_order.get(row["order_id"], 0) + amount
        if total > order["numeric_amount"]:
            raise InputError("refund_exceeds_order")
        refunds_per_order[row["order_id"]] = total

    with sqlite3.connect(":memory:") as db:
        db.create_aggregate("big_sum", 1, _BigSum)
        db.execute("CREATE TABLE orders (id TEXT PRIMARY KEY, region TEXT, currency TEXT, amount TEXT, status TEXT)")
        db.execute("CREATE TABLE refunds (id TEXT PRIMARY KEY, order_id TEXT, currency TEXT, amount TEXT)")
        db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)",
                       [(r["order_id"], r["region"], r["currency"], str(r["numeric_amount"]), r["status"]) for r in by_id.values()])
        db.executemany("INSERT INTO refunds VALUES (?, ?, ?, ?)",
                       [(r["refund_id"], r["order_id"], r["currency"], str(_amount(r["amount_cents"]))) for r in refunds])
        cancelled = db.execute("SELECT COUNT(*) FROM orders WHERE status = 'cancelled'").fetchone()[0]
        refund_groups = {
            (region, currency): int(amount)
            for region, currency, amount in db.execute(
                "SELECT o.region, o.currency, big_sum(r.amount) FROM refunds r "
                "JOIN orders o ON r.order_id=o.id GROUP BY o.region,o.currency")
        }
        totals = []
        for region, currency, count, gross in db.execute(
                "SELECT region,currency,COUNT(*),big_sum(amount) FROM orders "
                "WHERE status='paid' GROUP BY region,currency ORDER BY currency,region"):
            refund = refund_groups.get((region, currency), 0)
            gross_int = int(gross)
            totals.append({"region": region, "currency": currency, "paid_orders": count,
                           "gross_cents": gross_int, "refund_cents": refund,
                           "net_cents": gross_int - refund})
        return {"schema_version": 1, "cancelled_orders": cancelled, "totals": totals}
