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
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
