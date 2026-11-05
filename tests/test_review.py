"""Stop hook: one /review reminder after 3+ changed code files."""
import json, os, shutil, subprocess, sys, time
PLUGIN, SP = sys.argv[1], sys.argv[2]; HOOKS = os.path.join(PLUGIN, "hooks")
sys.path.insert(0, HOOKS); import conversation as conv
cid = "agp-reviewtest-0001"
VERIFIED = [True]
ws = os.path.join(SP, "rv_ws"); art = os.path.join(SP, "rv_art")
tp = os.path.join(art, "transcript_full.jsonl")
def setup(steps, answer="Готово, всё сделал."):
    for d in (ws, art): shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
    open(os.path.join(ws, "AGENTS.md"), "w").write("# Test project\n"); subprocess.run(["git", "init", "-q"], cwd=ws)
    for f in os.listdir(conv.STATE_DIR):
        if f.startswith(cid): os.remove(os.path.join(conv.STATE_DIR, f))
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
    rows, i = [], 0
    for kind, payload in steps:
        if kind == "user":
            rows.append({"step_index": i, "type": "USER_INPUT", "created_at": ts, "content": f"<USER_REQUEST>{payload}</USER_REQUEST>"})
        else:
            rows.append({"step_index": i, "type": "PLANNER_RESPONSE", "content": "", "tool_calls": [payload]})
        i += 1
    if VERIFIED[0]:
        rows.append({"step_index": i, "type": "PLANNER_RESPONSE", "content": "", "tool_calls": [{"name": "run_command", "args": {"CommandLine": "pytest -q"}}]}); i += 1
    rows.append({"step_index": i, "type": "PLANNER_RESPONSE", "content": answer})
    open(tp, "w").write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
def w(name): return ("tool", {"name": "write_file", "args": {"Path": os.path.join(ws, name), "Content": "x = 1\n"}})
def stop(n=0):
    p = {"conversationId": cid, "transcriptPath": tp, "artifactDirectoryPath": art, "workspacePaths": [ws],
         "terminationReason": "NO_TOOL_CALL", "fullyIdle": True, "executionNum": n}
    r = subprocess.run([sys.executable, os.path.join(HOOKS, "stop.py")], input=json.dumps(p), capture_output=True, text=True)
    if r.stderr.strip(): print("   stderr:", r.stderr.strip()[:200])
    d = json.loads(r.stdout or "{}")
    return d.get("reason") if d.get("decision") == "continue" else None
U = ("user", "Сделай фичу")
cases = [
 ("3 code files, no review",                  [U, w("a.py"), w("b.py"), w("c.ts")], None, True),
 ("2 code files",                             [U, w("a.py"), w("b.py")], None, False),
 ("3 files + code-reviewer subagent ran",     [U, w("a.py"), w("b.py"), w("c.py"), ("tool", {"name": "invoke_subagent", "args": {"TypeName": "code-reviewer", "Prompt": "review"}})], None, False),
 ("3 files + namespaced reviewer",            [U, w("a.py"), w("b.py"), w("c.py"), ("tool", {"name": "invoke_subagent", "args": {"TypeName": "antigravity-plus:security-auditor"}})], None, False),
 ("3 files + review SKILL.md read",           [U, w("a.py"), w("b.py"), w("c.py"), ("tool", {"name": "read_file", "args": {"Path": "/x/antigravity-plus/skills/review/SKILL.md"}})], None, False),
 ("3 files in the previous turn",             [U, w("a.py"), w("b.py"), w("c.py"), ("user", "А теперь объясни код")], None, False),
 ("3 files, agent asks a question",           [U, w("a.py"), w("b.py"), w("c.py")], "Готово. Запустить тесты?", False),
 ("3 markdown files",                         [U, w("a.md"), w("b.md"), w("c.md")], None, False),
 ("same file edited 3 times",                 [U, w("a.py"), w("a.py"), w("a.py")], None, False),
]
ok = True
for name, steps, answer, want in cases:
    setup(steps, answer or "Готово, всё сделал.")
    got = stop()
    ok &= bool(got) == want
    print(f"{'✅' if bool(got) == want else '❌'} {name:<40} → {'review reminder' if got else 'stop'}")
setup([U, w("a.py"), w("b.py"), w("c.ts")])
first, second = stop(), stop(1)
print(f"{'✅' if first and not second else '❌'} reminder only once per turn             → {'once' if first and not second else 'repeated/none'}")
ok &= bool(first) and not second
print("\nreminder text:", (first or "")[:420])
for d in (ws, art): shutil.rmtree(d, ignore_errors=True)
for f in os.listdir(conv.STATE_DIR):
    if f.startswith(cid): os.remove(os.path.join(conv.STATE_DIR, f))
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
