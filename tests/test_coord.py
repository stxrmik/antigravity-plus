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
    r = subprocess.run([sys.executable, os.path.join(HOOKS, script)], input=json.dumps({"conversationId": cid, "workspacePaths": [ws], **extra}),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.stderr.strip(): print("   stderr:", r.stderr.strip()[:300])
    return json.loads(r.stdout or "{}")
def post(cid, tool, path): return hook("check_edit.py", cid, {"toolCall": {"name": tool, "args": {"Path": path}}, "error": ""})
def pre(cid, tool, path, **extra_args): return hook("guard.py", cid, {"toolCall": {"name": tool, "args": {"Path": path, **extra_args}}})
def write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    time.sleep(0.01)
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
d_stale_b = pre(A, "edit_file", f)
expect("A edits after B changed it (A has an old version)", d_stale_b, True, "другой агент")
expect("…denial includes compact unified diff", d_stale_b, True, "+x = 2  # by B")
post(A, "read_file", f)
expect("A re-reads, then edits", pre(A, "edit_file", f), False)
write(f, "x = 3  # by Claude Code / editor\n")
expect("changed outside any agent after A read it", pre(A, "write_file", f), True, "кто-то другой")
expect("…an edit without a Target is not let through by retrying", pre(A, "edit_file", f), True, "read_file")
expect("…nor on the third try", pre(A, "edit_file", f), True, "read_file")
# an edit whose Target is still in the file word for word: one refusal with the diff, then the same edit goes through
write(f, "x = 4\ny = 1\n"); post(A, "read_file", f)
write(f, "x = 4\ny = 2  # by B\n"); post(B, "edit_file", f)
d_t = pre(A, "edit_file", f, Target="x = 4")
expect("B changed it, A's Target still there: refused once with the diff", d_t, True, "+y = 2  # by B")
expect("…told the diff is the whole change", d_t, True, "diff целиком")
expect("…told its Target is still there", d_t, True, "Target")
expect("…the same edit then goes through", pre(A, "edit_file", f, Target="x = 4"), False)
# Target gone (B changed those very lines): refused every time until A re-reads
write(f, "x = 40  # by B\ny = 2  # by B\n"); post(B, "edit_file", f)
expect("Target no longer in the file: refused", pre(A, "edit_file", f, Target="x = 4\n"), True, "read_file")
expect("…and again on a retry", pre(A, "edit_file", f, Target="x = 4\n"), True, "read_file")
post(A, "read_file", f)
expect("A re-reads, then the edit goes through", pre(A, "edit_file", f, Target="x = 40"), False)
# a stale whole-file write must keep what changed on disk, whatever the 10-minute window says
write(f, "a = 1\nb = 2\n"); post(A, "read_file", f)
write(f, "a = 1\nb = 20  # by B\n"); post(B, "edit_file", f)
with coord.Registry(ws) as data:  # B's write is older than the 10-minute window
    data["agents"][B]["files"][os.path.realpath(f)] = time.time() - 20 * 60
d_w = pre(A, "write_file", f, Content="a = 10\nb = 2\n")
expect("stale write_file dropping B's change (B idle 20 min): refused, names it", d_w, True, "b = 20  # by B")
expect("…and again on a retry", pre(A, "write_file", f, Content="a = 10\nb = 2\n"), True, "b = 20  # by B")
expect("stale write_file that keeps B's change goes through", pre(A, "write_file", f, Content="a = 10\nb = 20  # by B\n"), False)
# only the file's time changed: nothing to protect
post(A, "read_file", f)
fst = os.stat(f); os.utime(f, ns=(fst.st_atime_ns, fst.st_mtime_ns + 5_000_000))
expect("only the time stamp changed: the edit goes through", pre(A, "edit_file", f), False)
# large (>24 diff lines) external change: the diff is cut and the refusal says so; retries don't unlock it
write(f, "".join(f"line_{i} = {i}\n" for i in range(40))); post(A, "read_file", f)
write(f, "".join(f"line_{i} = {i * 10}\n" for i in range(40)))
d_l = pre(A, "edit_file", f)
expect("large external change: truncated diff, said to be cut", d_l, True, "diff обрезан")
expect("…with the count of diff lines not shown", d_l, True, "строк diff не показано")
expect("…never claimed to be the whole diff", {"decision": "deny" if "diff целиком" not in d_l.get("reason", "") else "x"}, True)
pre(A, "edit_file", f)
expect("large external change: still refused on the 3rd try", pre(A, "edit_file", f), True, "read_file")
# too big to diff (>128 KB): refused, and says nothing can be shown
huge = os.path.join(ws, "huge.py")
write(huge, "h = 1\n" + "#" * 140_000 + "\n"); post(A, "read_file", huge)
write(huge, "h = 2\n" + "#" * 140_000 + "\n")
d_h = pre(A, "edit_file", huge, Target="h = 1")
expect("too big to diff: refused, says no changes can be shown", d_h, True, "Изменения показать нельзя")
expect("…and claims no diff", {"decision": "deny" if "diff" not in d_h.get("reason", "") else "x"}, True)
# no prior snap (>64KB) + Target: contextual snippet around Target
big = os.path.join(ws, "big.py")
write(big, "".join(f"v_{i} = {i}\n" for i in range(6000)))
post(A, "read_file", big)
write(big, "".join(f"v_{i} = {i + (1 if i == 300 else 0)}\n" for i in range(6000)))
expect("no prior snap (>64KB): snippet extracts lines around Target",
       pre(A, "edit_file", big, Target="v_300 = 301"), True, "301: v_300 = 301")
# binary & secret files: no snap saved, no diff leaked, no auto-refresh
binf = os.path.join(ws, "data.bin")
with open(binf, "wb") as bf:
    bf.write(b"\x00\x01\x02hello\n")
post(A, "read_file", binf)
with open(binf, "wb") as bf:
    bf.write(b"\x00\x01\x02world\n")
with coord.Registry(ws, write=False) as regdata:
    no_bin_snap = binf not in regdata.get("snaps", {}).get(A, {})
ok &= no_bin_snap
print(f"{'✅' if no_bin_snap else '❌'} binary file with NUL bytes is skipped from snaps")
secf = os.path.join(ws, "credentials.json")
write(secf, '{"note": "initial_val"}\n'); post(A, "read_file", secf)
write(secf, '{"note": "DO_NOT_LEAK_DIFF"}\n')
d_sec = pre(A, "edit_file", secf)
sec_ok = d_sec.get("decision") == "deny" and "DO_NOT_LEAK_DIFF" not in d_sec.get("reason", "")
ok &= sec_ok
print(f"{'✅' if sec_ok else '❌'} secret file never leaks diff in stale-write")
expect("secret file is not auto-refreshed", pre(A, "edit_file", secf), True)
# snap cap at 30 newest paths
for i in range(35):
    sp = os.path.join(ws, f"cap_{i}.py")
    write(sp, f"c = {i}\n")
    post(A, "read_file", sp)
with coord.Registry(ws, write=False) as regdata:
    snap_cnt = len(regdata.get("snaps", {}).get(A, {}))
ok &= snap_cnt == coord.SNAP_KEEP
print(f"{'✅' if snap_cnt == coord.SNAP_KEEP else '❌'} snaps per agent capped at {coord.SNAP_KEEP} (got {snap_cnt})")
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
