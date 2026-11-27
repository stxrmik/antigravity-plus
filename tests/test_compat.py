"""compat.py on the platform it runs on: file locks between processes, atomic replace, workspace URIs,
the Python command from hooks.json, finding processes and their listening ports."""
import os, socket, subprocess, sys, time
PLUGIN, SP = sys.argv[1], sys.argv[2]; HOOKS = os.path.join(PLUGIN, "hooks")
sys.path.insert(0, HOOKS)
import compat, conversation as conv
ok = True
def check(name, cond, got=""):
    global ok; ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + ("" if cond else f" → {got}"))

lock_path = os.path.join(SP, "x.lock")
fh = open(lock_path, "w")
compat.lock(fh)
child = subprocess.Popen([sys.executable, "-c", f"import sys; sys.path.insert(0, {HOOKS!r}); import compat; "
                          f"f = open({lock_path!r}, 'w'); compat.lock(f); compat.unlock(f)"])
time.sleep(1.0)
check("a second process waits for the lock", child.poll() is None, child.poll())
compat.unlock(fh); fh.close()
check("…and gets it once it's released", child.wait(timeout=30) == 0)

target, tmp = os.path.join(SP, "state.json"), os.path.join(SP, "state.json.tmp")
open(target, "w").write("old"); open(tmp, "w").write("new")
compat.replace(tmp, target)
check("replace swaps the file in", open(target).read() == "new" and not os.path.exists(tmp))

p = conv.to_path("file:///c%3A/Users/me/proj")
check("file:///c%3A/… (Windows workspace URI) → c:…proj", p.lower().startswith("c:") and p.endswith("proj"), p)
if not compat.WINDOWS:
    check("file:///Users/me/My%20Proj → /Users/me/My Proj", conv.to_path("file:///Users/me/My%20Proj") == "/Users/me/My Proj")
check("python_cmd() reads hooks.json", compat.python_cmd() == "python3", compat.python_cmd())
check("slashes() only rewrites on Windows", compat.slashes(r"a\b") == ("a/b" if compat.WINDOWS else r"a\b"))

me = [pid for pid, cmd in compat.processes("test_compat")]
check("processes() finds this test", os.getpid() in me, me)
srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
port = srv.getsockname()[1]
ports = compat.listening_ports(os.getpid())
check(f"listening_ports() sees our port {port}", port in ports, ports)
srv.close()
check("browsers() lists candidates", len(compat.browsers()) > 3)

# ---- App vs CLI (contract B) ----
home = os.path.expanduser("~")
desk, cli = os.path.join(home, ".gemini", "antigravity"), os.path.join(home, ".gemini", "antigravity-cli")
import shutil as _sh
for d in (desk, cli):
    _sh.rmtree(d, ignore_errors=True)
check("data_dirs(): neither App nor CLI → []", compat.data_dirs() == [], compat.data_dirs())
os.makedirs(os.path.join(cli, "brain", "cid-cli-0001"))
check("data_dirs(): only the CLI → [cli]", compat.data_dirs() == [cli], compat.data_dirs())
os.makedirs(os.path.join(desk, "brain"))
check("data_dirs(): App first, then CLI", compat.data_dirs() == [desk, cli], compat.data_dirs())
tp_cli = cli + "/brain/abc/.system_generated/logs/transcript.jsonl"
check("data_dir(CLI transcript) → CLI folder", compat.data_dir(tp_cli) == cli, compat.data_dir(tp_cli))
check("data_dir('~/…') expands ~", compat.data_dir("~/.gemini/antigravity-cli/brain/x/t.jsonl") == cli)
check("data_dir() without a path → App folder", compat.data_dir() == desk and compat.data_dir("junk") == desk)
check("data_dir(Windows path) → its root",
      compat.data_dir(r"C:\Users\me\.gemini\antigravity-cli\brain\x\t.jsonl").replace("\\", "/").endswith(
          "/.gemini/antigravity-cli"), compat.data_dir(r"C:\Users\me\.gemini\antigravity-cli\brain\x\t.jsonl"))
check("runtime(CLI transcriptPath) → cli", compat.runtime({"transcriptPath": tp_cli}) == "cli")
check("runtime(App transcriptPath) → desktop",
      compat.runtime({"transcriptPath": desk + "/brain/a/.system_generated/logs/transcript.jsonl"}) == "desktop")
