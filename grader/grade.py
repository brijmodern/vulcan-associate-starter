#!/usr/bin/env python3
"""Automated grader for the Vulcan Associate (Beginner) lab -- Northwind Gear.

Candidates run it on their own machine with `make grade`. That runs this
script inside the Vulcan Docker image against the candidate's local Postgres,
and writes grading-report/report.json (uploaded to the course page) and
grading-report/report.md (human-readable).

Usage
-----
  python3 grader/grade.py --submission . --out grading-report
                          [--dry-run] [--assigned-seed N] [--env grading]
                          [--stages builds,lint,...] [--skip-injection] [--no-render]
                          [--keep-workdir] [--workdir DIR]
  python3 grader/grade.py --print-checksum

Isolation: the grader never touches the candidate's repo, dev environment or
state. It copies the repo to a temporary directory, replaces the `gateways`
section of the copy's config.yaml, and plans into a separate, freshly
re-created database (default `vulcan_grading`) on the same Postgres server.
Vulcan state for the grading run also lives in that database.

Stages (100 points; pass mark 70; * = critical gate, must score > 0)
  builds*        10  vulcan info
  lint           10  vulcan lint
  format          5  vulcan format --check
  plan_run*      15  vulcan plan <env> (non-interactive) 10 + vulcan run <env> 5
  tests_pass     10  vulcan test
  tests_exist     5  >= 1 unit test for marts.daily_revenue
  audits_pass     5  vulcan audit
  audits_exist    5  not_null (1) + uniqueness (1) + custom audit attached (3)
  models_present 10  8 required models with right kind (8) + mart grains (2)
  hidden*        25  correctness checks vs. results computed from your seeds + bad-data injection

--dry-run skips every Vulcan and database call. It validates the repo structure,
runs the static stages and computes the expected results.

Dependencies: Python 3.8+ standard library, plus psycopg2, psycopg (3) or the
`psql` CLI for database access. PyYAML or ruamel.yaml is used to parse unit-test
files when available; otherwise a simple line parser is used.

Environment variables
---------------------
  VULCAN_CMD            command used to invoke Vulcan (default "vulcan"). "{workdir}" is
                        replaced by the quoted work directory.
  GRADER_PG_HOST/PORT/USER/PASSWORD
                        Postgres server as seen by Vulcan. Defaults: WAREHOUSE_HOST/PORT/USER/
                        PASSWORD from .env, else warehouse/5432/vulcan/vulcan.
  GRADER_PG_DB          grading database (default vulcan_grading). Re-created on every run.
  GRADER_PG_ADMIN_DB    existing database used to (re)create GRADER_PG_DB
                        (default WAREHOUSE_DB, else warehouse)
  GRADER_RESET_DB       1 (default) = drop and re-create GRADER_PG_DB; 0 = create only if missing
  GRADER_QUERY_HOST/PORT/DB/USER/PASSWORD
                        same server as seen by this script (defaults to GRADER_PG_*)
  GRADER_STATE_TYPE     grading_db (default: state in GRADER_PG_DB) | duckdb | postgres
  GRADER_STATE_DUCKDB   duckdb state file when GRADER_STATE_TYPE=duckdb
  GRADER_STATE_HOST/PORT/DB/USER/PASSWORD   when GRADER_STATE_TYPE=postgres
  GRADER_CMD_TIMEOUT    seconds per Vulcan command (default 900)
  GRADE_COMMIT / GRADE_DIRTY / GRADE_TAGS
                        git facts computed on the host by the Makefile (git may not exist in
                        the image). Used in preference to running git.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True  # never leave __pycache__ (possibly root-owned) in the candidate's grader/
sys.path.insert(0, HERE)

import checks as hidden_checks  # noqa: E402
import expected as hidden_expected  # noqa: E402

GRADER_VERSION = "1.1.0"
REPORT_SCHEMA = "vulcan-associate-report/1"
PASS_MARK = 70

# =============================================================================
# Vulcan CLI invocation -- every CLI assumption lives here.
# Flags were checked against https://tmdc-io.github.io/vulcan-book/cli-commands/cli/
# Items marked "# VERIFY:" could not be confirmed from the docs / by running Vulcan.
# =============================================================================
VULCAN_CMD = os.environ.get("VULCAN_CMD", "vulcan")

INFO_ARGS = ["info"]
LINT_ARGS = ["lint"]
FORMAT_CHECK_ARGS = ["format", "--check"]
# VERIFY: `vulcan format --check` exits non-zero when any file would be reformatted
#         (docs only say it checks "without changing files").
TEST_ARGS = ["test"]
# VERIFY: `vulcan audit` has no environment option (docs list only --model/--start/--end/
#         --execution-time); assumed to audit the current project snapshots, whose tables
#         were built by the grading plan.
AUDIT_ARGS = ["audit"]
# Non-interactive plan into a fresh env. --auto-apply / --no-prompts / --skip-tests /
# --skip-linter are documented. Tests and lint are scored in their own stages.
# VERIFY: on a brand-new environment this combination never prompts (e.g. for a start
#         date) and backfills every model.
PLAN_ARGS = ["plan", "{env}", "--auto-apply", "--no-prompts", "--skip-tests", "--skip-linter"]
# --skip-janitor (documented) so the grading run never cleans anything up.
RUN_ARGS = ["run", "{env}", "--skip-janitor"]
RENDER_ARGS = ["render", "{model}"]
TABLE_NAME_ARGS = ["table_name", "--env", "{env}", "{model}"]

# VERIFY: every vulcan command exits non-zero on failure (the docs do not document exit
#         codes). As a safety net, these output markers also count as failure.
FAILURE_MARKERS = {
    "lint": ["linter detected errors"],
    "test": ["failed (", "test failures", "failures="],
}

# VERIFY: virtual-layer naming of non-prod environments. Assumed SQLMesh default
#         (environment_suffix_target = schema): model `marts.daily_revenue` in env
#         `grading` is exposed as view "marts__grading"."daily_revenue". If the view is
#         not found, the grader falls back to `vulcan table_name --env <env> <model>`.
ENV_SCHEMA_TEMPLATE = "{schema}__{env}"

# VERIFY: Vulcan accepts a Postgres state_connection that points at the same database as
#         the warehouse connection (the docker guide documents a Postgres state backend
#         in a separate `statestore` database).
DEFAULT_STATE_TYPE = "grading_db"

# Official linter configuration enforced by the grader (README section 1f).
GRADER_LINTER = {
    "enabled": True,
    "rules": ["ambiguousorinvalidcolumn", "invalidselectstarexpansion", "noselectstar"],
}

# =============================================================================
# Lab specification (mirrors README.md section 1)
# =============================================================================
REQUIRED_MODELS = [
    ("raw.customers", "SEED"),
    ("raw.products", "SEED"),
    ("raw.orders", "SEED"),
    ("stg.customers", "VIEW"),
    ("stg.products", "VIEW"),
    ("stg.orders", "VIEW"),
    ("marts.daily_revenue", "FULL"),
    ("marts.customer_orders", "FULL"),
]
REQUIRED_GRAINS = {
    "marts.daily_revenue": ["order_date"],
    "marts.customer_orders": ["customer_id"],
}
TEST_TARGET_MODEL = "marts.daily_revenue"
NOT_NULL_AUDITS = {"not_null"}
UNIQUE_AUDITS = {"unique_values", "unique_combination_of_columns"}
BUILTIN_AUDITS = {
    "forall", "number_of_rows", "not_null", "at_least_one", "not_null_proportion", "not_constant",
    "unique_values", "unique_combination_of_columns", "accepted_values", "not_accepted_values",
    "sequential_values", "accepted_range", "mutually_exclusive_ranges", "not_empty_string",
    "string_length_equal", "string_length_between", "valid_uuid", "valid_email", "valid_url",
    "valid_http_method", "match_regex_pattern_list", "not_match_regex_pattern_list",
    "match_like_pattern_list", "not_match_like_pattern_list", "mean_in_range", "stddev_in_range",
    "z_score", "kl_divergence", "chi_square",
}

STAGES = [
    # id, title, max points, critical
    ("builds", "Builds (vulcan info)", 10, True),
    ("lint", "Lint (vulcan lint)", 10, False),
    ("format", "Format check (vulcan format --check)", 5, False),
    ("plan_run", "Plan to fresh env + run", 15, True),
    ("tests_pass", "Candidate tests pass (vulcan test)", 10, False),
    ("tests_exist", "At least 1 unit test for marts.daily_revenue", 5, False),
    ("audits_pass", "Candidate audits pass (vulcan audit)", 5, False),
    ("audits_exist", "Audits exist: not_null + uniqueness + custom", 5, False),
    ("models_present", "Required models, kinds and grains", 10, False),
    ("hidden", "Hidden correctness tests", 25, True),
]
assert sum(s[2] for s in STAGES) == 100
STAGE_IDS = [s[0] for s in STAGES]
VULCAN_STAGES = {"builds", "lint", "format", "plan_run", "tests_pass", "audits_pass", "hidden"}

COPY_IGNORE = shutil.ignore_patterns(".git", ".state", ".cache", "logs", "__pycache__", ".env",
                                     "grading-report", "grading-output", "*.pyc", ".DS_Store")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")


# =============================================================================
# Grader checksum (embedded in the report; the course page compares it)
# =============================================================================
def grader_checksum(grader_dir=HERE):
    """sha256 over every *.py under grader/ (excluding __pycache__), in sorted
    relative-path order (POSIX separators, relative to the grader/ directory).
    Each file contributes "<relpath>\\n<sha256 hex of file bytes>\\n"."""
    files = []
    for dirpath, dirs, names in os.walk(grader_dir):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for n in names:
            if n.endswith(".py"):
                full = os.path.join(dirpath, n)
                files.append((os.path.relpath(full, grader_dir).replace(os.sep, "/"), full))
    h = hashlib.sha256()
    for rel, full in sorted(files):
        with open(full, "rb") as fh:
            h.update(f"{rel}\n{hashlib.sha256(fh.read()).hexdigest()}\n".encode("utf-8"))
    return "sha256:" + h.hexdigest()


# =============================================================================
# Helpers
# =============================================================================
def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def strip_ansi(text):
    return ANSI_RE.sub("", text or "")


def tail(text, n=4000):
    text = text or ""
    return text if len(text) <= n else "...[truncated]...\n" + text[-n:]


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read())
    return h.hexdigest()


def qident(name):
    return '"' + name.replace('"', '""') + '"'


class Settings:
    def __init__(self):
        e = os.environ.get
        self.pg = {
            "host": e("GRADER_PG_HOST") or e("WAREHOUSE_HOST") or "warehouse",
            "port": int(e("GRADER_PG_PORT") or e("WAREHOUSE_PORT") or "5432"),
            "database": e("GRADER_PG_DB") or "vulcan_grading",
            "user": e("GRADER_PG_USER") or e("WAREHOUSE_USER") or "vulcan",
            "password": e("GRADER_PG_PASSWORD") or e("WAREHOUSE_PASSWORD") or "vulcan",
        }
        self.admin_db = e("GRADER_PG_ADMIN_DB") or e("WAREHOUSE_DB") or "warehouse"
        self.reset_db = (e("GRADER_RESET_DB") or "1") not in ("0", "false", "no")
        self.query = {
            "host": e("GRADER_QUERY_HOST") or self.pg["host"],
            "port": int(e("GRADER_QUERY_PORT") or self.pg["port"]),
            "database": e("GRADER_QUERY_DB") or self.pg["database"],
            "user": e("GRADER_QUERY_USER") or self.pg["user"],
            "password": e("GRADER_QUERY_PASSWORD") or self.pg["password"],
        }
        self.state_type = (e("GRADER_STATE_TYPE") or DEFAULT_STATE_TYPE).lower()
        self.state_duckdb = e("GRADER_STATE_DUCKDB") or ".state/grader_state.db"
        self.state_pg = {
            "host": e("GRADER_STATE_HOST") or "statestore",
            "port": int(e("GRADER_STATE_PORT") or "5432"),
            "database": e("GRADER_STATE_DB") or "statestore",
            "user": e("GRADER_STATE_USER") or "vulcan",
            "password": e("GRADER_STATE_PASSWORD") or "vulcan",
        }
        self.timeout = int(e("GRADER_CMD_TIMEOUT") or "900")

    def state_connection(self):
        if self.state_type == "duckdb":
            return {"type": "duckdb", "database": self.state_duckdb}
        if self.state_type == "postgres":
            return dict(type="postgres", **self.state_pg)
        return dict(type="postgres", **self.pg)  # grading_db: state lives in the fresh grading database

    def child_env(self):
        """Env for Vulcan subprocesses. Also overrides the starter's env_var() names so that,
        even if the config rewrite failed, nothing points at the candidate's dev databases."""
        env = dict(os.environ)
        st = self.state_connection()
        env.update({
            "NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200", "PYTHONUNBUFFERED": "1",
            "WAREHOUSE_HOST": self.pg["host"], "WAREHOUSE_PORT": str(self.pg["port"]),
            "WAREHOUSE_DB": self.pg["database"], "WAREHOUSE_USER": self.pg["user"],
            "WAREHOUSE_PASSWORD": self.pg["password"],
            "STATE_HOST": str(st.get("host", self.pg["host"])), "STATE_PORT": str(st.get("port", self.pg["port"])),
            "STATE_DB": str(st.get("database", self.pg["database"])), "STATE_USER": str(st.get("user", self.pg["user"])),
            "STATE_PASSWORD": str(st.get("password", self.pg["password"])),
        })
        return env


