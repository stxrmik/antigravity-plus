"""/env: safe .env parity & code scan without leaking secrets, 2s non-hanging Docker socket probe, ports & runtime."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

PLUGIN, SP = sys.argv[1], sys.argv[2]
ENV_CHECK = os.path.join(PLUGIN, "skills", "env", "scripts", "env_check.py")
WIN = os.name == "nt"

sys.path.insert(0, os.path.join(PLUGIN, "skills", "env", "scripts"))
import env_check  # type: ignore[import-not-found]  # noqa: E402

ok = True


def check(name: str, cond: object, detail: Any = "") -> None:
    global ok
    ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + (f" — {str(detail)[:1200]}" if not cond and detail != "" else ""))


def write(root: str, rel: str, content: str) -> str:
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def make_exec(path: str, script_body: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if WIN:
        cmd_path = path if path.endswith(".cmd") else path + ".cmd"
        py_path = path + ".py"
        with open(py_path, "w", encoding="utf-8") as f:
            f.write(script_body)
        with open(cmd_path, "w", encoding="utf-8") as f:
            f.write(f'@echo off\r\n"{sys.executable}" "{py_path}" %*\r\n')
        return cmd_path
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"#!{sys.executable}\n{script_body}\n")
    os.chmod(path, 0o755)
    return path


def run_env(args: List[str], extra_env: Optional[Dict[str, str]] = None) -> Tuple[int, str, str]:
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    r = subprocess.run(
        [sys.executable, ENV_CHECK, *args],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return r.returncode, r.stdout, r.stderr


def run_json(args: List[str], extra_env: Optional[Dict[str, str]] = None) -> Tuple[int, Dict[str, Any], str, str]:
    code, out, err = run_env([*args, "--json"], extra_env=extra_env)
    try:
        data = json.loads(out) if out.strip() else {}
    except ValueError:
        data = {"_raw": out}
    return code, data, out, err


# ── 1. .env vs .env.example + code scan, strict secret non-leakage ──
ws_env = os.path.join(SP, "ws_env")
TEST_VAL = "super-secret-value-xyz123"
PLACEHOLDER_VAL = "your_api_key_here"
write(ws_env, ".env", (
    "# comment should be ignored\n"
    f"export API_KEY={TEST_VAL} # inline comment\n"
    'QUOTED_SECRET="super-secret-value-xyz123"\n'
    f"PLACEHOLDER_ONE='{PLACEHOLDER_VAL}'\n"
    "PLACEHOLDER_TWO=changeme\n"
    "PLACEHOLDER_THREE=TODO\n"
    "PLACEHOLDER_FOUR=<insert_key>\n"
    "EMPTY_ONE=\n"
    'EMPTY_TWO=""\n'
    "EXTRA_ONLY_IN_ENV=demo_val\n"
))
write(ws_env, ".env.example", (
    "# template\n"
    "API_KEY=\n"
    "QUOTED_SECRET=\n"
    "PLACEHOLDER_ONE=\n"
    "PLACEHOLDER_TWO=\n"
    "PLACEHOLDER_THREE=\n"
    "PLACEHOLDER_FOUR=\n"
    "EMPTY_ONE=\n"
    "EMPTY_TWO=\n"
    "MISSING_FROM_ENV_EXAMPLE=\n"
))
write(ws_env, "src/app.py", (
    "import os\n"
    "a = os.environ['API_KEY']\n"
    "b = os.environ.get('PY_CODE_VAR', '')\n"
    "c = os.getenv('PY_GETENV_VAR')\n"
    "p = os.getenv('PATH')\n"
    "w = os.getenv('ProgramFiles')\n"
))
write(ws_env, "src/web.ts", (
    "const k = process.env.JS_CODE_VAR;\n"
    "const v = import.meta.env.VITE_PUBLIC_URL;\n"
    "const n = process.env.NODE_ENV;\n"
))
write(ws_env, "src/main.go", 'package main\nimport "os"\nvar x = os.Getenv("GO_CODE_VAR")\n')
write(ws_env, "src/app.rb", 'x = ENV["RB_CODE_VAR"]\n')
write(ws_env, "scripts/run.sh", "#!/bin/sh\nLOCAL_ONLY=1\necho $LOCAL_ONLY $SH_CODE_VAR\n")
# Ignored dirs must be skipped
write(ws_env, "node_modules/pkg/index.js", "process.env.IGNORED_NM_VAR\n")
write(ws_env, ".venv/lib/x.py", "import os\nos.getenv('IGNORED_VENV_VAR')\n")
write(ws_env, "dist/bundle.js", "process.env.IGNORED_DIST_VAR\n")

code_text, txt_out, txt_err = run_env(["dotenv", "--workspace", ws_env])
code_js, data, json_out, json_err = run_json(["dotenv", "--workspace", ws_env])

missing_env_keys = {x["key"] for x in data.get("missing_in_env", [])}
missing_ex_keys = {x["key"] for x in data.get("missing_in_example", [])}
placeholder_keys = {x["key"]: x["issue"] for x in data.get("empty_or_placeholder", [])}

check("dotenv exits 1 when parity/placeholder issues exist", code_text == 1 and code_js == 1, (code_text, code_js, txt_err))
check(
    "dotenv detects missing keys from .env.example and multi-language code scan",
    {"MISSING_FROM_ENV_EXAMPLE", "PY_CODE_VAR", "PY_GETENV_VAR", "JS_CODE_VAR", "VITE_PUBLIC_URL",
     "GO_CODE_VAR", "RB_CODE_VAR", "SH_CODE_VAR"}.issubset(missing_env_keys),
    missing_env_keys,
)
check(
    "dotenv skips builtin/local vars and node_modules/.venv/dist",
    not (missing_env_keys & {"PATH", "ProgramFiles", "NODE_ENV", "LOCAL_ONLY", "IGNORED_NM_VAR", "IGNORED_VENV_VAR", "IGNORED_DIST_VAR"}),
    missing_env_keys,
)
check("dotenv detects keys missing in .env.example", "EXTRA_ONLY_IN_ENV" in missing_ex_keys, missing_ex_keys)
check(
    "dotenv flags empty and placeholder values",
    placeholder_keys == {
        "EMPTY_ONE": "empty",
        "EMPTY_TWO": "empty",
        "PLACEHOLDER_FOUR": "placeholder",
        "PLACEHOLDER_ONE": "placeholder",
        "PLACEHOLDER_THREE": "placeholder",
        "PLACEHOLDER_TWO": "placeholder",
    },
    placeholder_keys,
)
check(
    "dotenv NEVER prints real secret value or raw placeholder value in text or JSON",
    TEST_VAL not in txt_out and TEST_VAL not in txt_err
    and TEST_VAL not in json_out and TEST_VAL not in json_err
    and PLACEHOLDER_VAL not in txt_out and PLACEHOLDER_VAL not in json_out,
    (txt_out, json_out),
)

# Clean parity case + .env.local override + .env.template
ws_clean = os.path.join(SP, "ws_clean")
write(ws_clean, ".env", f"DEMO_KEY={TEST_VAL}\nLOCAL_VAR=\n")
write(ws_clean, ".env.local", "LOCAL_VAR=demo_override_value\n")
write(ws_clean, ".env.template", "DEMO_KEY=\nLOCAL_VAR=\n")
write(ws_clean, "app.py", "import os\nos.getenv('DEMO_KEY')\n")
c_clean, d_clean, out_clean, _ = run_json(["dotenv", "--workspace", ws_clean])
check("clean .env + .env.local override vs .env.template exits 0",
      c_clean == 0 and d_clean.get("issues_count") == 0 and TEST_VAL not in out_clean, (c_clean, d_clean))


# ── 2. Docker tests: hung socket (<4s, no docker call), healthy /_ping, missing socket ──
def _short_sock_path(filename: str) -> Tuple[str, Optional[str]]:
    cand = os.path.join(SP, filename)
    if len(cand.encode("utf-8")) < 95:
        return cand, None
    tmp_dir = tempfile.mkdtemp(prefix="agp-dk-")
    return os.path.join(tmp_dir, filename), tmp_dir


if hasattr(socket, "AF_UNIX"):
    # 2a. Hung daemon socket: accepts and sleeps without replying
    hung_sock, hung_tmp = _short_sock_path("hung.sock")
    stop_hung = threading.Event()
    hung_ready = threading.Event()
    bin_hung = os.path.join(SP, "bin_hung")
    docker_called_marker = os.path.join(SP, "docker_was_called.txt")
    make_exec(
        os.path.join(bin_hung, "docker"),
        f"import sys\nopen({docker_called_marker!r}, 'w').write('called')\nsys.exit(0)\n",
    )

    def _hung_server() -> None:
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conns = []
        try:
            srv.bind(hung_sock)
            srv.listen(4)
            srv.settimeout(0.2)
            hung_ready.set()
            while not stop_hung.is_set():
                try:
                    conn, _ = srv.accept()
                    conns.append(conn)
                except socket.timeout:
                    continue
                except OSError:
                    break
        finally:
            for c in conns:
                try:
                    c.close()
                except OSError:
                    pass
            srv.close()

    t_hung = threading.Thread(target=_hung_server, daemon=True)
    t_hung.start()
    hung_ready.wait(timeout=3.0)
    try:
        t0 = time.monotonic()
        c_hung, d_hung, out_hung, err_hung = run_json(
            ["docker"],
            extra_env={
                "AGP_DOCKER_SOCK": hung_sock,
                "PATH": bin_hung + os.pathsep + os.environ.get("PATH", ""),
            },
        )
        elapsed = time.monotonic() - t0
    finally:
        stop_hung.set()
        t_hung.join(timeout=2.0)
        if hung_tmp:
            import shutil
            shutil.rmtree(hung_tmp, ignore_errors=True)

    check(
        "hung Docker socket aborts < 4.0s, reports 'hung', and NEVER invokes docker CLI",
        elapsed < 4.0
        and c_hung == 1
        and d_hung.get("status") == "hung"
        and "do not run docker commands or they will hang" in d_hung.get("message", "")
        and not os.path.exists(docker_called_marker),
        (elapsed, c_hung, d_hung, err_hung),
    )
    if sys.platform == "darwin":
        check(
            "hung Docker recovery on macOS mentions kill -9 com.docker.backend + open -g -a Docker",
            "kill -9" in (d_hung.get("recovery") or "")
            and "com.docker.backend" in (d_hung.get("recovery") or "")
            and "open -g -a Docker" in (d_hung.get("recovery") or ""),
            d_hung.get("recovery"),
        )

    # 2b. Healthy mock /_ping returning HTTP/1.0 200 OK\r\n\r\nOK + fake docker ps
    ok_sock, ok_tmp = _short_sock_path("ok.sock")
    stop_ok = threading.Event()
    ok_ready = threading.Event()
    bin_ok = os.path.join(SP, "bin_ok")
    make_exec(
        os.path.join(bin_ok, "docker"),
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "if args[:3] == ['ps', '-a', '--format'] and args[3] == 'json':\n"
        "    print(json.dumps({'ID':'111122223333','Names':'demo-api','Image':'python:3.12','State':'running','Status':'Up 10m','Ports':'8000/tcp'}))\n"
        "    print(json.dumps({'ID':'444455556666','Names':'demo-worker','Image':'python:3.12','State':'running','Status':'Up 5m (unhealthy)','Ports':''}))\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n",
    )

    def _ok_server() -> None:
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(ok_sock)
            srv.listen(4)
            srv.settimeout(0.2)
            ok_ready.set()
            while not stop_ok.is_set():
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    conn.settimeout(1.0)
                    _ = conn.recv(1024)
                    conn.sendall(b"HTTP/1.0 200 OK\r\n\r\nOK")
                except OSError:
                    pass
                finally:
                    conn.close()
        finally:
            srv.close()

    t_ok = threading.Thread(target=_ok_server, daemon=True)
    t_ok.start()
    ok_ready.wait(timeout=3.0)
    try:
        c_ok, d_ok, _, err_ok = run_json(
            ["docker"],
            extra_env={
                "AGP_DOCKER_SOCK": ok_sock,
                "PATH": bin_ok + os.pathsep + os.environ.get("PATH", ""),
            },
        )
    finally:
        stop_ok.set()
        t_ok.join(timeout=2.0)
        if ok_tmp:
            import shutil
            shutil.rmtree(ok_tmp, ignore_errors=True)

    check(
        "healthy /_ping returns status=ok, ping_ok=True, and lists running/unhealthy containers",
        c_ok == 0
        and d_ok.get("status") == "ok"
        and d_ok.get("ping_ok") is True
        and [c["name"] for c in d_ok.get("containers", [])] == ["demo-api", "demo-worker"]
        and [c["name"] for c in d_ok.get("unhealthy_containers", [])] == ["demo-worker"],
        (c_ok, d_ok, err_ok),
    )

# 2c. Missing socket graceful handling
missing_sock = os.path.join(SP, "does_not_exist.sock")
c_miss, d_miss, _, err_miss = run_json(
    ["docker"],
    extra_env={"AGP_DOCKER_SOCK": missing_sock, "PATH": ""},
)
check(
    "missing Docker socket reports gracefully and exits 0",
    c_miss == 0 and d_miss.get("status") in ("not_running", "not_installed") and d_miss.get("ping_ok") is False,
    (c_miss, d_miss, err_miss),
)


# ── 3. Ports check on ephemeral listening 127.0.0.1 socket + closed port ──
srv_port = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv_port.bind(("127.0.0.1", 0))
srv_port.listen(1)
open_port = srv_port.getsockname()[1]

tmp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
tmp_sock.bind(("127.0.0.1", 0))
closed_port = tmp_sock.getsockname()[1]
tmp_sock.close()

try:
    c_ports, d_ports, _, err_ports = run_json(["ports", "--ports", f"{open_port},{closed_port}"])
finally:
    srv_port.close()

port_map = {p["port"]: p for p in d_ports.get("ports", [])}
check(
    "ports check detects ephemeral listening 127.0.0.1 socket and closed port",
    c_ports == 0
    and port_map.get(open_port, {}).get("listening") is True
    and port_map.get(closed_port, {}).get("listening") is False
    and d_ports.get("listening_count") == 1,
    (c_ports, d_ports, err_ports),
)


# ── 4. Default 'check' subcommand + runtime checks (missing node_modules & broken .venv) ──
ws_rt = os.path.join(SP, "ws_rt")
write(ws_rt, "package.json", '{"name": "demo-app"}\n')
if not WIN and hasattr(os, "symlink"):
    os.makedirs(ws_rt, exist_ok=True)
    os.symlink(os.path.join(SP, "nonexistent_venv_target"), os.path.join(ws_rt, ".venv"))
else:
    os.makedirs(os.path.join(ws_rt, ".venv"), exist_ok=True)  # missing python executable -> broken

c_rt, d_rt, _, err_rt = run_json(
    ["--workspace", ws_rt, "--ports", str(closed_port)],
    extra_env={"AGP_DOCKER_SOCK": missing_sock, "PATH": ""},
)
rt = d_rt.get("runtime", {})
check(
    "default check subcommand runs all checks and detects broken .venv + missing node_modules",
    c_rt == 1
    and rt.get("python", {}).get("status") == "broken"
    and rt.get("node", {}).get("status") == "missing"
    and "dotenv" in d_rt and "docker" in d_rt and "ports" in d_rt,
    (c_rt, d_rt, err_rt),
)

# Clean default check (node_modules exists, no python manifests/venv, clean env)
ws_ok_all = os.path.join(SP, "ws_ok_all")
write(ws_ok_all, "package.json", '{"name": "demo-app"}\n')
os.makedirs(os.path.join(ws_ok_all, "node_modules"), exist_ok=True)
write(ws_ok_all, ".env", "PORT=3000\n")
write(ws_ok_all, ".env.example", "PORT=\n")
c_all_ok, d_all_ok, _, err_all_ok = run_json(
    ["--workspace", ws_ok_all, "--ports", str(closed_port)],
    extra_env={"AGP_DOCKER_SOCK": missing_sock, "PATH": ""},
)
check("clean workspace passes default check with exit 0", c_all_ok == 0 and d_all_ok.get("exit") == 0,
      (c_all_ok, d_all_ok, err_all_ok))

# Direct unit check for env_check TSV fallback parser
tsv_parsed = env_check._parse_containers_tsv("123456\tdemo-db\tpostgres:16\texited\tExited (1) 2m ago\t5432/tcp\n")
check("TSV docker ps fallback parser handles container rows",
      len(tsv_parsed) == 1 and tsv_parsed[0]["name"] == "demo-db" and tsv_parsed[0]["state"] == "exited",
      tsv_parsed)

# An older Docker CLI prints the literal word "json" per row for `--format json`: JSON parse must return None so
# _list_containers falls back to the TSV template rather than surfacing "json" as an error.
check("literal-'json' docker ps output is not mistaken for JSON (triggers TSV fallback)",
      env_check._parse_containers_json("json\njson\n") is None,
      env_check._parse_containers_json("json\njson\n"))


# ── 5. .env value edge cases: `#` right after `=` is a literal value, not a comment ──
ws_vals = os.path.join(SP, "ws_vals")
write(ws_vals, ".env", (
    "COLOR=#ff0000\n"          # '#' immediately after '=' → literal hex value, not an empty/comment
    "HASHVAL=ab#cd\n"          # '#' with no preceding space → literal
    "SPACED=teal # trailing\n"  # ' #' with preceding space → inline comment stripped, value 'teal'
    "ONLYCOMMENT= # nothing\n"  # value is only a comment → empty
    "REALEMPTY=\n"
))
write(ws_vals, ".env.example", "COLOR=\nHASHVAL=\nSPACED=\nONLYCOMMENT=\nREALEMPTY=\n")
_c_vals, d_vals, _o_vals, _e_vals = run_json(["dotenv", "--workspace", ws_vals])
vals_status = {x["key"]: x["issue"] for x in d_vals.get("empty_or_placeholder", [])}
check(
    "dotenv treats `COLOR=#ff0000` as a set value (not empty) and only flags truly empty/comment-only values",
    "COLOR" not in vals_status and "HASHVAL" not in vals_status and "SPACED" not in vals_status
    and vals_status.get("ONLYCOMMENT") == "empty" and vals_status.get("REALEMPTY") == "empty",
    vals_status,
)

# ── 6. Virtualenv-like directories (site-packages / pyvenv.cfg) are not scanned for env references ──
ws_venv = os.path.join(SP, "ws_venv")
write(ws_venv, "app.py", "import os\nos.getenv('APP_KEY')\n")
write(ws_venv, ".env.example", "APP_KEY=\n")
write(ws_venv, ".direnv/python-3.12/lib/python3.12/site-packages/requests/utils.py",
      "import os\nos.getenv('REQUESTS_CA_BUNDLE')\n")
write(ws_venv, "myenv/pyvenv.cfg", "home = /usr/bin\n")
write(ws_venv, "myenv/lib/x.py", "import os\nos.getenv('VENV_INTERNAL_VAR')\n")
_c_venv, d_venv, _o_venv, _e_venv = run_json(["dotenv", "--workspace", ws_venv])
venv_missing = {x["key"] for x in d_venv.get("missing_in_env", [])}
check(
    "dotenv skips site-packages and pyvenv.cfg env dirs, scanning only project code",
    "APP_KEY" in venv_missing and "REQUESTS_CA_BUNDLE" not in venv_missing and "VENV_INTERNAL_VAR" not in venv_missing,
    venv_missing,
)

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