check("runtime(None / junk) → desktop", compat.runtime(None) == "desktop" and compat.runtime([1]) == "desktop")
check("runtime(): no transcriptPath, conversation only in the CLI folder → cli",
      compat.runtime({"conversationId": "cid-cli-0001"}) == "cli")
os.environ["AGP_RUNTIME"] = "cli"
check("runtime(): AGP_RUNTIME=cli", compat.runtime({}) == "cli")
del os.environ["AGP_RUNTIME"]
_lsv = os.environ.get("ANTIGRAVITY_LS_VERSION")
os.environ["ANTIGRAVITY_LS_VERSION"] = "cli-1.5.18"
check("runtime(): an agent command of the CLI (ANTIGRAVITY_LS_VERSION=cli-…) → cli", compat.runtime({}) == "cli")
os.environ["ANTIGRAVITY_LS_VERSION"] = "2.35.2"
check("runtime(): the App's ANTIGRAVITY_LS_VERSION → desktop", compat.runtime({}) == "desktop")
if _lsv is None:
    del os.environ["ANTIGRAVITY_LS_VERSION"]
else:
    os.environ["ANTIGRAVITY_LS_VERSION"] = _lsv
_env_ls = {k: os.environ.pop(k, None) for k in ("ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN")}
check("language_server(conversation=True) from a CLI session's hook → None (the App's server doesn't know it)",
      compat.language_server(probe=0.2, conversation=True, payload={"transcriptPath": tp_cli}) is None)
os.environ["ANTIGRAVITY_LS_ADDRESS"], os.environ["ANTIGRAVITY_CSRF_TOKEN"] = "127.0.0.1:9", "t"
got = compat.language_server(conversation=True, payload={"transcriptPath": tp_cli})
check("…an agent command keeps its own server from env", got is not None and got.addr == "127.0.0.1:9", got)
for k, v in _env_ls.items():
    if v is None:
        os.environ.pop(k, None)
    else:
        os.environ[k] = v
os.environ["AGP_AGY_HEADLESS"] = "1"
check("agy_headless(): AGP_AGY_HEADLESS=1", compat.agy_headless() is True)
os.environ["AGP_AGY_HEADLESS"] = "?"
check("agy_headless(): AGP_AGY_HEADLESS=? → None (can't tell)", compat.agy_headless() is None)
os.environ["AGP_AGY_HEADLESS"] = "0"
check("agy_headless(): AGP_AGY_HEADLESS=0 → False", compat.agy_headless() is False)
os.environ["AGP_AGY_HEADLESS"] = ""
check("agy_headless(): not under agy → None, never False", compat.agy_headless() is None)
for args in (["-p", "/bypass"], ["-p=/bypass"], ["-print", "/bypass"], ["--p", "x"], ["--print"], ["-prompt=x"],
             ["--prompt", "x"], ["-input-format=stream-json"], ["--input-format", "stream-json"], ["--PRINT"],
             ["acp"], ["--model", "m", "acp"]):
    check(f"agy_args_headless({' '.join(args)}) → headless", compat.agy_args_headless(args) is True)
for args in ([], ["-i", "hello"], ["--prompt-interactive=hi"], ["--model", "gemini-4-hydrogen-low"], ["-c"],
             ["--conversation", "abc"], ["-", "--"]):
    check(f"agy_args_headless({' '.join(args) or '∅'}) → interactive", compat.agy_args_headless(args) is False)
for name, want in (("write_to_file", "write_file"), ("view_file", "read_file"), ("replace_file_content", "edit_file"),
                   ("multi_replace_file_content", "edit_file"), ("run_command", "run_command"), ("odd_tool", "odd_tool")):
    check(f"tool({name}) → {want}", compat.tool(name) == want, compat.tool(name))
check("tool(None) → ''", compat.tool(None) == "")
check("tool_names(write_file) lists synonyms", "write_to_file" in compat.tool_names("write_file"))
saved_path = os.environ.get("PATH", "")
os.environ["PATH"] = SP
check("agy(): not installed → ''", compat.agy() == "", compat.agy())
if not compat.WINDOWS:
    fake = os.path.join(SP, "agy")
    open(fake, "w").write("#!/bin/sh\necho 'agy version 1.5.16'\n"); os.chmod(fake, 0o755)
    check("agy(): found on PATH", compat.agy() == fake, compat.agy())
    t0 = time.time()
    check("agy_version() parses `agy --version`", compat.agy_version() == "1.5.16", compat.agy_version())
    check("agy_version() is cached", compat.agy_version() == "1.5.16" and time.time() - t0 < 3)
os.environ["PATH"] = saved_path
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