class VulcanRunner:
    def __init__(self, settings, log_dir):
        self.settings = settings
        self.log_dir = log_dir
        self.counter = 0

    def run(self, args, cwd, label, **fmt):
        self.counter += 1
        argv = [a.format(**fmt) for a in args]
        base = VULCAN_CMD.replace("{workdir}", shlex.quote(cwd))
        cmd = shlex.split(base) + argv
        started = dt.datetime.now()
        try:
            proc = subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, env=self.settings.child_env(),
                                  timeout=self.settings.timeout)
            rc, out = proc.returncode, proc.stdout.decode("utf-8", "replace")
        except FileNotFoundError as exc:
            rc, out = 127, f"[grader] could not execute {cmd[0]!r}: {exc}. Set VULCAN_CMD."
        except subprocess.TimeoutExpired as exc:
            rc = 124
            out = (exc.stdout or b"").decode("utf-8", "replace") + f"\n[grader] TIMEOUT after {self.settings.timeout}s"
        out = strip_ansi(out)
        secs = (dt.datetime.now() - started).total_seconds()
        log_path = os.path.join(self.log_dir, f"{self.counter:02d}_{label}.log")
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write(f"$ {' '.join(shlex.quote(c) for c in cmd)}\n# exit: {rc}  ({secs:.1f}s)\n\n{out}")
        # report shows only the vulcan arguments (no local paths); the log has the full command
        return {"cmd": "vulcan " + " ".join(shlex.quote(a) for a in argv),
                "rc": rc, "output": out, "seconds": round(secs, 1),
                "log": "logs/" + os.path.basename(log_path)}


