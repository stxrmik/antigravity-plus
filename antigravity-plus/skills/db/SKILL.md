---
name: db
description: >-
  Safe read-only database inspection, EXPLAIN query plans, and migration risk analysis for SQLite, PostgreSQL, and
  Redis. Locates databases and DB env vars without exposing secrets, inspects schemas and row estimates, runs EXPLAIN
  strictly read-only (blocking mutating or DDL SQL), and flags risky Alembic, Django, Prisma, and SQL migrations.
  Use when inspecting tables/indexes, explaining slow queries, or reviewing migrations before /deploy («посмотри схему
  БД», «проверь миграции», «сделай EXPLAIN», /db).
---

# /db — read-only database inspection, EXPLAIN, and migration risk analysis

Helper: `python3 "<skill_dir>/scripts/db.py"` (below: `db.py`).

## Commands

```bash
db.py detect     [--workspace DIR] [--json]
db.py schema     [--workspace DIR] [--target PATH_OR_ENV_VAR] [--table NAME] [--json]
db.py explain    SQL [--workspace DIR] [--target PATH_OR_ENV_VAR] [--analyze] [--json]
db.py migrations [--workspace DIR] [--since REF] [--json]
```

Exit codes:
- `0` — success (or no migration risks found).
- `1` — risky operations found by `migrations`.
- `2` — rejected unsafe SQL, invalid/missing target, missing DB CLI (`psql` / `redis-cli` / `docker`), or query error.

## Safety guarantees

- **Write/mutating SQL is blocked:** `explain` accepts only a single strictly read-only statement (`SELECT`, `WITH ... SELECT`, or `VALUES`). Any query containing mutating/DDL keywords (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`, `TRUNCATE`, `REPLACE`, `MERGE`, `GRANT`, `REVOKE`, `VACUUM`, `COPY`, `CALL`, etc.) or multiple `;`-separated statements is rejected with exit code `2` before touching any database. For PostgreSQL, `explain` additionally rejects **every backslash** (no psql meta-commands, no `E'…'` escapes) and **every `$` outside a single-quoted literal** (no dollar-quoted strings, no `$n` parameters), closing the lexer-disagreement that could otherwise smuggle extra statements past the check.
- **Database-level read-only enforcement:**
  - SQLite opens in read-only URI mode (`file:<path>?mode=ro`) with `PRAGMA query_only = ON` and a wall-clock deadline (a slow `COUNT(*)`, e.g. on a recursive view, is abandoned and the row count reported as unknown rather than hanging).
  - PostgreSQL runs every catalog/EXPLAIN query inside `BEGIN READ ONLY; SET LOCAL statement_timeout = …; SET LOCAL default_transaction_read_only = on; … ; ROLLBACK;`, sent as a single `psql -c` string (so psql never interprets a meta-command from stdin), with `psql -X -w` (no `~/.psqlrc`, never a password prompt) and `PGCONNECT_TIMEOUT`.
  - **`--analyze` actually executes the statement**, so on top of the above it refuses a denylist of functions/forms whose effects a `ROLLBACK` would **not** undo: `pg_terminate_backend`/`pg_cancel_backend`, replication-slot admin (`pg_create_*_replication_slot`, `pg_drop_replication_slot`, …), `dblink*`, `lo_*`, `pg_reload_conf`, `pg_rotate_logfile`, `pg_promote`, `pg_stat_reset*`, `pg_advisory_*lock*`, `pg_notify`, `pg_sleep`, `set_config`, `nextval`/`setval`, and `FOR UPDATE`/`FOR SHARE`. Plain `--analyze` (no such call) runs the query once and rolls it back. SQLite has no `EXPLAIN ANALYZE`; `--analyze` falls back to `EXPLAIN QUERY PLAN`.
  - Redis runs only read-only `DBSIZE` and `INFO keyspace`.
- **`.env*` passwords are never printed:** a connection URL is shown only as `scheme://host:port/dbname` (credentials and query parameters dropped; user/password split at the **last** `@`, so `?`/`#` inside a password cannot leak); other DB variables print verbatim only when the name is a plain setting (host, port, db, user, sslmode, …) and the value looks like one — `jdbc:…?password=`, ODBC `Pwd=`, `sslpassword`, and `--requirepass` are masked, and anything else is `***`. Connection details reach `psql` through `PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`/`PGDATABASE` (+ `PGSSL*`) environment variables, **never argv**, and are scrubbed from any output or error message.

## Workflow

1. **Discover databases (`detect`):** run `db.py detect --workspace "<ws>"` to locate SQLite files (`*.db`, `*.sqlite`, `*.sqlite3`), Docker Compose `postgres`/`redis` services, and `.env*` DB variable names.
2. **Inspect schema (`schema`):** run `db.py schema --workspace "<ws>" [--target PATH_OR_ENV_VAR] [--table NAME]` to view tables, columns, primary keys, indexes, and estimated row counts (`pg_stat_user_tables` / `reltuples` on PostgreSQL, `sqlite_stat1` / `COUNT(*)` on SQLite). **Target selection:** without `--target`, only the workspace's root `.env` and `.env.local` (local wins) plus the process environment are read, and the chosen file/variable is named (masked) in the output; a nested or environment-specific file (e.g. `.env.production`) is **never** picked automatically — pass `--target <path>`, `--target <VAR>`, `--target <DSN>`, `--target <compose-service>`, or `--target <file>:<VAR>` to read a variable from a specific env file.
3. **Analyze query plans (`explain`):** pass a single `SELECT` / `WITH ... SELECT` / `VALUES` query to `db.py explain "<sql>" --workspace "<ws>"` (add `--analyze` on PostgreSQL for real runtime timings/buffers — it executes the query once inside a rolled-back read-only transaction and refuses side-effecting functions; see Safety guarantees).
4. **Check migration safety (`migrations`):** run `db.py migrations --workspace "<ws>" [--since REF]` (also invoked automatically by `deploy.py plan` when data/migration files changed) to catch (Alembic operations match on any receiver, including `batch_op.*`; `*.down.sql` files and goose `-- +goose Down` sections are skipped; an index on a table created in the same file is not flagged):
   - **Data loss / non-rollback-safe:** `DROP TABLE`, `DROP COLUMN`, `DROP SCHEMA [CASCADE]`, `op.drop_table`, `op.drop_column`, `DeleteModel`, `RemoveField`.
   - **Table failure / rewrite:** `ADD COLUMN ... NOT NULL` without `DEFAULT` (or with a **volatile** default such as `gen_random_uuid()`/`now()`), `op.add_column(..., nullable=False)` without `server_default`, Django `AddField` without `null=True` or `default`.
   - **Write locks:** `CREATE INDEX` without `CONCURRENTLY`, `op.create_index` without `postgresql_concurrently=True`, Django `AddIndex` (or `AddIndexConcurrently` without `atomic = False`).
   - **Locks & rolling-deploy breaks:** `ALTER COLUMN ... TYPE`, `RENAME [COLUMN]` / `RENAME TABLE` (with or without the `COLUMN` keyword), `TRUNCATE`.

## Rules

- Never pass `.env` secrets into commands on the CLI or echo them in chat.
- Never bypass read-only `explain` by shelling out directly with write queries.
- When `migrations` reports high-risk operations before `/deploy`, highlight the `file:line` and safer expand/contract alternative to the user.
