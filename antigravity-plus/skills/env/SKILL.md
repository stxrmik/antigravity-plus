---
name: env
description: >-
  Checks the local development environment safely: read-only `.env` vs `.env.example` and code reference parity without
  ever printing secret values, non-hanging 2s Docker daemon socket probing before invoking the CLI, localhost dev port
  inspection, and runtime checks (broken `.venv` symlink, missing `node_modules`). Use when the user asks to check
  environment config, `.env` keys, Docker health, or listening ports («проверь .env», «почему не стартует docker»,
  «занят ли порт», /env), or before running/debugging local dev servers.
---

# /env — safe `.env` parity, Docker health, ports & runtime checks

Helper: `python3 "<skill_dir>/scripts/env_check.py"` (below: `env_check.py`).
- Completely read-only and local. Never modifies `.env` files, installs dependencies, or restarts Docker.
- **Safe with secrets:** Antigravity Plus's guard blocks `cat .env` and `read_file` on `.env` files; `env_check.py` parses `.env` / `.env.local` in Python and outputs **only variable names and statuses** (`missing`, `empty`, `placeholder`), never secret values.

## Commands

```bash
env_check.py check  --workspace "<ws>" [--ports 3000,8000] [--json]   # all checks (default subcommand)
env_check.py dotenv --workspace "<ws>" [--json]                     # .env vs .env.example & source code scan
env_check.py docker [--json]                                        # 2s socket /_ping probe, then docker ps -a
env_check.py ports  [--ports 3000,5173,8000,8080,5432,6379] [--json]
```

Exit codes:
- `0` — clean (or Docker not installed/running without errors).
- `1` — findings: missing/extra/placeholder `.env` keys, hung or errored Docker daemon, broken `.venv`, or missing `node_modules`.
- `2` — invalid arguments or nonexistent `--workspace`.

## What each check does

1. **`dotenv` (`.env` parity + code scan):**
   - Compares `.env` / `.env.local` against `.env.example` / `.env.sample` / `.env.template`.
   - Scans `.py`, `.js`, `.ts`, `.tsx`, `.jsx`, `.mjs`, `.cjs`, `.go`, `.rb`, `.sh` (skipping `node_modules`, `.git`, `dist`, `build`, and any virtualenv/conda dir — `.venv`/`venv`/`.tox`/`.nox`/`.direnv`, any `site-packages`, or a folder containing `pyvenv.cfg` — so a library's own env reads are not reported) for `os.environ`, `os.getenv`, `process.env`, `import.meta.env`, `os.Getenv`, `ENV[...]`, and shell `$VAR` references. Only regular files are read (a FIFO/device named like code is skipped). A `#` immediately after `=` (`COLOR=#ff0000`) is a literal value; only ` #` with a preceding space is an inline comment.
   - Reports missing keys in `.env`, undocumented keys missing from `.env.example`, and empty or placeholder values (`""`, `changeme`, `your_api_key_here`, `TODO`, `<...>`) without revealing real values.
2. **`docker` (non-hanging 2s probe):**
   - On Unix, connects directly to the active endpoint — `DOCKER_HOST`, the active `docker context`'s socket (`DOCKER_CONTEXT` / `~/.docker/config.json` `currentContext`, resolved without running `docker`, so Colima/rootless/remote setups are found), `~/.docker/run/docker.sock`, or `/var/run/docker.sock` — and sends `GET /_ping` with a strict 2.0s deadline **before** calling any `docker` CLI command.
   - If the socket accepts a connection and hangs past 2.0s, reports `hung` immediately without running `docker ps` (preventing terminal stalls) and shows OS-specific recovery guidance without executing it.
   - Only when `/_ping` answers `200 OK` (or `docker info` succeeds on TCP/TLS/Windows), runs `docker ps -a` (4s timeout) to list containers and flag exited/unhealthy ones.
3. **`ports` & `runtime`:**
   - Probes `127.0.0.1` (and `::1`) ports with a 0.2s timeout and performs optional PID/process lookup when listening.
   - In `check`, verifies `.venv` / `venv` symlinks and executables (when Python manifests exist) and `node_modules/` presence (when `package.json` exists).

## Rules

- **Never inspect `.env` directly** with `cat`, `head`, `read_file`, or `printenv` — use `env_check.py dotenv`.
- **Never auto-kill or restart Docker** on a `hung` status without the user's explicit request: report the status and recovery command (`kill -9` on `com.docker.backend` + `open -g -a Docker` on macOS).
- If `env_check.py docker` fails due to socket permissions inside the sandbox, re-run with `BypassSandbox: true`.