def cmd_ok(res, kind=None):
    if res["rc"] != 0:
        return False
    low = res["output"].lower()
    return not any(m in low for m in FAILURE_MARKERS.get(kind, []))


# =============================================================================
# Postgres access (psycopg2 -> psycopg 3 -> psql CLI)
# =============================================================================
class PgClient:
    # VERIFY: psycopg2 is importable in tmdcio/vulcan-postgres:0.228.1.19 (Vulcan's Postgres
    #         engine adapter needs it); psql is a fallback and is probably NOT in the image.
    def __init__(self, cfg):
        self.cfg = cfg
        self.driver = None
        self.conn = None
        try:
            import psycopg2  # type: ignore
            self.conn = psycopg2.connect(host=cfg["host"], port=cfg["port"], dbname=cfg["database"],
                                         user=cfg["user"], password=cfg["password"], connect_timeout=15)
            self.conn.autocommit = True
            self.driver = "psycopg2"
        except ImportError:
            try:
                import psycopg  # type: ignore
                self.conn = psycopg.connect(host=cfg["host"], port=cfg["port"], dbname=cfg["database"],
                                            user=cfg["user"], password=cfg["password"], autocommit=True,
                                            connect_timeout=15)
                self.driver = "psycopg"
            except ImportError:
                if not shutil.which("psql"):
                    raise RuntimeError("no Postgres client: need psycopg2, psycopg or psql")
                self.driver = "psql"
                self.query("SELECT 1")

    def close(self):
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:  # noqa: BLE001
            pass

    def query(self, sql):
        if self.driver in ("psycopg", "psycopg2"):
            cur = self.conn.cursor()
            try:
                cur.execute(sql)
                if not cur.description:
                    return [], []
                cols = [d[0] for d in cur.description]
                rows = [tuple(None if v is None else str(v) for v in r) for r in cur.fetchall()]
            finally:
                cur.close()
            return cols, rows
        env = dict(os.environ, PGPASSWORD=self.cfg["password"], PGCONNECT_TIMEOUT="15")
        cmd = ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "--csv", "-P", "null=\\N",
               "-h", self.cfg["host"], "-p", str(self.cfg["port"]), "-U", self.cfg["user"],
               "-d", self.cfg["database"], "-c", sql]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=120)
        if proc.returncode != 0:
            raise hidden_checks.QueryError(proc.stderr.decode("utf-8", "replace").strip())
        rows = list(csv.reader(io.StringIO(proc.stdout.decode("utf-8"))))
        if not rows:
            return [], []
        return rows[0], [tuple(None if v == "\\N" else v for v in r) for r in rows[1:]]

    def relation_exists(self, rel):
        _c, rows = self.query(f"SELECT to_regclass('{rel.replace(chr(39), chr(39) * 2)}')")
        return bool(rows and rows[0][0])


def prepare_grading_database(settings):
    """(Re)create the isolated grading database. Returns (ok, message)."""
    target, admin = settings.pg["database"], settings.admin_db
    if target == admin or "grading" not in target.lower():
        return False, (f"refusing to (re)create database {target!r}: it must differ from {admin!r} "
                       "and contain 'grading'")
    cfg = dict(settings.query, database=admin)
    try:
        db = PgClient(cfg)
    except Exception as exc:  # noqa: BLE001
        return False, f"cannot connect to admin database {admin!r}: {exc}"
    try:
        _c, rows = db.query(f"SELECT 1 FROM pg_database WHERE datname = '{target.replace(chr(39), chr(39) * 2)}'")
        exists = bool(rows)
        if exists and settings.reset_db:
            try:
                db.query(f"DROP DATABASE IF EXISTS {qident(target)} WITH (FORCE)")
            except Exception:  # noqa: BLE001  (Postgres < 13 has no WITH (FORCE))
                db.query(f"DROP DATABASE IF EXISTS {qident(target)}")
            exists = False
        if not exists:
            db.query(f"CREATE DATABASE {qident(target)}")
            return True, f"grading database {target!r} {'re-created' if settings.reset_db else 'created'} (fresh)"
        return True, f"grading database {target!r} reused (GRADER_RESET_DB=0)"
    except Exception as exc:  # noqa: BLE001
        return False, f"could not prepare grading database {target!r}: {exc}"
    finally:
        db.close()


# =============================================================================
# Static analysis of the candidate repo
# =============================================================================
def _strip_sql_comments(text):
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"--[^\n]*", " ", text)


def _balanced(text, open_idx):
    """Given index of '(' return the inner text up to the matching ')'."""
    depth, i, quote = 0, open_idx, None
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i]
        i += 1
    return None


def _split_top(text):
    parts, depth, cur, quote = [], 0, [], None
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


def _ident(s):
    return s.strip().strip('"').strip("'").strip("`").lower()


def _list_value(value):
    v = value.strip()
    if v.startswith("(") or v.startswith("["):
        v = v[1:-1]
    return [p for p in (_split_top(v) if v else []) if p]


