"""Stop hook: verify reminder after code changes; web UI needs a real look (ui_check.py)."""
import json, os, shutil, subprocess, sys, time
PLUGIN, SP = sys.argv[1], sys.argv[2]; HOOKS = os.path.join(PLUGIN, "hooks")
sys.path.insert(0, HOOKS); import conversation as conv
cid = "agp-verifytest-0001"
ws = os.path.join(SP, "vf_ws"); art = os.path.join(SP, "vf_art"); tp = os.path.join(art, "transcript_full.jsonl")
def setup(steps, agents_md=None):
    for d in (ws, art): shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
    open(os.path.join(ws, "AGENTS.md"), "w").write(agents_md or "# Test project\n")  # a set-up project: no setup nudge
    subprocess.run(["git", "init", "-q"], cwd=ws)
    for f in os.listdir(conv.STATE_DIR):
        if f.startswith(cid): os.remove(os.path.join(conv.STATE_DIR, f))
    rows = [{"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-04T10:00:00Z", "content": "<USER_REQUEST>Сделай фичу</USER_REQUEST>"}]
    for i, (name, args) in enumerate(steps, 1):
        rows.append({"step_index": i, "type": "PLANNER_RESPONSE", "content": "", "tool_calls": [{"name": name, "args": args}]})
    rows.append({"step_index": len(rows), "type": "PLANNER_RESPONSE", "content": "Готово."})
    open(tp, "w").write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
def w(n): return ("write_file", {"Path": os.path.join(ws, n), "Content": "x=1\n"})
def cmd(c): return ("run_command", {"CommandLine": c})
def stop():
    p = {"conversationId": cid, "transcriptPath": tp, "artifactDirectoryPath": art, "workspacePaths": [ws], "terminationReason": "NO_TOOL_CALL", "fullyIdle": True, "executionNum": 0}
    r = subprocess.run([sys.executable, os.path.join(HOOKS, "stop.py")], input=json.dumps(p), capture_output=True, text=True)
    if r.stderr.strip(): print("   stderr:", r.stderr.strip()[:200])
    d = json.loads(r.stdout or "{}"); return d.get("reason") if d.get("decision") == "continue" else None
cases = [
 ("1 code file, nothing run",                 [w("a.py")], None, "verify"),
 ("1 code file, then pytest",                 [w("a.py"), cmd("pytest -q")], None, None),
 ("pytest before the last change",            [cmd("pytest -q"), w("a.py")], None, "verify"),
 ("ran the program: python3 main.py",         [w("a.py"), cmd("cd app && python3 main.py")], None, None),
 ("docker compose run tests",                 [w("a.py"), cmd("docker compose run --rm app pytest")], None, None),
 ("only git diff / ls after",                 [w("a.py"), cmd("git diff"), cmd("ls -la")], None, "verify"),
 ("markdown only",                            [w("README.md")], None, None),
 ("3 files, no review, no tests",             [w("a.py"), w("b.py"), w("c.ts")], None, "review+verify"),
 ("3 files + review + tests",                 [w("a.py"), w("b.py"), w("c.py"), ("invoke_subagent", {"TypeName": "code-reviewer"}), cmd("npm test")], None, None),
 ("AGENTS.md test command is suggested",      [w("a.py")], "## Commands\n- Test all: `pytest -q` · single: `pytest tests/test_x.py`\n", "verify:pytest -q"),
 ("page only, nothing run",                   [w("index.html")], None, "look"),
 ("page + fake check (python3 -c print)",     [w("index.html"), cmd("python3 -c \"print('HTML syntax check passed')\"")], None, "look"),
 ("page + pytest (tests don't show a page)",  [w("index.html"), cmd("pytest -q")], None, "look"),
 ("page + ui_check.py",                       [w("index.html"), cmd('python3 "/x/ui_check.py" "index.html" --widths 390,1440')], None, None),
 ("page, ui_check, then edited again",        [w("index.html"), cmd('python3 /x/ui_check.py index.html'), ("edit_file", {"Path": os.path.join(ws, "index.html")})], None, "look"),
 ("styles only → placeholder target",         [w("style.css")], None, "look:<URL or .html file"),
 ("component + npm test",                     [w("App.tsx"), cmd("npm test")], None, None),
 ("component, nothing run",                   [w("App.tsx")], None, "look"),
 ("page + code, nothing run",                 [w("a.py"), w("index.html")], None, "verify+look"),
 ("page + code, pytest",                      [w("a.py"), w("index.html"), cmd("pytest")], None, "look-only"),
 ("browser agent looked",                     [w("index.html"), ("browser_subagent", {"Task": "open the page"})], None, None),
]
ok = True
for name, steps, agents_md, want in cases:
    setup(steps, agents_md); got = stop()
    if want is None: good = got is None
    elif want == "verify": good = bool(got) and "ran no check" in got
    elif want == "review+verify": good = bool(got) and "/review" in got and "tests or build" in got
    elif want == "look": good = bool(got) and "haven't looked at it" in got and "ui_check.py" in got and "ran no check" not in got and (".html" not in steps[0][1]["Path"] or '"' + os.path.realpath(steps[0][1]["Path"]) + '"' in got)
    elif want == "verify+look": good = bool(got) and "ran no check" in got and "Also look at the web UI" in got and "(a.py)" in got
    elif want == "look-only": good = bool(got) and "haven't looked at it" in got and "ran no check" not in got
    else: good = bool(got) and want.split(":", 1)[1] in got
    ok &= good
    print(f"{'✅' if good else '❌'} {name:<42} → {(got or 'stop')[:120]}")
for d in (ws, art): shutil.rmtree(d, ignore_errors=True)
for f in os.listdir(conv.STATE_DIR):
    if f.startswith(cid): os.remove(os.path.join(conv.STATE_DIR, f))
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
