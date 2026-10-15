"""Independent Python implementation of the lab spec. It computes the expected
contents of the candidate's staging and mart models directly from the
candidate's seed CSVs, so expected results are always consistent with the
dataset the candidate actually committed (per-candidate seeds).

Semantics mirror starter/README.md section 1 exactly:
  * TRIM == strip ASCII spaces on both ends (Postgres TRIM default)
  * status = lower(trim(status))
  * order_date = date part of order_ts
  * revenue-recognised statuses: completed, shipped
"""
from __future__ import annotations

import csv
import os
from collections import OrderedDict
from decimal import Decimal

CANONICAL_STATUSES = ("completed", "shipped", "pending", "cancelled", "returned")
REVENUE_STATUSES = ("completed", "shipped")
ZERO = Decimal("0.00")

SEED_FILES = {
    "customers": ("customers.csv", ["customer_id", "full_name", "email", "country", "signup_date"]),
    "products": ("products.csv", ["product_id", "product_name", "category", "unit_price"]),
    "orders": ("orders.csv", ["order_id", "customer_id", "product_id", "order_ts", "quantity", "amount", "status"]),
}


def _trim(value: str) -> str:
    return value.strip(" ")


def read_csv(path: str):
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return reader.fieldnames or [], list(reader)


def validate_seeds(seeds_dir: str):
    """Return a list of problems with the seed files (empty list == OK)."""
    problems = []
    for key, (fname, header) in SEED_FILES.items():
        path = os.path.join(seeds_dir, fname)
        if not os.path.isfile(path):
            problems.append(f"missing seed file seeds/{fname}")
            continue
        fields, rows = read_csv(path)
        if fields != header:
            problems.append(f"seeds/{fname}: header {fields} != expected {header}")
        if not rows:
            problems.append(f"seeds/{fname}: no data rows")
    return problems


def load_seeds(seeds_dir: str):
    data = {}
    for key, (fname, _header) in SEED_FILES.items():
        _fields, rows = read_csv(os.path.join(seeds_dir, fname))
        data[key] = rows
    return data


def stg_customers(seeds):
    out = []
    for r in seeds["customers"]:
        out.append({
            "customer_id": int(r["customer_id"]),
            "customer_name": _trim(r["full_name"]),
            "email": _trim(r["email"]).lower(),
            "country": _trim(r["country"]).upper(),
            "signup_date": r["signup_date"][:10],
        })
    return out


def stg_orders(seeds):
    out = []
    for r in seeds["orders"]:
        out.append({
            "order_id": int(r["order_id"]),
            "customer_id": int(r["customer_id"]),
            "product_id": int(r["product_id"]),
            "order_date": _trim(r["order_ts"])[:10],
            "quantity": int(r["quantity"]),
            "order_amount": Decimal(_trim(r["amount"])),
            "status": _trim(r["status"]).lower(),
        })
    return out


def compute_expected(seeds):
    customers = stg_customers(seeds)
    orders = stg_orders(seeds)

    # ---- marts.daily_revenue (grain: order_date; only days with >= 1 order)
    daily = OrderedDict()
    for o in sorted(orders, key=lambda x: x["order_date"]):
        d = daily.setdefault(o["order_date"], {
            "order_count": 0, "completed_order_count": 0, "items_sold": 0, "revenue": ZERO,
        })
        d["order_count"] += 1
        if o["status"] in REVENUE_STATUSES:
            d["completed_order_count"] += 1
            d["items_sold"] += o["quantity"]
            d["revenue"] += o["order_amount"]

    # ---- marts.customer_orders (grain: customer_id; every customer)
    cust = OrderedDict()
    for c in sorted(customers, key=lambda x: x["customer_id"]):
        cust[c["customer_id"]] = {
            "customer_name": c["customer_name"],
            "email": c["email"],
            "order_count": 0,
            "completed_order_count": 0,
            "lifetime_revenue": ZERO,
            "first_order_date": None,
            "last_order_date": None,
        }
    for o in orders:
        c = cust.get(o["customer_id"])
        if c is None:  # order for an unknown customer: not in the mart (LEFT JOIN from customers)
            continue
        c["order_count"] += 1
        if o["status"] in REVENUE_STATUSES:
            c["completed_order_count"] += 1
            c["lifetime_revenue"] += o["order_amount"]
        if c["first_order_date"] is None or o["order_date"] < c["first_order_date"]:
            c["first_order_date"] = o["order_date"]
        if c["last_order_date"] is None or o["order_date"] > c["last_order_date"]:
            c["last_order_date"] = o["order_date"]

    status_counts = {}
    for o in orders:
        status_counts[o["status"]] = status_counts.get(o["status"], 0) + 1

    messy_status_rows = sum(1 for r in seeds["orders"] if r["status"] != _trim(r["status"]).lower())

    return {
        "daily_revenue": daily,
        "customer_orders": cust,
        "status_counts": status_counts,
        "summary": {
            "orders": len(orders),
            "customers": len(customers),
            "days_with_orders": len(daily),
            "customers_without_orders": sum(1 for c in cust.values() if c["order_count"] == 0),
            "messy_status_rows": messy_status_rows,
            "total_revenue": str(sum((d["revenue"] for d in daily.values()), ZERO)),
        },
    }


def to_jsonable(expected):
    """Make the expected-results dict JSON serialisable (Decimals -> str)."""
    def conv(v):
        if isinstance(v, Decimal):
            return f"{v:.2f}"
        if isinstance(v, dict):
            return {str(k): conv(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [conv(x) for x in v]
        return v
    return conv(expected)


def inject_negative_order(orders_csv: str) -> dict:
    """Append one otherwise-valid order with a negative amount.

    The row uses an existing customer and product, the last order date in the
    file and a canonical status, so the ONLY thing wrong with it is the
    negative amount -- only a "no negative amounts" audit should catch it.
    """
    fields, rows = read_csv(orders_csv)
    last = rows[-1]
    row = {
        "order_id": str(max(int(r["order_id"]) for r in rows) + 1),
        "customer_id": last["customer_id"],
        "product_id": last["product_id"],
        "order_ts": _trim(last["order_ts"])[:10] + " 23:59:00",
        "quantity": "1",
        "amount": "-42.00",
        "status": "completed",
    }
    with open(orders_csv, "rb") as fh:
        content = fh.read()
    with open(orders_csv, "a", newline="", encoding="utf-8") as fh:
        if content and not content.endswith(b"\n"):
            fh.write("\n")
        writer = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        writer.writerow(row)
    return row
