"""Coordination: a write over a version the agent hasn't seen is refused."""
import json, os, shutil, subprocess, sys, time
PLUGIN, SP = sys.argv[1], sys.argv[2]; HOOKS = os.path.join(PLUGIN, "hooks")
sys.path.insert(0, HOOKS); import coord, conversation as conv
ws = os.path.realpath(os.path.join(SP, "coord_ws")); shutil.rmtree(ws, ignore_errors=True); os.makedirs(ws)
reg = os.path.join(coord.COORD_DIR, coord.slug(ws) + ".json")
for p in (reg, reg + ".lock"):
    if os.path.exists(p): os.remove(p)
A, B, S = "agp-coord-agent-A1", "agp-coord-agent-B1", "agp-coord-agent-S1"
def hook(script, cid, extra):
    r = subprocess.run([sys.executable, os.path.join(HOOKS, script)], input=json.dumps({"conversationId": cid, "workspacePaths": [ws], **extra}), capture_output=True, text=True)
    if r.stderr.strip(): print("   stderr:", r.stderr.strip()[:300])
    return json.loads(r.stdout or "{}")
def post(cid, tool, path): return hook("check_edit.py", cid, {"toolCall": {"name": tool, "args": {"Path": path}}, "error": ""})
def pre(cid, tool, path): return hook("guard.py", cid, {"toolCall": {"name": tool, "args": {"Path": path}}})
def write(path, text): open(path, "w").write(text); time.sleep(0.01)
f = os.path.join(ws, "app.py"); write(f, "x = 1\n")
ok = True
def expect(name, decision, want_deny, contains=""):
    global ok
    deny = decision.get("decision") == "deny"
    good = deny == want_deny and (contains in decision.get("reason", "") if want_deny and contains else True)
    ok &= good
    print(f"{'✅' if good else '❌'} {name:<58} → {decision.get('decision')}" + (f": {decision.get('reason','')[:110]}" if deny else ""))
post(A, "read_file", f); post(B, "read_file", f)
expect("A edits a file nobody changed since A read it", pre(A, "edit_file", f), False)
write(f, "x = 2  # by B\n"); post(B, "edit_file", f)
expect("A edits after B changed it (A has an old version)", pre(A, "edit_file", f), True, "другой агент")
post(A, "read_file", f)
expect("A re-reads, then edits", pre(A, "edit_file", f), False)
write(f, "x = 3  # by Claude Code / editor\n")
expect("changed outside any agent after A read it", pre(A, "write_file", f), True, "кто-то другой")
g = os.path.join(ws, "util.py"); write(g, "y = 1\n"); post(B, "write_file", g)
expect("A overwrites util.py it never read, B wrote it just now", pre(A, "write_file", g), True, "ещё не читали")
h = os.path.join(ws, "old.py"); write(h, "z = 1\n")
expect("A writes old.py nobody touched (never read)", pre(A, "write_file", h), False)
expect("A creates a new file", pre(A, "write_file", os.path.join(ws, "new.py")), False)
# family: S is A's subagent
with coord.Registry(ws) as data:
    coord._agent(data, S)["parent"] = A
post(S, "read_file", f); post(A, "read_file", f)
write(f, "x = 4  # by subagent S\n"); post(S, "edit_file", f)
expect("A after its own subagent changed the file (stale → re-read)", pre(A, "edit_file", f), True)
# awareness
hook("instructions.py", B, {"lastUserInput": "x"})
st = {}
m1 = coord.others_message({"conversationId": A, "workspacePaths": [ws]}, st)
m2 = coord.others_message({"conversationId": A, "workspacePaths": [ws]}, st)
good = bool(m1) and B in m1 and S not in m1 and "app.py" in m1 and m2 is None
ok &= good
print(f"{'✅' if good else '❌'} A sees B (not its own subagent S), once                      → {m1.splitlines()[2][:90] if m1 else None}")
with coord.Registry(ws) as data:
    data["agents"][B]["last"] -= 11 * 60
st2 = {}
m3 = coord.others_message({"conversationId": A, "workspacePaths": [ws]}, st2)
ok &= m3 is None
print(f"{'✅' if m3 is None else '❌'} B idle for 11 min → not listed")
print("mine(A):", [os.path.basename(p) for p in coord.mine(ws, A)], " mine(B):", [os.path.basename(p) for p in coord.mine(ws, B)])
shutil.rmtree(ws); [os.remove(p) for p in (reg, reg + ".lock") if os.path.exists(p)]
for c in (A, B, S):
    for suffix in (".json", ".checks.json", ".checks.json.lock"):
        p = os.path.join(conv.STATE_DIR, c + suffix)
        if os.path.exists(p): os.remove(p)
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
