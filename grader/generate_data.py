#!/usr/bin/env python3
"""Generate the Northwind Gear seed CSVs (customers, products, orders).

This is the grader's own copy of the generator (it is covered by the grader
checksum). `tools/generate_data.py` and `make data` are thin wrappers around it.
The seed used is written to seeds/SEED so the grader can regenerate the data
and confirm the committed CSVs were not edited.

Every candidate can be given a different dataset by passing a different
``--seed``. The output is fully deterministic for a given seed and set of
options (it only uses ``random.Random(seed)`` and ``decimal.Decimal``), so a
reviewer can regenerate a candidate's dataset and compare it byte for byte.

The data is deliberately a little messy -- staging models must clean it:
  * order ``status`` comes in mixed case with leading/trailing spaces
  * customer ``full_name`` sometimes has trailing spaces
  * customer ``email`` is sometimes upper-case and/or padded with spaces
  * customer ``country`` is sometimes lower-case and/or padded with spaces
  * product ``category`` comes in mixed case, sometimes padded with spaces

Guaranteed shape (so the hidden checks always have something to bite on):
  * exactly 2 customers never place an order
  * exactly 1 day in the window has no orders at all
  * exactly 1 day has a single order, and it is cancelled (zero revenue)
  * every canonical status appears at least once, and every one of them
    appears at least once in a "messy" spelling

Usage:
  make data SEED=1234                            # from the repo root
  python3 tools/generate_data.py                 # default seed 42 -> seeds/
  python3 tools/generate_data.py --seed 1234 --out /tmp/cand-1234
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import random
from decimal import ROUND_HALF_UP, Decimal

DEFAULT_SEED = 42
DEFAULT_START = "2026-09-01"
DEFAULT_DAYS = 30
DEFAULT_CUSTOMERS = 20
DEFAULT_ORDERS = 150

CANONICAL_STATUSES = ["completed", "shipped", "pending", "cancelled", "returned"]
STATUS_WEIGHTS = [60, 15, 8, 10, 7]

FIRST_NAMES = [
    "Ava", "Liam", "Noah", "Emma", "Olivia", "Mateo", "Priya", "Arjun", "Sofia",
    "Lucas", "Hana", "Yuki", "Chloe", "Ethan", "Zara", "Omar", "Isla", "Leo",
    "Maya", "Ravi", "Freya", "Diego", "Amara", "Kai", "Nora", "Felix", "Ines",
    "Tariq", "Elena", "Jonas",
]
LAST_NAMES = [
    "Patel", "Smith", "Garcia", "Muller", "Kim", "Okafor", "Rossi", "Silva",
    "Nguyen", "Kowalski", "Haddad", "Larsen", "Mehta", "Brown", "Tanaka",
    "Dubois", "Fischer", "Reyes", "Singh", "Novak",
]
COUNTRIES = ["US", "GB", "DE", "IN", "CA", "FR"]

# (product_name, category, base_price)
PRODUCTS = [
    ("Trail Runner Shoes", "Footwear", 120),
    ("Alpine Shell Jacket", "Apparel", 240),
    ("Summit Daypack 28L", "Packs", 95),
    ("Camp Stove Mini", "Camping", 60),
    ("Merino Base Layer", "Apparel", 75),
    ("Carbon Trekking Poles", "Accessories", 140),
    ("Insulated Bottle 1L", "Accessories", 35),
    ("Headlamp 400", "Accessories", 45),
    ("Bivy Tent 2P", "Camping", 320),
    ("Chalk Bag", "Climbing", 25),
]

CENT = Decimal("0.01")


def messy_variant(rng: random.Random, value: str) -> str:
    """Return a messy spelling of ``value`` (never identical to it)."""
    variants = [
        value.upper(),
        value.capitalize(),
        " " + value,
        value + " ",
        " " + value.capitalize() + " ",
        value.upper() + "  ",
    ]
    return rng.choice(variants)


def generate(seed: int = DEFAULT_SEED, start: str = DEFAULT_START, days: int = DEFAULT_DAYS,
             n_customers: int = DEFAULT_CUSTOMERS, n_orders: int = DEFAULT_ORDERS):
    if days < 5:
        raise ValueError("--days must be >= 5")
    if n_customers < 5:
        raise ValueError("--customers must be >= 5")
    if n_orders < days:
        raise ValueError("--orders must be >= --days")

    rng = random.Random(seed)
    start_date = dt.date.fromisoformat(start)

    # ---------------- customers ----------------
    used_names = set()
    customers = []
    for cid in range(1, n_customers + 1):
        while True:
            first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
            if (first, last) not in used_names:
                used_names.add((first, last))
                break
        full_name = f"{first} {last}"
        email = f"{first.lower()}.{last.lower()}@example.com"
        country = rng.choice(COUNTRIES)
        if rng.random() < 0.15:
            full_name += rng.choice([" ", "  "])
        if rng.random() < 0.20:
            email = rng.choice([email.upper(), " " + email, email + " ", " " + email.upper()])
        if rng.random() < 0.15:
            country = rng.choice([country.lower(), " " + country, country.lower() + " "])
        signup = start_date - dt.timedelta(days=rng.randint(5, 400))
        customers.append({
            "customer_id": cid,
            "full_name": full_name,
            "email": email,
            "country": country,
            "signup_date": signup.isoformat(),
        })
    # Guarantee at least one of each kind of messy customer value.
    customers[0]["full_name"] = customers[0]["full_name"].rstrip(" ") + " "
    customers[1]["email"] = customers[1]["email"].strip(" ").upper() + " "
    customers[2]["country"] = " " + customers[2]["country"].strip(" ").lower()

    # ---------------- products ----------------
    products = []
    for pid, (name, category, base) in enumerate(PRODUCTS, start=1):
        price = Decimal(round(base * (0.9 + 0.2 * rng.random()))) - CENT
        if rng.random() < 0.4:
            category = messy_variant(rng, category)
        products.append({
            "product_id": pid,
            "product_name": name,
            "category": category,
            "unit_price": price.quantize(CENT),
        })
    products[0]["category"] = products[0]["category"].strip(" ").upper()

    # ---------------- orders ----------------
    customer_ids = [c["customer_id"] for c in customers]
    dormant = set(rng.sample(customer_ids, 2))
    active = [c for c in customer_ids if c not in dormant]

    day_indexes = list(range(days))
    empty_day = rng.randrange(1, days - 1)
    cancelled_day = rng.choice([d for d in day_indexes if d != empty_day])
    normal_days = [d for d in day_indexes if d not in (empty_day, cancelled_day)]

    # One order on the cancelled-only day, the rest spread over normal days
    # (every normal day gets at least one order).
    order_days = [cancelled_day] + list(normal_days)
    for _ in range(n_orders - len(order_days)):
        order_days.append(rng.choice(normal_days))

    raw = []
    for day in order_days:
        ts = dt.datetime.combine(start_date + dt.timedelta(days=day), dt.time()) + dt.timedelta(
            seconds=rng.randint(7 * 3600, 23 * 3600 - 1))
        product = rng.choice(products)
        qty = rng.choices([1, 2, 3, 4], weights=[55, 25, 12, 8])[0]
        if day == cancelled_day:
            status = "cancelled"
        else:
            status = rng.choices(CANONICAL_STATUSES, weights=STATUS_WEIGHTS)[0]
        raw.append({
            "ts": ts,
            "customer_id": rng.choice(active),
            "product_id": product["product_id"],
            "quantity": qty,
            "amount": (product["unit_price"] * qty).quantize(CENT, rounding=ROUND_HALF_UP),
            "status": status,
            "day": day,
        })
    raw.sort(key=lambda r: (r["ts"], r["product_id"], r["customer_id"]))

    # Every canonical status must appear at least once (outside the cancelled day).
    normal_idx = [i for i, r in enumerate(raw) if r["day"] != cancelled_day]
    for status in CANONICAL_STATUSES:
        if not any(r["status"] == status for r in raw):
            raw[rng.choice(normal_idx)]["status"] = status

    # Messy spellings: ~25% of rows, plus at least one per canonical status.
    spelled = [r["status"] for r in raw]
    for i, r in enumerate(raw):
        if rng.random() < 0.25:
            spelled[i] = messy_variant(rng, r["status"])
    for status in CANONICAL_STATUSES:
        idx = [i for i, r in enumerate(raw) if r["status"] == status]
        if all(spelled[i] == status for i in idx):
            i = rng.choice(idx)
            spelled[i] = messy_variant(rng, status)

    orders = []
    for n, (r, s) in enumerate(zip(raw, spelled)):
        orders.append({
            "order_id": 1001 + n,
            "customer_id": r["customer_id"],
            "product_id": r["product_id"],
            "order_ts": r["ts"].strftime("%Y-%m-%d %H:%M:%S"),
            "quantity": r["quantity"],
            "amount": f"{r['amount']:.2f}",
            "status": s,
        })

    for p in products:
        p["unit_price"] = f"{p['unit_price']:.2f}"
    return customers, products, orders


def write_csv(path: str, rows, fieldnames) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


CUSTOMER_FIELDS = ["customer_id", "full_name", "email", "country", "signup_date"]
PRODUCT_FIELDS = ["product_id", "product_name", "category", "unit_price"]
SEED_FILE = "SEED"
ORDER_FIELDS = ["order_id", "customer_id", "product_id", "order_ts", "quantity", "amount", "status"]


def write_all(out_dir: str, **kwargs) -> dict:
    customers, products, orders = generate(**kwargs)
    os.makedirs(out_dir, exist_ok=True)
    paths = {
        "customers": os.path.join(out_dir, "customers.csv"),
        "products": os.path.join(out_dir, "products.csv"),
        "orders": os.path.join(out_dir, "orders.csv"),
    }
    write_csv(paths["customers"], customers, CUSTOMER_FIELDS)
    write_csv(paths["products"], products, PRODUCT_FIELDS)
    write_csv(paths["orders"], orders, ORDER_FIELDS)
    paths["seed"] = os.path.join(out_dir, SEED_FILE)
    with open(paths["seed"], "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"{kwargs.get('seed', DEFAULT_SEED)}\n")
    return paths


def main(argv=None) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="dataset seed (default %(default)s)")
    parser.add_argument("--out", default=os.path.join(here, "..", "seeds"), help="output directory (default: <repo>/seeds)")
    parser.add_argument("--start", default=DEFAULT_START, help="first order date (default %(default)s)")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="number of days (default %(default)s)")
    parser.add_argument("--customers", type=int, default=DEFAULT_CUSTOMERS, help="number of customers (default %(default)s)")
    parser.add_argument("--orders", type=int, default=DEFAULT_ORDERS, help="number of orders (default %(default)s)")
    args = parser.parse_args(argv)
    paths = write_all(os.path.abspath(args.out), seed=args.seed, start=args.start, days=args.days,
                      n_customers=args.customers, n_orders=args.orders)
    for name, path in paths.items():
        print(f"wrote {name:<9} -> {os.path.relpath(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
