"""/db: detect secret masking, SQLite/Postgres/Redis schema & EXPLAIN safety, migration risk checks, and deploy.py plan integration."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

PLUGIN, SP = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
DB_SCRIPT = os.path.join(PLUGIN, "skills", "db", "scripts", "db.py")
DEPLOY_SCRIPT = os.path.join(PLUGIN, "skills", "deploy", "scripts", "deploy.py")
WIN = os.name == "nt"
SYS_PATH = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32") if WIN else ""

ok = True


def check(name: str, cond: object, detail: Any = "") -> None:
    global ok
    ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + (f" — {str(detail)[:1200]}" if not cond and detail != "" else ""))


def write(root: str, rel: str, content: str) -> str:
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    return path


def make_exec(path: str, target_script: str, tool_name: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if WIN:
        cmd_path = path if path.endswith(".cmd") else path + ".cmd"
        with open(cmd_path, "w", encoding="utf-8") as f:
            f.write(f'@echo off\r\n"{sys.executable}" "{target_script}" {tool_name} %*\r\n')
        return cmd_path
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            f"#!{sys.executable}\nimport runpy, sys\n"
            f"sys.argv = [sys.argv[0], {tool_name!r}, *sys.argv[1:]]\n"
            f"runpy.run_path({target_script!r}, run_name='__main__')\n"
        )
    os.chmod(path, 0o755)
    return path


def run_db(args: List[str], env: Optional[Dict[str, str]] = None) -> Tuple[int, str, str]:
    r = subprocess.run(
        [sys.executable, DB_SCRIPT] + list(args),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=30,
    )
    return r.returncode, r.stdout, r.stderr


def run_db_json(args: List[str], env: Optional[Dict[str, str]] = None) -> Tuple[int, Dict[str, Any], str]:
    code, out, err = run_db(list(args) + ["--json"], env=env)
    try:
        parsed = json.loads(out)
        return code, (parsed if isinstance(parsed, dict) else {"_val": parsed}), err
    except ValueError:
        return code, {"_raw": out, "_err": err}, err


# ── 1. detect: SQLite, compose services, .env* masking ──
PW_PG = "secretpass" + "123"
PW_PG_ALT = "anothersecret" + "99"
PW_REDIS = "redispw" + "456"
PW_REDIS2 = "redisSecret" + "88"
PW_COMPOSE = "compose_secret" + "_pw"

ws_det = os.path.join(SP, "ws_detect")
write(
    ws_det,
    ".env",
    f"DATABASE_URL=postgresql://u:{PW_PG}@db.example.com:5432/app\n"
    f"PGPASSWORD={PW_PG_ALT}\n"
    f"REDIS_URL=redis://:{PW_REDIS}@cache.example.com:6379/0\n"
    "UNRELATED_KEY=ignore_me\n",
)
write(ws_det, ".env.example", "DATABASE_URL=postgresql://app_user:placeholder@localhost:5432/example_db\n")
write(
    ws_det,
    "docker-compose.yml",
    "services:\n"
    "  postgres:\n"
    "    image: postgres:16-alpine\n"
    "    ports:\n"
    '      - "5432:5432"\n'
    "    environment:\n"
    "      POSTGRES_DB: example_db\n"
    "      POSTGRES_USER: app_user\n"
    f"      POSTGRES_PASSWORD: {PW_COMPOSE}\n"
    "  cache:\n"
    "    image: redis:7-alpine\n"
    "    ports:\n"
    '      - "6379:6379"\n',
)
sqlite_det_path = os.path.join(ws_det, "data", "example.sqlite3")
os.makedirs(os.path.dirname(sqlite_det_path), exist_ok=True)
with sqlite3.connect(sqlite_det_path) as conn:
    conn.execute("CREATE TABLE ping (id INTEGER PRIMARY KEY)")

code, out, err = run_db(["detect", "--workspace", ws_det])
combined_det = out + err
check(
    "detect masks .env credentials and compose passwords in text output",
    code == 0
    and "postgresql://db.example.com:5432/app" in out
    and "redis://cache.example.com:6379/0" in out
    and "data/example.sqlite3" in out
    and "postgres" in out
    and "cache" in out
    and PW_PG not in combined_det
    and PW_PG_ALT not in combined_det
    and PW_REDIS not in combined_det
    and PW_COMPOSE not in combined_det,
    (code, out, err),
)

code, jdet, err = run_db_json(["detect", "--workspace", ws_det])
jdet_str = json.dumps(jdet)
check(
    "detect --json masks secrets and includes sqlite/compose/env_files",
    code == 0
    and len(jdet.get("sqlite", [])) == 1
    and len(jdet.get("compose_services", [])) == 2
    and len(jdet.get("env_files", [])) == 2
    and PW_PG not in jdet_str
    and PW_PG_ALT not in jdet_str
    and PW_REDIS not in jdet_str
    and PW_COMPOSE not in jdet_str,
    (code, jdet, err),
)


# ── 2. SQLite schema + explain + mutating/multi-statement rejection ──
ws_sq = os.path.join(SP, "ws_sqlite")
os.makedirs(ws_sq, exist_ok=True)
sq_db = os.path.join(ws_sq, "app.db")
with sqlite3.connect(sq_db) as conn:
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL, name TEXT)")
    conn.execute(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), "
        "total INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute("CREATE INDEX idx_orders_user ON orders (user_id)")
    conn.execute("INSERT INTO users (id, email, name) VALUES (1, 'a@example.com', 'Alice'), (2, 'b@example.com', 'Bob')")
    conn.execute("INSERT INTO orders (id, user_id, total) VALUES (10, 1, 500), (11, 1, 250), (12, 2, 100)")

code, sch_out, err = run_db(["schema", "--workspace", ws_sq])
check(
    "SQLite schema lists tables, columns, indexes, and row counts",
    code == 0
    and "users (~2 rows)" in sch_out
    and "orders (~3 rows)" in sch_out
    and "idx_orders_user" in sch_out
    and "email: TEXT [NOT NULL]" in sch_out,
    (code, sch_out, err),
)

code, sch_json, err = run_db_json(["schema", "--workspace", ws_sq, "--target", "app.db", "--table", "orders"])
check(
    "SQLite schema --table orders --json returns single table and index",
    code == 0
    and len(sch_json.get("tables", [])) == 1
    and sch_json["tables"][0]["name"] == "orders"
    and sch_json["tables"][0]["estimated_rows"] == 3
    and any(i["name"] == "idx_orders_user" for i in sch_json["tables"][0]["indexes"]),
    (code, sch_json, err),
)

code, exp_out, err = run_db(["explain", "SELECT * FROM orders WHERE user_id = 1", "--workspace", ws_sq])
check(
    "SQLite explain runs EXPLAIN QUERY PLAN on valid SELECT",
    code == 0 and ("SEARCH orders" in exp_out or "idx_orders_user" in exp_out),
    (code, exp_out, err),
)

code, exp_cte, err = run_db(
    ["explain", "WITH active AS (SELECT id FROM users WHERE email != 'DELETE') SELECT * FROM active;", "--workspace", ws_sq]
)
check(
    "SQLite explain allows WITH ... SELECT even when string literal contains keyword text",
    code == 0 and "SCAN" in exp_cte,
    (code, exp_cte, err),
)

code, exp_vals, err = run_db(["explain", "VALUES (1, 'ok')", "--workspace", ws_sq])
check("SQLite explain allows VALUES query", code == 0, (code, exp_vals, err))

unsafe_queries = [
    "DELETE FROM users",
    "SELECT 1; DROP TABLE users",
    "INSERT INTO users (email, name) VALUES ('c@example.com', 'Charlie')",
    "UPDATE users SET name = 'Evil'",
    "DROP TABLE users",
    "ALTER TABLE users ADD COLUMN role TEXT",
    "CREATE TABLE pwn (x INT)",
    "TRUNCATE TABLE users",
    "REPLACE INTO users (id, email) VALUES (1, 'x@example.com')",
    "MERGE INTO users USING orders ON users.id = orders.user_id WHEN MATCHED THEN DELETE",
    "GRANT ALL ON users TO public",
    "REVOKE ALL ON users FROM public",
    "VACUUM",
    "COPY users TO STDOUT",
    "CALL do_something()",
    "WITH d AS (DELETE FROM users RETURNING *) SELECT * FROM d",
    "SELECT * FROM users FOR UPDATE",
    "SELECT 1; SELECT 2",
    "SELECT 1\n\\! id",
]
rejected_all = True
bad_sample: Any = None
for q in unsafe_queries:
    c, o, e = run_db(["explain", q, "--workspace", ws_sq])
    if c != 2:
        rejected_all = False
        bad_sample = (q, c, o, e)
        break
with sqlite3.connect(sq_db) as conn:
    user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
check(
    f"explain rejects all {len(unsafe_queries)} mutating/DDL/multi-statement queries with exit 2 and leaves DB intact",
    rejected_all and user_count == 2,
    (bad_sample, user_count),
)


# ── 3. Mocked psql & redis-cli in PATH: BEGIN READ ONLY + ROLLBACK + no secret leak ──
FAKE_DB_CLI = os.path.join(SP, "fake_db_cli.py")
FAKE_DB_CLI_SRC = r'''import json, os, sys

tool = sys.argv[1]
argv = sys.argv[2:]
cap_file = os.environ.get("AGP_FAKE_DB_CAPTURE", "")
stdin_data = sys.stdin.read() if not sys.stdin.isatty() else ""

# psql now receives its SQL via -c (never argv connection string, never stdin meta-commands).
cmd_sql = ""
for i, a in enumerate(argv):
    if a == "-c" and i + 1 < len(argv):
        cmd_sql = argv[i + 1]
payload = stdin_data + "\n" + cmd_sql

rec = {
    "tool": tool,
    "argv": argv,
    "stdin": stdin_data,
    "cmd_sql": cmd_sql,
    "payload": payload,
    "PGHOST": os.environ.get("PGHOST", ""),
    "PGPORT": os.environ.get("PGPORT", ""),
    "PGUSER": os.environ.get("PGUSER", ""),
    "PGDATABASE": os.environ.get("PGDATABASE", ""),
    "PGPASSWORD": os.environ.get("PGPASSWORD", ""),
    "PGSSLMODE": os.environ.get("PGSSLMODE", ""),
    "REDISCLI_AUTH": os.environ.get("REDISCLI_AUTH", ""),
}
if cap_file:
    with open(cap_file, "w", encoding="utf-8") as f:
        json.dump(rec, f)

if tool == "psql":
    if "EXPLAIN" in payload:
        sys.stdout.write("Index Scan using idx_users_email on users  (cost=0.15..8.17 rows=1 width=36)\n")
        sys.stdout.write("  Buffers: shared hit=2\n")
        sys.exit(0)
    if "'T'" in payload:  # the schema catalog query selects the literal 'T', 'C', 'I' tags
        rows = [
            "T\tpublic\tusers\t42",
            "C\tpublic\tusers\tid\tinteger\tt\t\tt",
            "C\tpublic\tusers\temail\ttext\tt\t\tf",
            "I\tpublic\tusers\tidx_users_email\tCREATE UNIQUE INDEX idx_users_email ON public.users USING btree (email)",
        ]
        sys.stdout.write("\n".join(rows) + "\n")
        sys.exit(0)
    sys.exit(1)

if tool == "redis-cli":
    if "DBSIZE" in argv:
        sys.stdout.write("12\n")
        sys.exit(0)
    if "INFO" in argv and "keyspace" in argv:
        sys.stdout.write("# Keyspace\ndb0:keys=12,expires=3,avg_ttl=5000\n")
        sys.exit(0)
    sys.exit(1)

sys.exit(2)
'''
with open(FAKE_DB_CLI, "w", encoding="utf-8") as _f:
    _f.write(FAKE_DB_CLI_SRC)

BIN_DB = os.path.join(SP, "bin_db")
make_exec(os.path.join(BIN_DB, "psql"), FAKE_DB_CLI, "psql")
make_exec(os.path.join(BIN_DB, "redis-cli"), FAKE_DB_CLI, "redis-cli")
CAP_FILE = os.path.join(SP, "fake_db_capture.json")

env_pg = dict(
    os.environ,
    PATH=BIN_DB + (os.pathsep + SYS_PATH if WIN else "") + os.pathsep + os.environ.get("PATH", ""),
    AGP_FAKE_DB_CAPTURE=CAP_FILE,
)

ws_pg = os.path.join(SP, "ws_pg")
write(ws_pg, ".env", f"DATABASE_URL=postgresql://app_user:{PW_PG}@db.example.com:5432/example_db\n")

code, pg_exp, err = run_db(["explain", "SELECT id FROM users WHERE email = 'a@example.com'", "--workspace", ws_pg], env=env_pg)
with open(CAP_FILE, encoding="utf-8") as f:
    cap_exp = json.load(f)
check(
    "Postgres explain wraps EXPLAIN (FORMAT TEXT, BUFFERS) in BEGIN READ ONLY / ROLLBACK via -c, PG* env, hides secret",
    code == 0
    and "BEGIN READ ONLY;" in cap_exp["cmd_sql"]
    and "statement_timeout" in cap_exp["cmd_sql"]
    and "EXPLAIN (FORMAT TEXT, BUFFERS) SELECT id FROM users" in cap_exp["cmd_sql"]
    and "ROLLBACK;" in cap_exp["cmd_sql"]
    and not cap_exp["stdin"].strip()  # SQL goes through -c, never stdin (so psql never reads meta-commands)
    and "-w" in cap_exp["argv"]  # never prompt for a password
    and cap_exp["PGPASSWORD"] == PW_PG and cap_exp["PGHOST"] == "db.example.com" and cap_exp["PGDATABASE"] == "example_db"
    and all(PW_PG not in a for a in cap_exp["argv"])
    and PW_PG not in (pg_exp + err)
    and "Index Scan" in pg_exp,
    (code, cap_exp, pg_exp, err),
)

code, pg_ana, err = run_db(["explain", "SELECT id FROM users", "--workspace", ws_pg, "--analyze"], env=env_pg)
with open(CAP_FILE, encoding="utf-8") as f:
    cap_ana = json.load(f)
check(
    "Postgres explain --analyze includes ANALYZE inside BEGIN READ ONLY / ROLLBACK",
    code == 0
    and "BEGIN READ ONLY;" in cap_ana["cmd_sql"]
    and "ANALYZE" in cap_ana["cmd_sql"]
    and "BUFFERS" in cap_ana["cmd_sql"]
    and "ROLLBACK;" in cap_ana["cmd_sql"],
    (code, cap_ana, pg_ana, err),
)

code, pg_sch, err = run_db(["schema", "--workspace", ws_pg, "--target", "DATABASE_URL"], env=env_pg)
with open(CAP_FILE, encoding="utf-8") as f:
    cap_sch = json.load(f)
check(
    "Postgres schema (--target DATABASE_URL) runs read-only + statement_timeout and parses tables/indexes/rows",
    code == 0
    and "BEGIN READ ONLY;" in cap_sch["cmd_sql"]
    and "default_transaction_read_only = on" in cap_sch["cmd_sql"]
    and "statement_timeout" in cap_sch["cmd_sql"]
    and "users (~42 rows)" in pg_sch
    and "idx_users_email" in pg_sch
    and PW_PG not in (pg_sch + err),
    (code, cap_sch, pg_sch, err),
)

ws_redis = os.path.join(SP, "ws_redis")
write(ws_redis, ".env", f"REDIS_URL=redis://:{PW_REDIS2}@cache.example.com:6379/0\n")
code, red_out, err = run_db(["schema", "--workspace", ws_redis], env=env_pg)
with open(CAP_FILE, encoding="utf-8") as f:
    cap_red = json.load(f)
check(
    "Redis schema runs DBSIZE + INFO keyspace via REDISCLI_AUTH without printing secret",
    code == 0
    and "DBSIZE: 12 keys" in red_out
    and "db0: keys=12" in red_out
    and cap_red["REDISCLI_AUTH"] == PW_REDIS2
    and all(PW_REDIS2 not in a for a in cap_red["argv"])
    and PW_REDIS2 not in (red_out + err),
    (code, cap_red, red_out, err),
)


# ── 4. migrations: SQL, Alembic, Django, Prisma risky fixtures + clean fixtures + --since ──
ws_mig = os.path.join(SP, "ws_migrations")
write(
    ws_mig,
    "migrations/001_risky.sql",
    "-- risky SQL migration\n"
    "CREATE TABLE safe_tbl (id INT NOT NULL);\n"
    "DROP TABLE legacy_sessions;\n"
    "ALTER TABLE users DROP COLUMN old_flag;\n"
    "ALTER TABLE users ADD COLUMN role TEXT NOT NULL;\n"
    "CREATE INDEX idx_users_role ON users (role);\n"
    "ALTER TABLE orders ALTER COLUMN total TYPE BIGINT;\n"
    "ALTER TABLE users RENAME COLUMN name TO full_name;\n"
    "TRUNCATE TABLE audit_log;\n",
)
write(
    ws_mig,
    "alembic/versions/002_risky.py",
    '"""risky alembic"""\n'
    "from alembic import op\n"
    "import sqlalchemy as sa\n\n"
    "def upgrade():\n"
    '    op.drop_table("old_t")\n'
    '    op.drop_column("users", "legacy")\n'
    '    op.add_column("users", sa.Column("bio", sa.String(100), nullable=False))\n'
    '    op.create_index("ix_users_bio", "users", ["bio"])\n'
    '    op.alter_column("users", "age", type_=sa.BigInteger())\n\n'
    "def downgrade():\n"
    '    op.drop_column("users", "added_elsewhere")\n',
)
write(
    ws_mig,
    "app/migrations/0003_risky.py",
    "from django.db import migrations, models\n\n"
    "class Migration(migrations.Migration):\n"
    "    operations = [\n"
    '        migrations.DeleteModel(name="Legacy"),\n'
    '        migrations.RemoveField(model_name="user", name="legacy"),\n'
    '        migrations.AddField(model_name="user", name="role", field=models.CharField(max_length=20)),\n'
    '        migrations.AddIndex(model_name="user", index=models.Index(fields=["role"], name="user_role_idx")),\n'
    '        migrations.AlterField(model_name="user", name="age", field=models.BigIntegerField()),\n'
    "    ]\n",
)
write(
    ws_mig,
    "prisma/migrations/20260101_init/migration.sql",
    'CREATE UNIQUE INDEX "User_email_key" ON "User"("email");\n',
)

code, mig_data, err = run_db_json(["migrations", "--workspace", ws_mig])
risks = mig_data.get("risks", [])
by_file: Dict[str, List[Dict[str, Any]]] = {}
for r in risks:
    by_file.setdefault(r["file"], []).append(r)

sql_kinds = {r["kind"] for r in by_file.get("migrations/001_risky.sql", [])}
alembic_kinds = {r["kind"] for r in by_file.get("alembic/versions/002_risky.py", [])}
alembic_lines = {r["line"] for r in by_file.get("alembic/versions/002_risky.py", [])}
django_kinds = {r["kind"] for r in by_file.get("app/migrations/0003_risky.py", [])}
prisma_kinds = {r["kind"] for r in by_file.get("prisma/migrations/20260101_init/migration.sql", [])}

check(
    "migrations exits 1 and catches SQL/Alembic/Django/Prisma risks while ignoring CREATE TABLE & Alembic downgrade()",
    code == 1
    and {"drop_table", "drop_column", "add_not_null_no_default", "create_index_no_concurrently", "alter_column_type", "rename", "truncate"} <= sql_kinds
    and len(by_file.get("migrations/001_risky.sql", [])) == 7  # CREATE TABLE safe_tbl NOT flagged
    and {"drop_table", "drop_column", "add_not_null_no_default", "create_index_no_concurrently", "alter_column_type"} == alembic_kinds
    and 12 not in alembic_lines  # downgrade() line 12 skipped
    and {"drop_table", "drop_column", "add_not_null_no_default", "create_index_no_concurrently", "alter_column_type"} == django_kinds
    and {"create_index_no_concurrently"} == prisma_kinds
    and all(r.get("alternative") for r in risks),
    (code, by_file, err),
)

# Clean concurrent index & safe defaults pass with exit 0
ws_clean = os.path.join(SP, "ws_clean_mig")
write(
    ws_clean,
    "migrations/001_clean.sql",
    "CREATE TABLE items (id INT NOT NULL);\n"
    "ALTER TABLE items ADD COLUMN status TEXT NOT NULL DEFAULT 'open';\n"
    "ALTER TABLE items ADD COLUMN notes TEXT;\n"
    "CREATE INDEX CONCURRENTLY idx_items_status ON items (status);\n"
    "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS idx_items_uniq ON items (id);\n",
)
write(
    ws_clean,
    "alembic/versions/002_clean.py",
    "from alembic import op\n"
    "import sqlalchemy as sa\n\n"
    "def upgrade():\n"
    '    op.add_column("items", sa.Column("tag", sa.Text(), nullable=True))\n'
    '    op.add_column("items", sa.Column("flag", sa.Boolean(), nullable=False, server_default=sa.text("false")))\n'
    '    op.create_index("ix_items_tag", "items", ["tag"], postgresql_concurrently=True)\n\n'
    "def downgrade():\n"
    '    op.drop_index("ix_items_tag", table_name="items")\n'
    '    op.drop_column("items", "flag")\n',
)
write(
    ws_clean,
    "app/migrations/0003_clean.py",
    "from django.db import migrations, models\n"
    "from django.contrib.postgres.operations import AddIndexConcurrently\n\n"
    "class Migration(migrations.Migration):\n"
    "    atomic = False\n"
    "    operations = [\n"
    '        migrations.AddField("item", "note", models.TextField(null=True)),\n'
    '        migrations.AddField("item", "state", models.CharField(max_length=16, default="ok")),\n'
    '        AddIndexConcurrently("item", models.Index(fields=["state"], name="item_state_idx")),\n'
    "    ]\n",
)
code, clean_data, err = run_db_json(["migrations", "--workspace", ws_clean])
check(
    "clean SQL, Alembic (with downgrade), and Django AddIndexConcurrently(atomic=False) pass with exit 0",
    code == 0 and clean_data.get("risks") == [] and clean_data.get("files_scanned") == 3,
    (code, clean_data, err),
)

# --since REF filtering in a git repo
ws_since = os.path.join(SP, "ws_since")
write(ws_since, "migrations/001_old.sql", "DROP TABLE ancient;\n")
subprocess.run(["git", "init", "-q"], cwd=ws_since, check=True)
subprocess.run(["git", "add", "."], cwd=ws_since, check=True)
subprocess.run(["git", "commit", "-qm", "old"], cwd=ws_since, check=True)
write(ws_since, "migrations/002_new.sql", "CREATE INDEX CONCURRENTLY idx_ok ON t (c);\n")
code, since_data, err = run_db_json(["migrations", "--workspace", ws_since, "--since", "HEAD"])
check(
    "migrations --since HEAD scans only newly changed/added migrations",
    code == 0 and since_data.get("files") == ["migrations/002_new.sql"] and since_data.get("risks") == [],
    (code, since_data, err),
)


# ── 5. deploy.py plan shows Migration risks in both git-status mode and snapshot mode ──
ws_dep = os.path.join(SP, "ws_deploy")
write(ws_dep, "app.py", "print('hello')\n")
subprocess.run(["git", "init", "-q"], cwd=ws_dep, check=True)
subprocess.run(["git", "add", "."], cwd=ws_dep, check=True)
subprocess.run(["git", "commit", "-qm", "init"], cwd=ws_dep, check=True)

write(
    ws_dep,
    "migrations/002_risky.sql",
    "DROP TABLE old_sessions;\nCREATE INDEX idx_users_email ON users (email);\n",
)
r_git_plan = subprocess.run(
    [sys.executable, DEPLOY_SCRIPT, "plan", "--workspace", ws_dep, "--conversation", "agp-dbtest-01"],
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
)
check(
    "deploy.py plan (no record yet, git mode) outputs Migration risks block for risky migration",
    r_git_plan.returncode == 0
    and "Migration risks:" in r_git_plan.stdout
    and "migrations/002_risky.sql:1" in r_git_plan.stdout
    and "DROP TABLE" in r_git_plan.stdout,
    (r_git_plan.returncode, r_git_plan.stdout, r_git_plan.stderr),
)

subprocess.run([sys.executable, DEPLOY_SCRIPT, "record", "--workspace", ws_dep, "--conversation", "agp-dbtest-01"],
               cwd=ws_dep, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)

write(
    ws_dep,
    "migrations/003_risky.sql",
    "ALTER TABLE users ADD COLUMN role TEXT NOT NULL;\n",
)
r_plan = subprocess.run(
    [sys.executable, DEPLOY_SCRIPT, "plan", "--workspace", ws_dep, "--conversation", "agp-dbtest-01"],
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
)
check(
    "deploy.py plan (snapshot mode) outputs Migration risks block when risky migration file changed",
    r_plan.returncode == 0
    and "Migration risks:" in r_plan.stdout
    and "migrations/003_risky.sql:1" in r_plan.stdout
    and "ADD COLUMN ... NOT NULL without DEFAULT" in r_plan.stdout
    and "Safer:" in r_plan.stdout,
    (r_plan.returncode, r_plan.stdout, r_plan.stderr),
)

# Clean migration should not show Migration risks block
os.remove(os.path.join(ws_dep, "migrations", "003_risky.sql"))
write(ws_dep, "migrations/004_safe.sql", "CREATE INDEX CONCURRENTLY idx_users_email ON users (email);\n")
r_clean_plan = subprocess.run(
    [sys.executable, DEPLOY_SCRIPT, "plan", "--workspace", ws_dep, "--conversation", "agp-dbtest-01"],
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
)
check(
    "deploy.py plan omits Migration risks block when changed migration is safe",
    r_clean_plan.returncode == 0 and "Migration risks:" not in r_clean_plan.stdout,
    (r_clean_plan.returncode, r_clean_plan.stdout, r_clean_plan.stderr),
)

# ── 5. Security & edge-case regressions (Postgres backticks, ?password=, quoted .env, quoted SQL idents) ──
ws_sec = os.path.join(SP, "ws_sec")
PW_QUERY = "qSec" + "Ret!99"
write(ws_sec, ".env", f'DATABASE_URL="postgresql://u:{PW_PG}@db.example.com:5432/app" # dev comment\n')
code_det, det_out, _ = run_db(["detect", "--workspace", ws_sec])
check(
    "detect masks password in quoted .env URL with trailing # comment",
    code_det == 0 and PW_PG not in det_out and "postgresql://db.example.com:5432/app" in det_out,
    det_out,
)

ws_qpg = os.path.join(SP, "ws_qpg")
write(ws_qpg, ".env", f"DATABASE_URL=postgresql://app_user@db.example.com:5432/app?password={PW_QUERY}&sslmode=require\n")
code_q, out_q, err_q = run_db(["explain", "SELECT 1", "--workspace", ws_qpg], env=env_pg)
with open(CAP_FILE, encoding="utf-8") as f:
    cap_q = json.load(f)
check(
    "Postgres ?password= query param stripped from psql argv and passed via PGPASSWORD",
    code_q == 0
    and cap_q["PGPASSWORD"] == PW_QUERY
    and all(PW_QUERY not in a for a in cap_q["argv"])
    and all("password=" not in a.lower() for a in cap_q["argv"])
    and PW_QUERY not in (out_q + err_q),
    (code_q, cap_q, out_q, err_q),
)

code_bt, out_bt, err_bt = run_db(["explain", "SELECT `\\n\\! id\\n`", "--workspace", ws_pg], env=env_pg)
sys.path.insert(0, os.path.join(PLUGIN, "skills", "db", "scripts"))
import db as db_mod  # type: ignore[import-not-found]  # noqa: E402
nul_ok, _, _ = db_mod.validate_readonly_sql("SELECT 1\x00")
check(
    "Postgres explain rejects backtick psql breakout and NUL bytes with exit 2",
    code_bt == 2 and not nul_ok,
    (code_bt, out_bt, err_bt, nul_ok),
)

write(ws_sec, "migrations/010_quoted.sql", 'ALTER TABLE "User" ADD COLUMN "email" TEXT NOT NULL;\nALTER TABLE "User" ALTER COLUMN "id" TYPE BIGINT;\n')
code_qm, qm_data, _ = run_db_json(["migrations", "--workspace", ws_sec])
qm_kinds = {r["kind"] for r in qm_data.get("risks", [])}
check(
    "migrations detects risky ALTER TABLE statements using quoted SQL identifiers",
    code_qm == 1 and {"add_not_null_no_default", "alter_column_type"}.issubset(qm_kinds),
    qm_data,
)


# ── 6. Validator hardening regression: lexer-disagreement bypass, psql meta-commands, --analyze side effects ──
# These are the exact payloads that previously slipped past the validator (fake psql here so no real DB is touched).
DOLLAR_BYPASS = "SELECT 1 AS €$a$, 2; ROLLBACK; DROP TABLE users; SELECT $a$"
SHELL_BYPASS = "SELECT 1 AS €$a$;\n\\! touch /tmp/agp_pwned\nSELECT $a$"
pg_strict_rejects = all(
    not db_mod.validate_readonly_sql(q, dialect="postgres")[0]
    for q in (DOLLAR_BYPASS, SHELL_BYPASS, "SELECT $$x$$", "SELECT $1 FROM t", "SELECT a$b FROM t")
)
pg_strict_allows = db_mod.validate_readonly_sql("SELECT * FROM t WHERE note = '$not a dollar quote$'", dialect="postgres")[0]
# And exercised through the CLI against the fake psql: a bypass payload must exit 2 and never reach the fake.
pwned_marker = os.path.join(SP, "agp_pwned_marker")
SHELL_BYPASS = SHELL_BYPASS.replace("/tmp/agp_pwned", pwned_marker)
if os.path.exists(pwned_marker):
    os.remove(pwned_marker)
code_byp, out_byp, err_byp = run_db(["explain", DOLLAR_BYPASS, "--workspace", ws_pg], env=env_pg)
code_sh, out_sh, err_sh = run_db(["explain", SHELL_BYPASS, "--workspace", ws_pg], env=env_pg)
check(
    "Postgres explain rejects the €$a$ dollar-quote bypass and the \\! shell breakout (strict dialect, exit 2)",
    pg_strict_rejects and pg_strict_allows and code_byp == 2 and code_sh == 2
    and "Dollar-quoted" in (err_byp + out_byp) and not os.path.exists(pwned_marker),
    (pg_strict_rejects, pg_strict_allows, code_byp, err_byp, code_sh, err_sh),
)

# --analyze denylist: side-effecting functions and row locks are refused before psql runs.
deny_hits = [
    db_mod._analyze_denylist_error("SELECT pg_terminate_backend(123)"),
    db_mod._analyze_denylist_error("SELECT pg_drop_replication_slot('s')"),
    db_mod._analyze_denylist_error("SELECT dblink_exec('h', 'DELETE FROM t')"),
    db_mod._analyze_denylist_error("SELECT nextval('seq')"),
    db_mod._analyze_denylist_error("SELECT setval('seq', 1)"),
    db_mod._analyze_denylist_error("SELECT pg_sleep(10)"),
    db_mod._analyze_denylist_error("SELECT * FROM t FOR UPDATE"),
]
deny_clean = db_mod._analyze_denylist_error("SELECT count(*) FROM users WHERE note = 'pg_terminate_backend(1)'")
code_an, out_an, err_an = run_db(["explain", "--analyze", "SELECT pg_terminate_backend(1)", "--workspace", ws_pg], env=env_pg)
check(
    "--analyze refuses side-effecting functions / FOR UPDATE and ignores denylisted names inside string literals",
    all(deny_hits) and not deny_clean and code_an == 2 and "cannot run under --analyze" in (err_an + out_an),
    (deny_hits, deny_clean, code_an, err_an),
)

# Secret masking: a URL is reduced to scheme://host:port/db; libpq-tolerated ?/# in passwords no longer leak.
PW_HASH = "Pa#ss" + "1x"
PW_QMARK = "Qw?er" + "2y"
masked_url = db_mod.mask_url(f"postgresql://u:{PW_HASH}@db.example.com:5432/app")
masked_qmark = db_mod.mask_url(f"postgres://app:{PW_QMARK}@h.example.com:5432/d")
masked_jdbc = db_mod._mask_env_value("DATABASE_JDBC", "jdbc:postgresql://db:5432/app?user=app&password=JdbcLeak9")
masked_odbc = db_mod._mask_env_value("DATABASE_DSN", "Driver={PostgreSQL};Server=db;Uid=app;Pwd=OdbcLeak9;")
masked_redis_args = db_mod._mask_env_value("REDIS_ARGS", "--requirepass RedisLeak9")
masked_host = db_mod._mask_env_value("DB_HOST", "db.example.com")
check(
    "mask_url/_mask_env_value show only scheme://host:port/db and never leak ?/# passwords, jdbc/odbc/redis secrets",
    masked_url == "postgresql://db.example.com:5432/app"
    and masked_qmark == "postgresql://h.example.com:5432/d"
    and PW_HASH not in masked_url and PW_QMARK not in masked_qmark
    and "JdbcLeak9" not in masked_jdbc and "OdbcLeak9" not in masked_odbc and "RedisLeak9" not in masked_redis_args
    and masked_host == "db.example.com",
    (masked_url, masked_qmark, masked_jdbc, masked_odbc, masked_redis_args, masked_host),
)

# Auto-detect reads only the root .env/.env.local (local wins); a nested or .env.production file is ignored.
for _v in ("DATABASE_URL", "POSTGRES_URL", "POSTGRESQL_URL", "PGURL", "DB_URL", "REDIS_URL", "REDIS_URI",
           "PGHOST", "PGDATABASE", "POSTGRES_HOST", "POSTGRES_DB"):
    os.environ.pop(_v, None)  # ensure the workspace files (not the CI environment) decide the target
ws_autoroot = os.path.join(SP, "ws_autoroot")
write(ws_autoroot, ".env", "DATABASE_URL=postgresql://dev@localhost:5432/app_dev\n")
write(ws_autoroot, ".env.local", "DATABASE_URL=postgresql://dev@localhost:5432/app_local\n")
write(ws_autoroot, ".env.production", "DATABASE_URL=postgresql://deploy@prod-db.example.com:5432/app\n")
write(ws_autoroot, "ops/prod/.env", "DATABASE_URL=postgresql://deploy@prod-db.example.com:5432/app\n")
tgt_root, _terr = db_mod.resolve_target(ws_autoroot)
ws_prodonly = os.path.join(SP, "ws_prodonly")
write(ws_prodonly, ".env.production", "DATABASE_URL=postgresql://deploy@prod-db.example.com:5432/app\n")
tgt_prodonly, perr = db_mod.resolve_target(ws_prodonly)
check(
    "auto-detect uses root .env.local over .env, ignores .env.production / nested env files, names the source",
    tgt_root is not None and "app_local" in tgt_root["label"] and ".env.local" in tgt_root["label"]
    and "prod-db.example.com" not in tgt_root["label"]
    and tgt_prodonly is None and "--target" in perr,
    (tgt_root and tgt_root["label"], tgt_prodonly, perr),
)

# Migration analyzer: Alembic batch_op.*, RENAME without COLUMN, volatile DEFAULT, DROP SCHEMA, quoted schema;
# and no false positives on *.down.sql, goose Down sections, or indexes on a table created in the same file.
ws_mig2 = os.path.join(SP, "ws_mig2")
write(ws_mig2, "migrations/versions/a_batch.py",
      "from alembic import op\nimport sqlalchemy as sa\n\n"
      "def upgrade():\n"
      "    with op.batch_alter_table('users') as batch_op:\n"
      "        batch_op.drop_column('legacy')\n"
      "        batch_op.add_column(sa.Column('role', sa.String(20), nullable=False))\n"
      "        batch_op.alter_column('name', new_column_name='full_name')\n\n"
      "def downgrade():\n    pass\n")
write(ws_mig2, "migrations/b_sql.sql",
      "ALTER TABLE users RENAME email TO mail;\n"
      "ALTER TABLE users ADD COLUMN token uuid NOT NULL DEFAULT gen_random_uuid();\n"
      "DROP SCHEMA billing CASCADE;\n"
      'ALTER TABLE "my schema".users DROP COLUMN legacy;\n')
write(ws_mig2, "migrations/000007_add.up.sql",
      "CREATE TABLE invoices (id bigint PRIMARY KEY, user_id bigint NOT NULL);\n"
      "CREATE INDEX idx_invoices_user ON invoices (user_id);\n")
write(ws_mig2, "migrations/000007_add.down.sql", "DROP TABLE invoices;\n")
write(ws_mig2, "migrations/000008_goose.sql",
      "-- +goose Up\nCREATE TABLE tags (id int);\n-- +goose Down\nDROP TABLE tags;\n")
code_m2, m2, _ = run_db_json(["migrations", "--workspace", ws_mig2])
by_file2 = {}
for r in m2.get("risks", []):
    by_file2.setdefault(r["file"], set()).add(r["kind"])
check(
    "migrations catches batch_op.*, RENAME w/o COLUMN, volatile NOT NULL default, DROP SCHEMA, quoted schema; "
    "skips *.down.sql / goose Down / same-file index",
    code_m2 == 1
    and {"drop_column", "add_not_null_no_default", "rename"} <= by_file2.get("migrations/versions/a_batch.py", set())
    and {"rename", "add_not_null_volatile_default", "drop_schema", "drop_column"} <= by_file2.get("migrations/b_sql.sql", set())
    and "migrations/000007_add.up.sql" not in by_file2  # index on invoices (created in-file) not flagged
    and "migrations/000007_add.down.sql" not in by_file2  # rollback script skipped
    and "migrations/000008_goose.sql" not in by_file2,   # goose Down section blanked, Up only creates a table
    (code_m2, by_file2),
)

# --since must not let a value be read as a git option (e.g. --since=--output=FILE).
ws_optinj = os.path.join(SP, "ws_optinj")
write(ws_optinj, "migrations/001.sql", "SELECT 1;\n")
subprocess.run(["git", "init", "-q"], cwd=ws_optinj, check=True)
subprocess.run(["git", "add", "."], cwd=ws_optinj, check=True)
subprocess.run(["git", "commit", "-qm", "init"], cwd=ws_optinj, check=True)
victim = os.path.join(SP, "optinj_victim.txt")
with open(victim, "w", encoding="utf-8") as f:
    f.write("precious\n")
code_opt, out_opt, err_opt = run_db(["migrations", "--workspace", ws_optinj, f"--since=--output={victim}"])
with open(victim, encoding="utf-8") as f:
    victim_after = f.read()
check(
    "migrations --since refuses a ref starting with '-' and never lets git overwrite a file via --output=",
    code_opt == 2 and "must not start with '-'" in (err_opt + out_opt) and victim_after == "precious\n",
    (code_opt, err_opt, victim_after),
)

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
