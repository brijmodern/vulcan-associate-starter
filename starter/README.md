# Vulcan Associate (Beginner) - Hands-on Lab: Northwind Gear

**Time box:** 3 hours · **Engine:** Postgres (Vulcan Docker setup) · **Pass mark:** 70 / 100

Northwind Gear is a small online shop that sells outdoor gear. Every day it exports three CSV files
from its shop system: customers, products and orders. The exports are messy. Order statuses come in
mixed case with stray spaces, some emails are upper-case, and so on.

The finance team wants two trustworthy tables:

* **daily revenue**, one row per day, and
* **customer orders**, one row per customer, with lifetime value.

Your job is to build them as a Vulcan project: load the CSVs, clean them, build the marts, protect
them with audits and a unit test, keep the project lint-clean, and deploy it to a `dev` environment.

The grader is automated. **Use the exact model names, column names, types and grains below.** If a
name is wrong, the check for it scores zero.

Reference: the Vulcan book, <https://tmdc-io.github.io/vulcan-book/>. Vulcan is derived from SQLMesh,
but some keywords differ (for example, models attach audits with `assertions (...)` and declare their
grain with `grains (...)`). Trust the Vulcan book over SQLMesh docs.

---

## 0. Setup (about 15 minutes)

Follow *Get started → Docker* in the Vulcan book. This repo already contains the pieces:

| File | Purpose |
|---|---|
| `docker-compose.infra.yml` | `warehouse` (Postgres 15, host port 5434) and `statestore` (Postgres 15, host port 5433) on the `vulcan` network |
| `.env.example` | connection settings read by `config.yaml` through `env_var(...)`. Copy it to `.env` |
| `Makefile` | wraps the Vulcan CLI image (`tmdcio/vulcan-postgres:0.228.1.19`) and passes `.env` into the container |
| `config.yaml` | skeleton project config: Postgres gateway, `model_defaults`, linter enabled |
| `seeds/` | the three CSVs you must load (`customers.csv`, `products.csv`, `orders.csv`) |
| `tools/generate_data.py` | regenerates the CSVs for your assigned dataset seed |

```bash
cp .env.example .env
make network up          # docker network + warehouse + statestore
make data SEED=<your assigned seed>   # IMPORTANT: use the seed in your invitation email
make info                # must show a successful connection
```

> If you prefer the book's `vulcan` shell alias, add `--env-file .env` to it so the `env_var(...)`
> values in `config.yaml` reach the container. You can also use the global `--dotenv .env` option.

**Your dataset is personal.** The grader checks that `seeds/*.csv` match the seed you were assigned.
Do not edit the CSVs by hand.

---

## 1. What to build

Create your models under `models/` (subfolders are fine), custom audits under `audits/`, and unit
tests under `tests/`.

### (a) Seed models: `kind SEED`

| Model | CSV | Columns (type) |
|---|---|---|
| `raw.customers` | `seeds/customers.csv` | `customer_id INT`, `full_name TEXT`, `email TEXT`, `country TEXT`, `signup_date DATE` |
| `raw.products` | `seeds/products.csv` | `product_id INT`, `product_name TEXT`, `category TEXT`, `unit_price DECIMAL(10,2)` |
| `raw.orders` | `seeds/orders.csv` | `order_id INT`, `customer_id INT`, `product_id INT`, `order_ts TIMESTAMP`, `quantity INT`, `amount DECIMAL(10,2)`, `status TEXT` |

Declare the `columns (...)` explicitly. The seed `path` is relative to the model file.

### (b) Staging models: `kind VIEW`

Each staging model reads from its raw model only. It renames, casts and cleans. Columns must appear
**in this order**, with these names:

| Model | Grain | Columns and rules |
|---|---|---|
| `stg.customers` | `customer_id` | `customer_id INT`; `customer_name TEXT` = `full_name` with leading and trailing spaces removed; `email TEXT` = trimmed and **lower-case**; `country TEXT` = trimmed and **upper-case**; `signup_date DATE` |
| `stg.products` | `product_id` | `product_id INT`; `product_name TEXT` (trimmed); `category TEXT` = trimmed and **lower-case**; `unit_price DECIMAL(10,2)` |
| `stg.orders` | `order_id` | `order_id INT`; `customer_id INT`; `product_id INT`; `ordered_at TIMESTAMP` (= `order_ts`); `order_date DATE` (= the date part of `order_ts`); `quantity INT`; `order_amount DECIMAL(10,2)` (= `amount`); `status TEXT` = trimmed and **lower-case** |

After cleaning, `stg.orders.status` contains only these values: `completed`, `shipped`, `pending`,
`cancelled`, `returned`.

### (c) Marts: `kind FULL`

**Revenue-recognised orders** are orders whose cleaned status is `completed` or `shipped`. Orders in
any other status count toward `order_count`, but not toward revenue or items sold.

**`marts.daily_revenue`**, grain `order_date`: one row per calendar day that has **at least one
order of any status**. Days with no orders have no row.

