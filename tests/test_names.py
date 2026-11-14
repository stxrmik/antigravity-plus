"""Every script of the plugin: it parses, and no function uses a name that nothing defines (a forgotten import)."""
import ast, builtins, os, subprocess, symtable, sys
PLUGIN, SP = sys.argv[1], sys.argv[2]
ROOT = os.path.dirname(PLUGIN)
ok = True
def check(name, cond, detail=""):
    global ok; ok &= bool(cond); print(f"{'✅' if cond else '❌'} {name}" + (f" — {str(detail)[:900]}" if not cond and detail != "" else ""))
def undefined(path):
    """(function, name) for globals a function reads that the module never binds."""
    with open(path, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src, path)
    top = symtable.symtable(src, path, "exec")
    known = {s.get_name() for s in top.get_symbols() if s.is_assigned() or s.is_imported() or s.is_namespace()}
    known |= set(dir(builtins)) | {"__file__", "__name__", "__doc__"}
    known |= {n for node in ast.walk(tree) if isinstance(node, ast.Global) for n in node.names}
    out = []
    def walk(table):
        for child in table.get_children():
            out.extend((child.get_name(), s.get_name()) for s in child.get_symbols()
                       if s.is_global() and s.is_referenced() and s.get_name() not in known)
            walk(child)
    walk(top)
    return out
files = []
for base in (PLUGIN, os.path.join(ROOT, "tests"), os.path.join(ROOT, "claude-code"), ROOT):
    for folder, dirs, names in os.walk(base):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "node_modules")] if base != ROOT else []
        files += [os.path.join(folder, n) for n in names if n.endswith(".py")]
files = sorted(set(files))
problems = []
for path in files:
    try:
        problems += [f"{os.path.relpath(path, ROOT)}: {fn}() uses {name!r}, which nothing defines" for fn, name in undefined(path)]
    except SyntaxError as e:
        problems.append(f"{os.path.relpath(path, ROOT)}: {e}")
check(f"{len(files)} scripts parse and every name they use is defined", len(files) > 40 and not problems, "\n".join(problems))
probe = os.path.join(SP, "probe.py")
with open(probe, "w", encoding="utf-8") as f:
    f.write("import os\nX = 1\ndef good():\n    return os.sep, X, len('a')\ndef bad():\n    return compat.python_cmd()\n")
check("the check itself catches a forgotten import", undefined(probe) == [("bad", "compat")], undefined(probe))
# /handoff's list of recent work: crashed with a NameError from 0.11.0 to 0.15.0 whenever there was something to list
ws = os.path.join(SP, "hf_ws"); os.makedirs(ws)
sys.path.insert(0, os.path.join(PLUGIN, "skills", "memory", "scripts")); sys.path.insert(0, os.path.join(PLUGIN, "hooks"))
import memory as memstore
folder, _store = memstore.project_dir(ws, memstore.settings())
os.makedirs(folder, exist_ok=True)
with open(os.path.join(folder, "handoff-2026-11-10-probe.md"), "w", encoding="utf-8") as f:
    f.write("---\nname: handoff-2026-11-10-probe\ndescription: Проверка списка\nmetadata:\n  type: project\n---\n# Проверка\n## Осталось\n- [ ] пункт\n")
r = subprocess.run([sys.executable, os.path.join(PLUGIN, "skills", "handoff", "scripts", "handoff.py"), "recent", "--workspace", ws],
                   capture_output=True, text=True, encoding="utf-8")
check("handoff.py recent lists a note and how to continue from it", r.returncode == 0 and "handoff-2026-11-10-probe" in r.stdout
      and "collect --conversation" in r.stdout and "Traceback" not in r.stderr, r.stdout[-300:] + r.stderr[-400:])
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
