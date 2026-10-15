"""Hidden-correctness checks (25 points) for the Vulcan Associate (Beginner) lab.

"Hidden" means the candidate's own tests don't cover these: the grader checks
the built tables against results it computes itself from the candidate's seeds.
Each check queries the candidate's tables in the grading environment and
compares them to the results computed by ``expected.py``. Checks are all-or-nothing; mismatches are listed in
the report (first few) so appeals can be decided from the report alone.

Point allocation (must sum to 25 together with the injection check):
  dr_dates            4   marts.daily_revenue has exactly the expected set of days
  dr_revenue          5   revenue per day, to the cent
  dr_counts           3   order_count / completed_order_count / items_sold per day
  co_rows             3   marts.customer_orders has every customer (incl. 0-order ones)
  co_counts_revenue   2   order counts and lifetime_revenue per customer
  co_dates            1   first/last order date per customer (NULL when no orders)
  co_identity         1   customer_name trimmed, email trimmed + lower-cased
  stg_status          2   stg.orders statuses normalised; counts per status match
  grain_unique        2   1 point per mart: no duplicate grain rows
  bad_data_blocked    2   (run by grade.py) negative-amount row blocks the plan
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

CENT_TOLERANCE = Decimal("0.005")
# VERIFY: a plan blocked by a failing audit prints the word "audit" (or "assertion")
#         in its output, e.g. "Audit 'x' for model 'y' failed. Got 3 results, expected 0."
#         (wording taken from the Vulcan book audits page for `vulcan audit`).
INJECTION_AUDIT_MARKERS = ("audit", "assertion")
MAX_MISMATCHES = 5

CHECK_POINTS = {
    "dr_dates": 4,
    "dr_revenue": 5,
    "dr_counts": 3,
    "co_rows": 3,
    "co_counts_revenue": 2,
    "co_dates": 1,
    "co_identity": 1,
    "stg_status": 2,
    "grain_unique": 2,
    "bad_data_blocked": 2,
}
assert sum(CHECK_POINTS.values()) == 25


class QueryError(Exception):
    pass


def _dec(v):
    if v is None:
        return None
    try:
        return Decimal(str(v).strip())
    except InvalidOperation:
        return None


def _int(v):
    d = _dec(v)
    if d is None or d != d.to_integral_value():
        return None
    return int(d)


def _date(v):
    if v is None:
        return None
    return str(v).strip()[:10]


def _check(cid, title, ok, message, mismatches=None, points=None):
    max_pts = CHECK_POINTS[cid]
    return {
        "id": cid,
        "title": title,
        "max": max_pts,
        "points": (max_pts if ok else 0) if points is None else points,
        "ok": bool(ok),
        "message": message,
        "mismatches": (mismatches or [])[:MAX_MISMATCHES],
        "mismatch_count": len(mismatches or []),
    }


def _fetch(query, sql):
    try:
        cols, rows = query(sql)
        return cols, rows, None
    except Exception as exc:  # noqa: BLE001 - any DB error means the check fails
        return None, None, f"{type(exc).__name__}: {str(exc).strip().splitlines()[0] if str(exc).strip() else ''}"


def run_correctness_checks(query, table_for, expected):
    """Run all DB-backed hidden checks.

    query(sql) -> (column_names, rows) with every value as str or None.
    table_for(model_name) -> SQL relation string (already quoted) or None.
    expected -> dict from hidden.expected.compute_expected().
    """
    results = []
    exp_dr = expected["daily_revenue"]
    exp_co = expected["customer_orders"]

    dr = table_for("marts.daily_revenue")
    co = table_for("marts.customer_orders")
    so = table_for("stg.orders")

    # ------------------------------------------------------------------ daily_revenue
    if dr is None:
        for cid, title in (("dr_dates", "daily_revenue: set of days"),
                           ("dr_revenue", "daily_revenue: revenue per day"),
                           ("dr_counts", "daily_revenue: counts per day")):
            results.append(_check(cid, title, False, "marts.daily_revenue not found in grading environment"))
    else:
        _c, rows, err = _fetch(query, f"SELECT order_date, order_count, completed_order_count, items_sold, revenue FROM {dr}")
        if err:
            msg = f"query failed (missing/renamed column?): {err}"
            results.append(_check("dr_dates", "daily_revenue: set of days", False, msg))
            results.append(_check("dr_revenue", "daily_revenue: revenue per day", False, msg))
            results.append(_check("dr_counts", "daily_revenue: counts per day", False, msg))
        else:
            actual = {}
            for r in rows:
                actual.setdefault(_date(r[0]), []).append(r)
            exp_days, act_days = set(exp_dr), set(actual)
            missing = sorted(exp_days - act_days)
            extra = sorted(d for d in act_days - exp_days)
            mism = [f"missing day {d}" for d in missing] + [f"unexpected day {d}" for d in extra]
            results.append(_check(
                "dr_dates", "daily_revenue: set of days", not mism,
                f"expected {len(exp_days)} days, got {len(act_days)} distinct days ({len(rows)} rows)", mism))

            rev_mism, cnt_mism = [], []
            for day, exp in exp_dr.items():
                got = actual.get(day)
                if not got:
                    rev_mism.append(f"{day}: no row")
                    cnt_mism.append(f"{day}: no row")
                    continue
                r = got[0]
                rev = _dec(r[4])
                if rev is None or abs(rev - exp["revenue"]) > CENT_TOLERANCE:
                    rev_mism.append(f"{day}: revenue expected {exp['revenue']:.2f}, got {r[4]}")
                for idx, key in ((1, "order_count"), (2, "completed_order_count"), (3, "items_sold")):
                    if _int(r[idx]) != exp[key]:
                        cnt_mism.append(f"{day}: {key} expected {exp[key]}, got {r[idx]}")
            results.append(_check("dr_revenue", "daily_revenue: revenue per day (to the cent, 0 not NULL)",
                                  not rev_mism, f"{len(rev_mism)} day(s) wrong", rev_mism))
            results.append(_check("dr_counts", "daily_revenue: order_count / completed_order_count / items_sold",
                                  not cnt_mism, f"{len(cnt_mism)} value(s) wrong", cnt_mism))

    # ------------------------------------------------------------------ customer_orders
    co_ids = ("co_rows", "co_counts_revenue", "co_dates", "co_identity")
    co_titles = {
        "co_rows": "customer_orders: one row per customer (incl. customers with no orders)",
        "co_counts_revenue": "customer_orders: order counts and lifetime_revenue",
        "co_dates": "customer_orders: first/last order date (NULL if none)",
        "co_identity": "customer_orders: customer_name trimmed, email cleaned",
    }
    if co is None:
        for cid in co_ids:
            results.append(_check(cid, co_titles[cid], False, "marts.customer_orders not found in grading environment"))
    else:
        _c, rows, err = _fetch(query, (
            "SELECT customer_id, customer_name, email, order_count, completed_order_count, "
            f"lifetime_revenue, first_order_date, last_order_date FROM {co}"))
        if err:
            for cid in co_ids:
                results.append(_check(cid, co_titles[cid], False, f"query failed (missing/renamed column?): {err}"))
        else:
            actual = {}
            for r in rows:
                actual.setdefault(_int(r[0]), []).append(r)
            exp_ids, act_ids = set(exp_co), set(actual)
            mism = [f"missing customer_id {c}" for c in sorted(exp_ids - act_ids)]
            mism += [f"unexpected customer_id {c}" for c in sorted(x for x in act_ids - exp_ids if x is not None)]
            if None in act_ids:
                mism.append("row(s) with NULL/non-integer customer_id")
            zero = sum(1 for v in exp_co.values() if v["order_count"] == 0)
            results.append(_check("co_rows", co_titles["co_rows"], not mism,
                                  f"expected {len(exp_ids)} customers ({zero} with no orders), got {len(act_ids)}", mism))

            cnt, dates, ident = [], [], []
            for cid, exp in exp_co.items():
                got = actual.get(cid)
                if not got:
                    cnt.append(f"customer {cid}: no row")
                    dates.append(f"customer {cid}: no row")
                    ident.append(f"customer {cid}: no row")
                    continue
                r = got[0]
                if _int(r[3]) != exp["order_count"]:
                    cnt.append(f"customer {cid}: order_count expected {exp['order_count']}, got {r[3]}")
                if _int(r[4]) != exp["completed_order_count"]:
                    cnt.append(f"customer {cid}: completed_order_count expected {exp['completed_order_count']}, got {r[4]}")
                rev = _dec(r[5])
                if rev is None or abs(rev - exp["lifetime_revenue"]) > CENT_TOLERANCE:
                    cnt.append(f"customer {cid}: lifetime_revenue expected {exp['lifetime_revenue']:.2f}, got {r[5]}")
                if _date(r[6]) != exp["first_order_date"]:
                    dates.append(f"customer {cid}: first_order_date expected {exp['first_order_date']}, got {r[6]}")
                if _date(r[7]) != exp["last_order_date"]:
                    dates.append(f"customer {cid}: last_order_date expected {exp['last_order_date']}, got {r[7]}")
                if r[1] != exp["customer_name"]:
                    ident.append(f"customer {cid}: customer_name expected {exp['customer_name']!r}, got {r[1]!r}")
                if r[2] != exp["email"]:
                    ident.append(f"customer {cid}: email expected {exp['email']!r}, got {r[2]!r}")
            results.append(_check("co_counts_revenue", co_titles["co_counts_revenue"], not cnt, f"{len(cnt)} value(s) wrong", cnt))
            results.append(_check("co_dates", co_titles["co_dates"], not dates, f"{len(dates)} value(s) wrong", dates))
            results.append(_check("co_identity", co_titles["co_identity"], not ident, f"{len(ident)} value(s) wrong", ident))

    # ------------------------------------------------------------------ stg.orders status normalisation
    if so is None:
        results.append(_check("stg_status", "stg.orders: status normalised", False, "stg.orders not found in grading environment"))
    else:
        _c, rows, err = _fetch(query, f"SELECT status, COUNT(*) FROM {so} GROUP BY status")
        if err:
            results.append(_check("stg_status", "stg.orders: status normalised", False, f"query failed: {err}"))
        else:
            got = {r[0]: _int(r[1]) for r in rows}
            exp = expected["status_counts"]
            mism = []
            for s in sorted(set(got) | set(exp), key=lambda x: (x is None, str(x))):
                if got.get(s) != exp.get(s):
                    mism.append(f"status {s!r}: expected {exp.get(s, 0)} rows, got {got.get(s, 0)}")
            results.append(_check("stg_status", "stg.orders: status trimmed + lower-cased (counts per status)",
                                  not mism, f"distinct statuses found: {sorted(str(k) for k in got)}", mism))

    # ------------------------------------------------------------------ grain uniqueness
    pts, msgs = 0, []
    for rel, grain, label in ((dr, "order_date", "marts.daily_revenue"), (co, "customer_id", "marts.customer_orders")):
        if rel is None:
            msgs.append(f"{label}: not found")
            continue
        _c, rows, err = _fetch(query, f"SELECT COUNT(*), COUNT(DISTINCT {grain}), COUNT({grain}) FROM {rel}")
        if err:
            msgs.append(f"{label}: query failed: {err}")
            continue
        total, distinct, nonnull = (_int(x) for x in rows[0])
        if total == distinct == nonnull and total and total > 0:
            pts += 1
            msgs.append(f"{label}: {total} rows, grain unique")
        else:
            msgs.append(f"{label}: {total} rows, {distinct} distinct {grain}, {nonnull} non-null")
    results.append(_check("grain_unique", "grain uniqueness (1 point per mart)", pts == 2, "; ".join(msgs), points=pts))

    return results


def injection_result(plan_rc, output, injected_row, skipped_reason=None):
    title = "bad-data injection: negative-amount order blocks the plan"
    if skipped_reason:
        return _check("bad_data_blocked", title, False, f"not run: {skipped_reason}")
    low = (output or "").lower()
    blocked = plan_rc != 0
    mentions_audit = any(m in low for m in INJECTION_AUDIT_MARKERS)
    if blocked and mentions_audit:
        return _check("bad_data_blocked", title, True,
                      f"plan failed as required (exit {plan_rc}) and an audit was reported; injected order_id {injected_row['order_id']} amount {injected_row['amount']}")
    if blocked:
        return _check("bad_data_blocked", title, False,
                      f"plan failed (exit {plan_rc}) but no audit failure was reported -- failed for another reason; review log")
    return _check("bad_data_blocked", title, False,
                  f"plan SUCCEEDED with a negative amount (order_id {injected_row['order_id']}, amount {injected_row['amount']}); no audit blocked it")