def parse_sql_model(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = _strip_sql_comments(fh.read())
    m = re.search(r"\bMODEL\s*\(", text, flags=re.I)
    if not m:
        return None
    inner = _balanced(text, m.end() - 1)
    if inner is None:
        return {"file": path, "name": None, "kind": "?", "grains": [], "audits": [], "inline_audits": [],
                "error": "unbalanced MODEL ( ... ) block"}
    props = {}
    for part in _split_top(inner):
        mm = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*(.*)$", part, flags=re.S)
        if mm:
            props[mm.group(1).lower()] = mm.group(2).strip()
    name = _ident(props.get("name", "")) or None
    km = re.match(r"\s*([A-Za-z_]+)", props.get("kind", "VIEW"))  # VIEW is the documented default kind
    kind = km.group(1).upper() if km else "?"
    grains = []
    for key in ("grains", "grain"):
        if key in props:
            grains += [_ident(g) for g in _list_value(props[key])]
    audits = []
    for key in ("assertions", "audits"):
        if key in props:
            for item in _list_value(props[key]):
                am = re.match(r"\s*([A-Za-z_][A-Za-z0-9_.]*)", item)
                if am:
                    audits.append(am.group(1).lower())
    inline_audits = [a.lower() for a in re.findall(r"\bAUDIT\s*\(\s*name\s+([A-Za-z0-9_.]+)", text, flags=re.I)]
    return {"file": path, "name": name, "kind": kind, "grains": grains, "audits": audits,
            "inline_audits": inline_audits}


def parse_python_model(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    if "@model" not in text:
        return None
    nm = re.search(r"""@model\(\s*["']([^"']+)["']""", text) or re.search(r"""name\s*=\s*["']([^"']+)["']""", text)
    km = re.search(r"ModelKindName\.([A-Z_]+)", text) or re.search(r"""kind\s*=\s*["']([A-Za-z_]+)""", text)
    gm = re.search(r"grains?\s*=\s*[\[(]([^\])]*)[\])]", text)
    return {"file": path, "name": nm.group(1).lower() if nm else None,
            "kind": km.group(1).upper() if km else "FULL",
            "grains": [_ident(g) for g in gm.group(1).split(",") if g.strip()] if gm else [],
            "audits": [], "inline_audits": []}


def scan_models(root):
    models = []
    for dirpath, _dirs, files in os.walk(os.path.join(root, "models")):
        for f in sorted(files):
            p = os.path.join(dirpath, f)
            parsed = parse_sql_model(p) if f.endswith(".sql") else parse_python_model(p) if f.endswith(".py") else None
            if parsed:
                parsed["file"] = os.path.relpath(p, root)
                models.append(parsed)
    return models


def scan_custom_audits(root, models):
    names = {}
    for dirpath, _dirs, files in os.walk(os.path.join(root, "audits")):
        for f in sorted(files):
            if f.endswith(".sql"):
                p = os.path.join(dirpath, f)
                with open(p, encoding="utf-8", errors="replace") as fh:
                    text = _strip_sql_comments(fh.read())
                for n in re.findall(r"\bAUDIT\s*\(\s*name\s+([A-Za-z0-9_.]+)", text, flags=re.I):
                    names[n.lower()] = os.path.relpath(p, root)
    inline = {}
    for m in models:
        for n in m.get("inline_audits", []):
            inline[n] = m["file"]
    return names, inline


def _load_yaml(text):
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except ImportError:
        pass
    try:
        from ruamel.yaml import YAML  # type: ignore
        return YAML(typ="safe").load(text)
    except ImportError:
        raise LookupError("no yaml library")


def scan_tests(root):
    tests, errors = [], []
    for dirpath, _dirs, files in os.walk(os.path.join(root, "tests")):
        for f in sorted(files):
            if not (f.startswith("test") and f.endswith((".yaml", ".yml"))):
                continue
            p = os.path.join(dirpath, f)
            rel = os.path.relpath(p, root)
            with open(p, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            try:
                doc = _load_yaml(text) or {}
                if isinstance(doc, dict):
                    for tname, body in doc.items():
                        if isinstance(body, dict) and body.get("model"):
                            tests.append({"file": rel, "name": str(tname), "model": _ident(str(body["model"]))})
                continue
            except LookupError:
                pass
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{rel}: YAML error: {exc}")
                continue
            cur = None  # line-based fallback
            for line in text.splitlines():
                tm = re.match(r"^([A-Za-z0-9_\-]+):\s*$", line)
                if tm:
                    cur = tm.group(1)
                mm = re.match(r"^\s+model:\s*['\"]?([A-Za-z0-9_.\"]+)", line)
                if mm and cur:
                    tests.append({"file": rel, "name": cur, "model": _ident(mm.group(1))})
    return tests, errors


def validate_structure(root):
    problems, notes = [], []
    if not os.path.isfile(os.path.join(root, "config.yaml")):
        if os.path.isfile(os.path.join(root, "config.yml")) or os.path.isfile(os.path.join(root, "config.py")):
            problems.append("config.yaml missing (config.yml/config.py found -- the lab requires config.yaml)")
        else:
            problems.append("config.yaml missing")
    for d in ("models", "seeds", "audits", "tests"):
        if not os.path.isdir(os.path.join(root, d)):
            problems.append(f"directory {d}/ missing")
    problems += hidden_expected.validate_seeds(os.path.join(root, "seeds"))
    return problems, notes


# =============================================================================
# Config rewrite (text based: no YAML library needed, Jinja elsewhere untouched)
# =============================================================================
_REPLACED_KEYS = ("gateways", "default_gateway", "linter")


def rewrite_config_text(text, settings):
    """Remove the top-level gateways / default_gateway / linter blocks and append the
    grader's own, as JSON flow mappings (valid YAML). Returns (new_text, notes)."""
    notes = []
    lines = text.splitlines()
    out, skipping = [], False
    linter_enabled = False
    for line in lines:
        top = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:", line)
        if top:
            skipping = top.group(1) in _REPLACED_KEYS
            if top.group(1) == "linter":
                linter_enabled = False
        elif skipping and re.match(r"^\s*enabled\s*:\s*(true|True|yes|on)\b", line):
            linter_enabled = True
        if not skipping:
            out.append(line)
    if not linter_enabled:
        notes.append("candidate config.yaml does not enable the linter (README requires it); grader enforces its own rules")
    if not re.search(r"^model_defaults\s*:", text, flags=re.M):
        out.append('model_defaults: {"dialect": "postgres"}')
        notes.append("model_defaults missing; grader set dialect: postgres")
    elif not re.search(r"^\s+dialect\s*:", text, flags=re.M):
        notes.append("model_defaults.dialect not found; Vulcan may reject the config")
    gateways = {"grading": {"connection": dict(type="postgres", **settings.pg),
                            "state_connection": settings.state_connection()}}
    out += ["", "# --- added by the Vulcan Associate grader (grading copy only) ---",
            "gateways: " + json.dumps(gateways),
            "default_gateway: grading",
            "linter: " + json.dumps(GRADER_LINTER), ""]
    return "\n".join(out), notes


def prepare_config(work, settings):
    path = os.path.join(work, "config.yaml")
    if not os.path.isfile(path):
        return ["config.yaml missing -- nothing to rewrite"]
    if settings.state_type == "duckdb":
        os.makedirs(os.path.join(work, os.path.dirname(settings.state_duckdb) or "."), exist_ok=True)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    new, notes = rewrite_config_text(text, settings)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new)
    notes.append("grading copy: config.yaml gateways replaced (isolated grading database); linter rules enforced")
    return notes


# =============================================================================
# Dataset: seeds/SEED + regenerate with the grader's own generator
# =============================================================================
def load_generator():
    p = os.path.join(HERE, "generate_data.py")
    spec = importlib.util.spec_from_file_location("grader_generate_data", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_seed_file(seeds_dir):
    p = os.path.join(seeds_dir, "SEED")
    if not os.path.isfile(p):
        return None, "seeds/SEED missing"
    try:
        with open(p, encoding="utf-8") as fh:
            return int(fh.read().strip()), None
    except ValueError:
        return None, "seeds/SEED is not a plain integer"


def csvs_match_seed(seeds_dir, seed, sha_map):
    tmp = tempfile.mkdtemp(prefix="grader-seed-")
    try:
        load_generator().write_all(tmp, seed=seed)
        diffs = [f for f in sha_map if sha_map[f] is None or sha256_file(os.path.join(tmp, f)) != sha_map[f]]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return not diffs, diffs


def dataset_info(root, assigned_seed):
    seeds = os.path.join(root, "seeds")
    info = {"seed": None, "seed_matches_csvs": None, "sha256": {}}
    for f in ("customers.csv", "products.csv", "orders.csv"):
        p = os.path.join(seeds, f)
        info["sha256"][f] = sha256_file(p) if os.path.isfile(p) else None
    seed, err = read_seed_file(seeds)
    info["seed"] = seed
    if err:
        info["seed_note"] = err
    if seed is not None:
        info["seed_matches_csvs"], info["mismatched_files"] = csvs_match_seed(seeds, seed, info["sha256"])
    if assigned_seed is not None:
        info["assigned_seed"] = assigned_seed
        info["assigned_seed_matches"] = (seed == assigned_seed and info["seed_matches_csvs"] is True)
    return info


# =============================================================================
# Git facts (host-computed env vars preferred; git as fallback)
# =============================================================================
def git_info(path):
    def g(*a):
        try:
            p = subprocess.run(["git", "-C", path] + list(a), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
        except Exception:  # noqa: BLE001
            return None
        return p.stdout.decode("utf-8", "replace") if p.returncode == 0 else None

    commit = os.environ.get("GRADE_COMMIT")
    dirty_env = os.environ.get("GRADE_DIRTY")
    tags_env = os.environ.get("GRADE_TAGS")
    source = "env" if commit is not None else "git"
    if commit is None:
        commit = (g("rev-parse", "HEAD") or "").strip()
    commit = commit.strip().lower()
    commit = commit if re.fullmatch(r"[0-9a-f]{40}", commit) else None

    if dirty_env is not None and dirty_env.strip().lower() in ("true", "false"):
        dirty = dirty_env.strip().lower() == "true"
    elif commit is not None:
        st = g("status", "--porcelain")
        dirty = None if st is None else bool(st.strip())
    else:
        dirty = None

    if tags_env is not None:
        tags = tags_env.split()
    else:
        out = g("tag", "--points-at", "HEAD") if commit else None
        tags = out.split() if out else []
    return {"commit": commit, "dirty": dirty, "tags": sorted(set(tags)), "source": source}


# =============================================================================
# Grading
# =============================================================================
class Grader:
    def __init__(self, args):
        self.args = args
        self.settings = Settings()
        self.submission = os.path.abspath(args.submission)
        self.out = os.path.abspath(args.out)
        self.log_dir = os.path.join(self.out, "logs")
        os.makedirs(self.log_dir, exist_ok=True)
        self.runner = VulcanRunner(self.settings, self.log_dir)
        self.env = args.env
        self.stages = {}
        for sid, title, mx, crit in STAGES:
            self.stages[sid] = {"id": sid, "title": title, "max": mx, "points": 0, "critical": crit,
                                "status": "not_run", "details": [], "checks": [], "commands": []}
        self.selected = set(STAGE_IDS) if not args.stages else {s.strip() for s in args.stages.split(",") if s.strip()}
        unknown = self.selected - set(STAGE_IDS)
        if unknown:
            raise SystemExit(f"unknown stage(s): {', '.join(sorted(unknown))}; valid: {', '.join(STAGE_IDS)}")
        self.flags, self.notes = [], []
        self.plan_ok = False
        self.db = None
        self.work_base = None
        self.work = self.submission

    def should_run(self, sid):
        if sid not in self.selected:
            self.stages[sid]["status"] = "skipped"
            self.stages[sid]["details"].append("stage not selected (--stages)")
            return False
        if self.args.dry_run and sid in VULCAN_STAGES:
            self.stages[sid]["status"] = "skipped"
            self.stages[sid]["details"].append("dry run: Vulcan/database not called")
            return False
        return True

    def finish(self, sid, points, details=None, checks=None):
        st = self.stages[sid]
        st["points"] = int(max(0, min(st["max"], points)))
        if details:
            st["details"] += details if isinstance(details, list) else [details]
        if checks:
            st["checks"] += checks
        st["status"] = "pass" if st["points"] == st["max"] else ("partial" if st["points"] > 0 else "fail")

    def vulcan(self, sid, args, label, cwd=None, **fmt):
        res = self.runner.run(args, cwd or self.work, label, **fmt)
        entry = {k: res[k] for k in ("cmd", "rc", "seconds", "log")}
        entry["output_tail"] = tail(res["output"], 2500)
        self.stages[sid]["commands"].append(entry)
        return res

    # -- main flow -------------------------------------------------------------
    def run(self):
        started = now_iso()
        self.structure_problems, notes = validate_structure(self.submission)
        self.notes += notes
        self.git = git_info(self.submission)
        if self.git["dirty"]:
            self.flags.append("working tree had uncommitted changes when graded (the course page rejects this)")
        self.dataset = dataset_info(self.submission, self.args.assigned_seed)
        ds = self.dataset
        if ds["seed"] is None:
            self.flags.append(f"DATASET: {ds.get('seed_note', 'no seed')} -- run `make data SEED=<your seed>`")
        elif ds["seed_matches_csvs"] is False:
            self.flags.append(f"DATASET MISMATCH: {ds.get('mismatched_files')} do not match seeds/SEED={ds['seed']} "
                              "(hand-edited CSVs or edited SEED file)")
        if ds.get("assigned_seed_matches") is False:
            self.flags.append(f"DATASET: seeds/SEED={ds['seed']} but assigned seed is {ds['assigned_seed']}")

        self.expected = None
        if not any(p.startswith("missing seed") or "header" in p or "no data rows" in p for p in self.structure_problems):
            self.expected = hidden_expected.compute_expected(hidden_expected.load_seeds(os.path.join(self.submission, "seeds")))
            with open(os.path.join(self.out, "expected.json"), "w", encoding="utf-8") as fh:
                json.dump(hidden_expected.to_jsonable(self.expected), fh, indent=2)

        self.models = scan_models(self.submission)
        self.custom_audits, self.inline_audits = scan_custom_audits(self.submission, self.models)
        self.tests, self.test_errors = scan_tests(self.submission)

        if not self.args.dry_run:
            base = self.args.workdir or tempfile.mkdtemp(prefix="vulcan-grade-")
            self.work_base = os.path.abspath(base)
            self.work = os.path.join(self.work_base, "submission")
            if os.path.exists(self.work):
                shutil.rmtree(self.work)
            shutil.copytree(self.submission, self.work, ignore=COPY_IGNORE)
            self.notes += prepare_config(self.work, self.settings)
            if self.selected & VULCAN_STAGES:
                if "plan_run" not in self.selected:  # partial re-run: keep what an earlier plan built
                    self.settings.reset_db = False
                ok, msg = prepare_grading_database(self.settings)
                self.notes.append(msg)
                if not ok:
                    self.flags.append(f"GRADER SETUP: {msg} -- grading results are unreliable; fix and re-run")

        try:
            self.stage_builds()
            self.stage_lint()
            self.stage_format()
            self.stage_tests_exist()
            self.stage_tests_pass()
            self.stage_audits_exist()
            self.stage_models_present()
            self.stage_plan_run()
            self.stage_audits_pass()
            self.stage_hidden()
        finally:
            if self.db is not None:
                self.db.close()
            if self.work_base and not self.args.keep_workdir and not self.args.workdir:
                shutil.rmtree(self.work_base, ignore_errors=True)
        return self.report(started)

    # -- stages ----------------------------------------------------------------
    def stage_builds(self):
        sid = "builds"
        if not self.should_run(sid):
            return
        res = self.vulcan(sid, INFO_ARGS, "info")
        self.finish(sid, 10 if cmd_ok(res) else 0, f"vulcan info exit code {res['rc']}")

    def stage_lint(self):
        sid = "lint"
        if not self.should_run(sid):
            return
        res = self.vulcan(sid, LINT_ARGS, "lint")
        ok = cmd_ok(res, "lint")
        self.finish(sid, 10 if ok else 0, f"vulcan lint exit code {res['rc']}" + ("" if ok else " / linter reported errors"))

    def stage_format(self):
        sid = "format"
        if not self.should_run(sid):
            return
        res = self.vulcan(sid, FORMAT_CHECK_ARGS, "format_check")
        ok = cmd_ok(res)
        self.finish(sid, 5 if ok else 0, f"vulcan format --check exit code {res['rc']}"
                    + ("" if ok else " (run `vulcan format` and commit)"))

    def stage_tests_exist(self):
        sid = "tests_exist"
        if not self.should_run(sid):
            return
        details = [f"found {len(self.tests)} test(s): " + ", ".join(f"{t['name']} -> {t['model']}" for t in self.tests)]
        details += self.test_errors
        target = [t for t in self.tests if t["model"] == TEST_TARGET_MODEL]
        pts = 5 if target else (2 if self.tests else 0)
        if not target and self.tests:
            details.append(f"tests exist but none target {TEST_TARGET_MODEL} (2/5)")
        self.finish(sid, pts, details)

    def stage_tests_pass(self):
        sid = "tests_pass"
        if not self.should_run(sid):
            return
        if not self.tests:
            self.finish(sid, 0, "no unit tests found in tests/ -- nothing to pass")
            return
        res = self.vulcan(sid, TEST_ARGS, "test")
        self.finish(sid, 10 if cmd_ok(res, "test") else 0, f"vulcan test exit code {res['rc']}")

    def _attached_audits(self):
        return [(m.get("name"), a) for m in self.models for a in m.get("audits", [])]

    def stage_audits_exist(self):
        sid = "audits_exist"
        if not self.should_run(sid):
            return
        attached = self._attached_audits()
        names = {a for _m, a in attached}
        nn = bool(names & NOT_NULL_AUDITS)
        un = bool(names & UNIQUE_AUDITS)
        custom_defined = set(self.custom_audits)
        custom_attached = sorted(n for n in names if n in custom_defined)
        inline_attached = sorted(n for n in names if n in self.inline_audits and n not in custom_defined)
        ck = bool(custom_attached)
        checks = [
            {"name": "not_null attached", "ok": nn, "points": 1 if nn else 0, "max": 1},
            {"name": "uniqueness audit attached", "ok": un, "points": 1 if un else 0, "max": 1},
            {"name": "custom audit in audits/ attached to a model", "ok": ck, "points": 3 if ck else 0, "max": 3,
             "message": ", ".join(custom_attached) or
             ("inline audit(s) found but README requires audits/: " + ", ".join(inline_attached) if inline_attached else "none")},
        ]
        pts = sum(c["points"] for c in checks)
        details = [f"{len(attached)} audit attachment(s): " + ", ".join(f"{m}:{a}" for m, a in attached),
                   f"custom audits defined in audits/: {sorted(custom_defined) or 'none'}"]
        unknown = sorted(n for n in names if n not in BUILTIN_AUDITS and n not in custom_defined and n not in self.inline_audits)
        if unknown:
            details.append(f"attached audits that are neither built-in nor defined: {unknown}")
        if len(attached) < 2:
            pts = min(pts, 1)
            details.append("fewer than 2 audits attached")
        self.finish(sid, pts, details, checks)

    def stage_models_present(self):
        sid = "models_present"
        if not self.should_run(sid):
            return
        by_name = {}
        for m in self.models:
            if m.get("name"):
                by_name.setdefault(m["name"], []).append(m)
        checks = []
        use_render = not self.args.dry_run and not self.args.no_render
        for name, kind in REQUIRED_MODELS:
            found = by_name.get(name, [])
            ok = bool(found) and found[0]["kind"] == kind
            msg = "missing" if not found else f"{found[0]['file']} kind {found[0]['kind']}"
            if len(found) > 1:
                msg += f" (defined {len(found)} times!)"
                ok = False
            if ok and use_render and kind != "SEED":
                res = self.vulcan(sid, RENDER_ARGS, f"render_{name.replace('.', '_')}", model=name)
                if not cmd_ok(res):
                    ok = False
                    msg += f"; vulcan render failed (exit {res['rc']})"
            checks.append({"name": f"{name} ({kind})", "ok": ok, "points": 1 if ok else 0, "max": 1, "message": msg})
        for name, grains in REQUIRED_GRAINS.items():
            found = by_name.get(name, [])
            got = found[0]["grains"] if found else []
            ok = bool(found) and got == grains
            checks.append({"name": f"{name} grains {grains}", "ok": ok, "points": 1 if ok else 0, "max": 1,
                           "message": f"declared {got or 'none'}"})
        extra = sorted(set(by_name) - {n for n, _ in REQUIRED_MODELS})
        details = [f"{len(self.models)} model file(s) parsed"] + ([f"additional models: {extra}"] if extra else [])
        self.finish(sid, sum(c["points"] for c in checks), details, checks)

    def stage_plan_run(self):
        sid = "plan_run"
        if not self.should_run(sid):
            return
        res = self.vulcan(sid, PLAN_ARGS, "plan", env=self.env)
        pts = 0
        details = [f"vulcan plan {self.env} exit code {res['rc']}"]
        if cmd_ok(res):
            self.plan_ok = True
            pts = 10
            res2 = self.vulcan(sid, RUN_ARGS, "run", env=self.env)
            pts += 5 if cmd_ok(res2) else 0
            details.append(f"vulcan run {self.env} exit code {res2['rc']}")
        else:
            details.append("plan failed -- run not attempted; hidden correctness cannot be evaluated")
        self.finish(sid, pts, details)

    def stage_audits_pass(self):
        sid = "audits_pass"
        if not self.should_run(sid):
            return
        if not self._attached_audits():
            self.finish(sid, 0, "no audits attached to any model")
            return
        if not self.plan_ok and "plan_run" in self.selected:
            self.finish(sid, 0, "plan failed -- tables to audit do not exist")
            return
        res = self.vulcan(sid, AUDIT_ARGS, "audit")
        self.finish(sid, 5 if cmd_ok(res) else 0, f"vulcan audit exit code {res['rc']}")

    def table_for(self, model):
        schema, table = model.split(".", 1)
        rel = f"{qident(ENV_SCHEMA_TEMPLATE.format(schema=schema, env=self.env))}.{qident(table)}"
        try:
            if self.db.relation_exists(rel):
                return rel
        except Exception:  # noqa: BLE001
            pass
        res = self.vulcan("hidden", TABLE_NAME_ARGS, f"table_name_{model.replace('.', '_')}", env=self.env, model=model)
        if res["rc"] == 0:
            for line in reversed([l.strip() for l in res["output"].splitlines() if l.strip()]):
                if re.fullmatch(r'[A-Za-z0-9_"]+(\.[A-Za-z0-9_"]+){1,2}', line):
                    parts = [p.strip('"') for p in line.split(".")][-2:]
                    cand = ".".join(qident(p) for p in parts)
                    try:
                        if self.db.relation_exists(cand):
                            self.stages["hidden"]["details"].append(f"{model}: resolved via table_name -> {cand}")
                            return cand
                    except Exception:  # noqa: BLE001
                        pass
        return None

    def stage_hidden(self):
        sid = "hidden"
        if not self.should_run(sid):
            return
        if self.expected is None:
            self.finish(sid, 0, "seed CSVs missing or malformed -- cannot compute expected results")
            return
        if "plan_run" in self.selected and not self.plan_ok:
            self.finish(sid, 0, "plan failed -- nothing to check")
            return
        try:
            self.db = PgClient(self.settings.query)
        except Exception as exc:  # noqa: BLE001
            self.finish(sid, 0, f"GRADER ERROR: cannot connect to grading database: {exc}")
            self.flags.append("grader could not query the grading database -- infrastructure problem, re-run")
            return
        results = hidden_checks.run_correctness_checks(self.db.query, self.table_for, self.expected)
        results.append(self.injection())
        self.finish(sid, sum(r["points"] for r in results), [f"db driver: {self.db.driver}"], results)

    def injection(self):
        if self.args.skip_injection:
            return hidden_checks.injection_result(None, "", None, skipped_reason="--skip-injection")
        inj = os.path.join(self.work_base, "inject")
        if os.path.exists(inj):
            shutil.rmtree(inj)
        shutil.copytree(self.work, inj)
        row = hidden_expected.inject_negative_order(os.path.join(inj, "seeds", "orders.csv"))
        res = self.vulcan("hidden", PLAN_ARGS, "plan_injected_negative_amount", cwd=inj, env=f"{self.env}_inject")
        return hidden_checks.injection_result(res["rc"], res["output"], row)

    # -- report ----------------------------------------------------------------
    def _scrub(self, obj):
        reps = [p for p in (self.work_base, self.submission, self.out, os.path.expanduser("~")) if p and len(p) > 1]
        reps.sort(key=len, reverse=True)
        names = {self.work_base: "<workdir>", self.submission: "<submission>", self.out: "<out>"}

        def s(v):
            if isinstance(v, str):
                for p in reps:
                    v = v.replace(p, names.get(p, "~"))
                return v
            if isinstance(v, dict):
                return {k: s(x) for k, x in v.items()}
            if isinstance(v, list):
                return [s(x) for x in v]
            return v
        return s(obj)

    def report(self, started):
        stages = [self.stages[s] for s in STAGE_IDS]
        total = sum(s["points"] for s in stages)
        full_run = not self.args.dry_run and self.selected == set(STAGE_IDS)
        failed_gates = [s["id"] for s in stages if s["critical"] and s["points"] <= 0] if not self.args.dry_run else []
        if self.structure_problems:
            self.notes = [f"STRUCTURE: {p}" for p in self.structure_problems] + self.notes
        if self.args.dry_run:
            verdict = "DRY_RUN"
        elif failed_gates or total < PASS_MARK or not full_run:
            verdict = "FAIL"
        else:
            verdict = "PASS"
        if not full_run and not self.args.dry_run:
            self.flags.append("partial run (--stages): verdict forced to FAIL; not valid for submission")
        rep = {
            "schema": REPORT_SCHEMA,
            "grader_version": GRADER_VERSION,
            "grader_checksum": grader_checksum(),
            "started_at": started,
            "finished_at": now_iso(),
            "git": self.git,
            "environment": self.env,
            "dry_run": bool(self.args.dry_run),
            "partial_run": bool(not full_run and not self.args.dry_run),
            "stages_selected": [s for s in STAGE_IDS if s in self.selected],
            "verdict": verdict,
            "total": total,
            "max": 100,
            "pass_mark": PASS_MARK,
            "passed": verdict == "PASS",
            "failed_gates": failed_gates,
            "critical_gates": {s["id"]: s["points"] > 0 for s in stages if s["critical"]},
            "flags": self.flags,
            "notes": self.notes,
            "structure_problems": self.structure_problems,
            "dataset": self.dataset,
            "expected_summary": self.expected["summary"] if self.expected else None,
            "vulcan_cmd": "vulcan" if VULCAN_CMD.strip() == "vulcan" else "custom (VULCAN_CMD)",
            "stages": stages,
        }
        rep = self._scrub(rep)
        assert sum(s["points"] for s in rep["stages"]) == rep["total"]
        with open(os.path.join(self.out, "report.json"), "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=2, default=str)
            fh.write("\n")
        with open(os.path.join(self.out, "report.md"), "w", encoding="utf-8") as fh:
            fh.write(render_markdown(rep))
        relax_permissions(self.out)
        return rep


def relax_permissions(path):
    """When run as root inside Docker, make the report deletable by the candidate."""
    try:
        for dirpath, dirs, files in os.walk(path):
            os.chmod(dirpath, 0o777)
            for f in files:
                os.chmod(os.path.join(dirpath, f), 0o666)
    except OSError:
        pass


def render_markdown(rep):
    icon = {"pass": "PASS", "partial": "PARTIAL", "fail": "FAIL", "skipped": "SKIPPED", "not_run": "-"}
    L = ["# Vulcan Associate (Beginner) - grading report\n",
         f"**Verdict: {rep['verdict']}**, score **{rep['total']} / {rep['max']}** (pass mark {rep['pass_mark']})\n"]
    if rep["failed_gates"]:
        L.append(f"> Failed critical gate(s): **{', '.join(rep['failed_gates'])}**. The submission fails whatever the total.\n")
    if rep["dry_run"]:
        L.append("> DRY RUN: Vulcan and database stages were not executed, so their points are 0. Not valid for submission.\n")
    git = rep.get("git") or {}
    L.append(f"Commit `{git.get('commit') or 'n/a'}` · dirty `{git.get('dirty')}` · tags `{' '.join(git.get('tags') or []) or 'none'}` · "
             f"seed `{(rep.get('dataset') or {}).get('seed')}` · grader v{rep['grader_version']} `{rep['grader_checksum'][:19]}...`\n")
    L.append("| Stage | Points | Status | Critical |\n|---|---:|---|:---:|")
    for s in rep["stages"]:
        L.append(f"| {s['title']} | {s['points']} / {s['max']} | {icon.get(s['status'], s['status'])} | {'yes' if s['critical'] else ''} |")
    L.append(f"| **Total** | **{rep['total']} / {rep['max']}** | | |\n")
    if rep["flags"]:
        L.append("## Flags\n")
        L += [f"- {f}" for f in rep["flags"]] + [""]
    if rep["notes"]:
        L.append("## Notes\n")
        L += [f"- {n}" for n in rep["notes"]] + [""]
    L.append("## Details\n")
    for s in rep["stages"]:
        L.append(f"### {s['title']} - {s['points']}/{s['max']}\n")
        L += [f"- {d}" for d in s["details"]]
        for c in s["checks"]:
            line = f"- {'[x]' if c.get('ok') else '[ ]'} {c.get('title', c.get('name'))}: {c.get('points', 0)}/{c.get('max', 0)}"
            if c.get("message"):
                line += f" ({c['message']})"
            L.append(line)
            for mm in c.get("mismatches", []) or []:
                L.append(f"    - {mm}")
            if c.get("mismatch_count", 0) > len(c.get("mismatches", []) or []):
                L.append(f"    - ... {c['mismatch_count'] - len(c['mismatches'])} more")
        for cmd in s["commands"]:
            L.append(f"- `{cmd['cmd']}` exit {cmd['rc']} ({cmd['seconds']}s, log `{cmd['log']}`)")
            if cmd["rc"] != 0:
                L.append("\n<details><summary>output (tail)</summary>\n\n```text\n" + cmd["output_tail"][-1500:] + "\n```\n</details>\n")
        L.append("")
    ds = rep.get("dataset") or {}
    L.append("## Dataset\n")
    L.append(f"- seeds/SEED: {ds.get('seed')}, CSVs match seed: {ds.get('seed_matches_csvs')}")
    for f, hsh in (ds.get("sha256") or {}).items():
        L.append(f"- `{f}` sha256 `{(hsh or 'missing')[:16]}...`")
    if rep.get("expected_summary"):
        L.append(f"- expected summary: {rep['expected_summary']}")
    return "\n".join(L) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description="Vulcan Associate (Beginner) lab grader")
    p.add_argument("--submission", default=".", help="path to the lab repo (never modified; default .)")
    p.add_argument("--out", default="grading-report", help="output dir (default grading-report)")
    p.add_argument("--env", default="grading", help="Vulcan environment to plan into (default: grading)")
    p.add_argument("--assigned-seed", type=int, default=None, help="reviewers: also check seeds/SEED equals this seed")
    p.add_argument("--dry-run", action="store_true", help="no Vulcan/DB calls: structure + static stages + expected results")
    p.add_argument("--stages", default=None, help=f"comma-separated subset of: {','.join(STAGE_IDS)} (debugging only)")
    p.add_argument("--skip-injection", action="store_true", help="do not run the bad-data injection plan")
    p.add_argument("--no-render", action="store_true", help="models_present: static parse only, skip `vulcan render`")
    p.add_argument("--keep-workdir", action="store_true", help="keep the temporary work copy")
    p.add_argument("--workdir", default=None, help="use (and keep) this work directory")
    p.add_argument("--print-checksum", action="store_true", help="print the grader checksum and exit")
    args = p.parse_args(argv)
    if args.print_checksum:
        print(grader_checksum())
        return 0
    if not os.path.isdir(args.submission):
        print(f"submission not found: {args.submission}", file=sys.stderr)
        return 2
    try:
        rep = Grader(args).run()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 2
    print(f"verdict={rep['verdict']} total={rep['total']}/100 failed_gates={rep['failed_gates']}")
    for s in rep["stages"]:
        print(f"  {s['id']:<15} {s['points']:>3}/{s['max']:<3} {s['status']}")
    for f in rep["flags"]:
        print(f"  FLAG: {f}")
    print(f"report: {os.path.join(args.out, 'report.md')}  (upload {os.path.join(args.out, 'report.json')})")
    if rep["dry_run"]:
        return 0 if not rep["structure_problems"] else 1
    return 0 if rep["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