| Column | Type | Definition |
|---|---|---|
| `order_date` | `DATE` | the day |
| `order_count` | `BIGINT` | number of orders that day, any status |
| `completed_order_count` | `BIGINT` | number of revenue-recognised orders that day |
| `items_sold` | `BIGINT` | sum of `quantity` over revenue-recognised orders. `0` if there are none |
| `revenue` | `DECIMAL(12,2)` | sum of `order_amount` over revenue-recognised orders. `0` if there are none, never NULL |

**`marts.customer_orders`**, grain `customer_id`: one row per customer in `stg.customers`,
**including customers who never ordered**.

| Column | Type | Definition |
|---|---|---|
| `customer_id` | `INT` | |
| `customer_name` | `TEXT` | from `stg.customers` |
| `email` | `TEXT` | from `stg.customers` (cleaned) |
| `order_count` | `BIGINT` | number of orders, any status. `0` if none |
| `completed_order_count` | `BIGINT` | number of revenue-recognised orders. `0` if none |
| `lifetime_revenue` | `DECIMAL(12,2)` | sum of `order_amount` over revenue-recognised orders. `0` if none |
| `first_order_date` | `DATE` | earliest `order_date` of any status. `NULL` if no orders |
| `last_order_date` | `DATE` | latest `order_date` of any status. `NULL` if no orders |

Declare the grain on both marts with `grains (...)`.

### (d) Audits

Attach audits to your models with `assertions (...)`. You need at least:

1. a **not-null** audit on key columns (built-in `not_null`),
2. a **uniqueness** audit on a model's grain (built-in `unique_values` or `unique_combination_of_columns`),
3. one **custom audit** in `audits/` that fails if any order has a **negative order amount**.
   Attach it to a model so that a negative amount in `orders.csv` **blocks the plan**.

All audits must pass on your data.

### (e) Unit test

Add at least one unit test in `tests/` (file name `test_*.yaml`) whose `model:` is
`marts.daily_revenue`. Mock its upstream input or inputs with `rows`, and assert the output. Include
at least one non-revenue status in the mock data. `vulcan test` must pass.

### (f) Lint and format

Keep the `linter` block in `config.yaml`. The grader lints with these rules:
`ambiguousorinvalidcolumn`, `invalidselectstarexpansion`, `noselectstar`. That means no top-level
`SELECT *` in models. Run `vulcan format` before you submit. The grader runs `vulcan format --check`.

### (g) Plan and run in `dev`

```bash
make plan-dev        # vulcan plan dev (review it, then apply)
make run-dev         # vulcan run dev
```

Then spot-check your output, for example:
`vulcan fetchdf "SELECT * FROM marts__dev.daily_revenue ORDER BY order_date"`.

---

## 2. Submission

1. Make sure `.env`, `.state/` and `.cache/` are **not** committed (see `.gitignore`).
2. Commit everything, including your `seeds/` exactly as generated with your seed.
3. Push to your lab repository and tag the commit:

```bash
git tag submission-v1
git push origin main --tags
```

Pushing the tag triggers the grading workflow. The report appears in the Actions run, both as the job
summary and as the `grading-report` artifact. If you need to resubmit, use `submission-v2` and so on.
**The highest-numbered tag pushed before the deadline is graded.**

---

## 3. How you are scored (100 points, pass mark 70)

The grader copies your repo and deploys it to a fresh environment called `grading`. It uses its own
database connection; it replaces the `gateways` section of your `config.yaml`, so your `.env` values
don't matter.

| # | Stage | Points | What is checked |
|---|---|---:|---|
| 1 | Builds **(critical)** | 10 | `vulcan info` succeeds: the project loads and the connection works |
| 2 | Lint | 10 | `vulcan lint` reports no errors with the rule set above |
| 3 | Format | 5 | `vulcan format --check` reports no changes needed |
| 4 | Plan + run **(critical)** | 15 | `vulcan plan grading` applies non-interactively (10), then `vulcan run grading` succeeds (5) |
| 5 | Your tests pass | 10 | `vulcan test` succeeds |
| 6 | Test exists | 5 | at least 1 unit test for `marts.daily_revenue` |
| 7 | Your audits pass | 5 | `vulcan audit` succeeds |
| 8 | Audits exist | 5 | not-null audit, uniqueness audit, and a custom audit in `audits/`, all attached to models |
| 9 | Required models and kinds | 10 | all 8 models above exist with the right kind, and both marts declare the right grain |
| 10 | Hidden correctness **(critical)** | 25 | the grader queries your tables and compares them with results it computes independently from your CSVs. It also injects a bad row into a copy of your data and checks that your audits block it |
|   | **Total** | **100** | |

**Critical stages:** if Builds, Plan + run, or Hidden correctness scores 0, the submission **fails
whatever the total**.

Tips:

* Hidden correctness compares values exactly, with revenue to the cent. Watch case, whitespace,
  `NULL` vs `0`, and days or customers with no orders.
* The grader adds `--skip-tests` to `plan`, so tests are scored only in stage 5. Your plan still has
  to pass your own audits.
* Before you tag, run the whole sequence locally: `make info lint format-check test plan-dev run-dev audit`.
