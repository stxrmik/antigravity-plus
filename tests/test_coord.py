"""Coordination: a write over a version the agent hasn't seen is refused; the registry stays small (the text the
agents saw lies beside it); agents get real titles; <other_agents> comes only when the picture changes."""
import json, os, shutil, sqlite3, subprocess, sys, time
PLUGIN, SP = sys.argv[1], sys.argv[2]; HOOKS = os.path.join(PLUGIN, "hooks")
sys.path.insert(0, HOOKS); import coord, conversation as conv
ws = os.path.realpath(os.path.join(SP, "coord_ws")); shutil.rmtree(ws, ignore_errors=True); os.makedirs(ws)
reg = os.path.join(coord.COORD_DIR, coord.slug(ws) + ".json")
for p in (reg, reg + ".lock"):
    if os.path.exists(p): os.remove(p)
shutil.rmtree(coord._snap_root(ws), ignore_errors=True)
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
no_bin_snap = not os.path.exists(coord._snap_file(ws, A, os.path.realpath(binf)))
ok &= no_bin_snap
print(f"{'✅' if no_bin_snap else '❌'} binary file with NUL bytes is skipped from snaps")
secf = os.path.join(ws, "credentials.json")
write(secf, '{"note": "initial_val"}\n'); post(A, "read_file", secf)
no_sec_snap = not os.path.exists(coord._snap_file(ws, A, os.path.realpath(secf)))
ok &= no_sec_snap
print(f"{'✅' if no_sec_snap else '❌'} a secret file's text is never kept as a snapshot")
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
snap_cnt = len([n for n in os.listdir(coord._snap_dir(ws, A)) if n.endswith(".json")])
cap_snap = lambda i: coord._snap_file(ws, A, os.path.realpath(os.path.join(ws, f"cap_{i}.py")))
newest_kept = os.path.exists(cap_snap(34)) and os.path.exists(cap_snap(5)) and not os.path.exists(cap_snap(4))
good = snap_cnt == coord.SNAP_KEEP and newest_kept
ok &= good
print(f"{'✅' if good else '❌'} snaps per agent capped at {coord.SNAP_KEEP}, the newest kept (got {snap_cnt}, newest kept: {newest_kept})")
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
def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + (f" — {str(detail)[:300]}" if not cond and detail != "" else ""))

# ── the text an agent saw lies beside the registry (every hook call of every agent rewrites the registry) ──
L = "agp-coord-agent-L1"
large = []
for i in range(40):  # 40 reads of ~54 KB files: 30 snapshots kept, ~1.6 MB that 0.25.0 kept inside the registry
    p = os.path.join(ws, f"large_{i}.py")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("".join(f"large_{i}_line_{n:04d} = '{'x' * 30}'\n" for n in range(1000)))
    large.append(p)
    coord.record({"conversationId": L, "workspacePaths": [ws]}, "read_file", p)  # = check_edit.py after read_file
with open(reg, encoding="utf-8") as fh:
    reg_text = fh.read()
l_snaps = [n for n in os.listdir(coord._snap_dir(ws, L)) if n.endswith(".json")]
check(f"40 reads of large files: the registry stays small ({len(reg_text.encode('utf-8'))} bytes, no file text in it), "
      f"{len(l_snaps)} snapshot files beside it",
      len(reg_text.encode("utf-8")) < 32_000 and "large_39_line_0000" not in reg_text and '"snaps"' not in reg_text
      and len(l_snaps) == coord.SNAP_KEEP)
with open(large[-1], encoding="utf-8") as fh:
    body = fh.read()
write(large[-1], body.replace("large_39_line_0500 = ", "large_39_line_0500 = 'by B' or ", 1)); post(B, "edit_file", large[-1])
d_s = pre(L, "edit_file", large[-1])
expect("stale edit of a large file: refused with the diff from its snapshot", d_s, True, "+large_39_line_0500 = 'by B'")
expect("…the whole diff", d_s, True, "diff целиком")
# a registry 0.25.0 left with the snapshots inside loses them on its next write (they are a cache)
with open(reg, encoding="utf-8") as fh:
    legacy = json.load(fh)
legacy["snaps"] = {L: {os.path.realpath(large[0]): "legacy = 1\n" * 20_000}}
with open(reg, "w", encoding="utf-8") as fh:
    json.dump(legacy, fh)
before = os.path.getsize(reg)
coord.heartbeat({"conversationId": L, "workspacePaths": [ws]})
with open(reg, encoding="utf-8") as fh:
    after = json.load(fh)
check(f"a legacy registry with snapshots inside ({before} bytes) drops them on the next write ({os.path.getsize(reg)} bytes)",
      before > 200_000 and "snaps" not in after and os.path.getsize(reg) < 32_000 and L in after["agents"])
# agents the registry forgets (idle for a day) take their snapshots along; folders no registry keeps go too
F = "agp-coord-agent-F1"
coord.record({"conversationId": F, "workspacePaths": [ws]}, "read_file", f)
f_dir = coord._snap_dir(ws, F)
had_f = os.path.isdir(f_dir) and len(os.listdir(f_dir)) == 1
day_ago = time.time() - coord.KEEP - 3600
gone_root = os.path.join(coord.SNAP_DIR, "Users-someone-deleted-project")  # a workspace whose registry was deleted
orphans = {"old": coord._snap_dir(ws, "agp-coord-orphan-01"), "new": coord._snap_dir(ws, "agp-coord-orphan-02"),
           "gone": os.path.join(gone_root, "agp-coord-agent-G1")}
for d in orphans.values():
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "0123456789abcdef.json"), "w", encoding="utf-8") as fh:
        fh.write("{}")
for d in (orphans["old"], orphans["gone"], coord._snap_dir(ws, A)):  # A still works here, it only read nothing new
    os.utime(d, (day_ago, day_ago))
with coord.Registry(ws) as data:  # F's last activity a day ago: this very write forgets it
    data["agents"][F]["last"] = day_ago
with coord.Registry(ws, write=False) as data:
    f_known = F in data["agents"] or F in data["seen"]
check("a forgotten agent's snapshots are deleted with it", had_f and not f_known and not os.path.exists(f_dir))
check("…orphaned snapshot folders older than a day go (here and of a deleted registry), fresh ones and those of "
      "active agents stay", not os.path.exists(orphans["old"]) and not os.path.exists(gone_root)
      and os.path.isdir(orphans["new"]) and os.path.isdir(coord._snap_dir(ws, A)) and os.path.isdir(coord._snap_dir(ws, L)))

# ── titles: provisional until Antigravity's index names the conversation ──
D, E = "agp-coord-agent-D1", "agp-coord-agent-E1"
brief = (conv.DELEGATED_MARK + " This task comes from Claude Code, another AI coding agent that works on this project "
         "for the same user, at the same time as you.\n\nRules for this run:\n1. Do exactly the task below.\n\n"
         "Working folder: /tmp/example-app\n\nTask:\n## Fix the list animation\nThe list jumps when an item is removed.")
conv.store(D, {"transcript": "", "offset": 0, "users": [{"step": 0, "ts": "", "text": brief}], "checkpoints": [],
               "last_step": 0, "injected": {}})
os.makedirs(os.path.dirname(coord.INDEX_DB), exist_ok=True)
def index(sql, *args):
    con = sqlite3.connect(coord.INDEX_DB)
    con.execute(sql, args)
    con.commit()
    con.close()
index("CREATE TABLE conversation_summaries (conversation_id TEXT, title TEXT, preview TEXT, parent_conversation_id TEXT)")
index("INSERT INTO conversation_summaries VALUES (?, '', ?, '')", D, brief[:120])  # no title yet; the preview: the rules
with coord.Registry(ws) as data:
    t1 = dict(coord._agent(data, D))
check("a task from Claude Code the index hasn't titled: named by the task's first line, not the run rules",
      t1.get("title") == "Claude Code: Fix the list animation" and t1.get("title_from") == "request", t1)
index("UPDATE conversation_summaries SET title = ? WHERE conversation_id = ?", "Claude Code: List animation fixes", D)
with coord.Registry(ws) as data:
    t2 = dict(coord._agent(data, D))  # the index was asked a moment ago: not again yet
    data["agents"][D]["looked"] -= coord.LOOK_EVERY + 1
    t3 = dict(coord._agent(data, D))
check("…the provisional title gives way to the index's title (asked again at most once a minute)",
      t2.get("title") == t1.get("title") and t3.get("title") == "Claude Code: List animation fixes"
      and t3.get("title_from") == "index", (t2, t3))
with coord.Registry(ws) as data:  # an entry from an earlier build: the start of the run rules as its title
    data["agents"][E] = {"title": conv.DELEGATED_MARK + " This task comes from Claude Cod…", "parent": "",
                         "last": time.time(), "looked": 0, "files": {}}
    t4 = dict(coord._agent(data, E))
check("…an earlier build's title made of the run rules is replaced", t4.get("title") == "Claude Code: task"
      and t4.get("title_from") == "request", t4)
check("…a person's first request: its start, whitespace folded",
      coord.request_title("Add  a dark mode\ntoggle") == "Add a dark mode toggle"
      and coord.request_title("word " * 20) == ("word " * 12).strip() + "…", coord.request_title("word " * 20))

# ── <other_agents>: every block stays in the conversation for good, so one comes only when the picture changes ──
ws2 = os.path.realpath(os.path.join(SP, "coord_ws2")); shutil.rmtree(ws2, ignore_errors=True); os.makedirs(ws2)
M, P, Q = "agp-coord-agent-M1", "agp-coord-agent-P1", "agp-coord-agent-Q1"
def edit2(cid, name):
    p = os.path.join(ws2, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(f"# {name}\n")
    coord.record({"conversationId": cid, "workspacePaths": [ws2]}, "edit_file", p)  # = check_edit.py after edit_file
def msg2(st):
    return coord.others_message({"conversationId": M, "workspacePaths": [ws2]}, st)
def idle2(cid):
    with coord.Registry(ws2) as data:
        data["agents"][cid]["last"] -= 11 * 60
st3 = {"users": [], "checkpoints": [], "last_step": 10}
edit2(P, "p1.py"); time.sleep(0.05)
first = msg2(st3)
time.sleep(0.05)
quiet = []
for i in range(coord.OTHERS_EVERY - 1):  # P keeps changing files on every call of M
    edit2(P, f"churn_{i}.py"); quiet.append(msg2(st3))
edit2(P, f"churn_{coord.OTHERS_EVERY - 1}.py"); time.sleep(0.05)  # (time.time() ticks 15.6 ms on Windows' 3.9)
twentieth = msg2(st3)
check("the first block: the full list with the rules", bool(first) and "right now" in first and P in first
      and "p1.py" in first and "Commit only your own files" in first, first)
check(f"file churn: no block for {coord.OTHERS_EVERY - 1} calls", all(q is None for q in quiet), [q for q in quiet if q])
check(f"…on the {coord.OTHERS_EVERY}th call one with only the files changed since the last block",
      bool(twentieth) and f"churn_{coord.OTHERS_EVERY - 1}.py" in twentieth and "p1.py" not in twentieth
      and "Commit only" not in twentieth, twentieth)
edit2(Q, "q1.py"); time.sleep(0.05)
joined = msg2(st3)
check("a new agent: a block at once, about it alone (nothing else changed)",
      bool(joined) and Q in joined and "q1.py" in joined and "started working here" in joined and P not in joined, joined)
check("…then quiet again", msg2(st3) is None)
idle2(P)
left = msg2(st3)
check("an agent leaving: a short block at once — who stopped, who still works here",
      bool(left) and P in left and "stopped" in left and f"Still working here: «без названия» (conversation {Q})" in left
      and len(left.splitlines()) <= 5, left)
idle2(Q)
check("the last one leaving: no block (their changes stay in the tree; the guard still guards every file)",
      msg2(st3) is None and st3.get("others_shown") == {}, st3.get("others_shown"))
edit2(P, "p2.py")
back = msg2(st3)
check("someone working here again after that: the full list", bool(back) and "right now" in back and "p2.py" in back, back)
st3["checkpoints"], st3["last_step"] = [12], 13
check("a context compaction since the last block: the full list again, once",
      (msg2(st3) or "").count("right now") == 1 and msg2(st3) is None)
check("the conversation's calls are counted", st3.get("others_calls") == coord.OTHERS_EVERY + 8, st3.get("others_calls"))
st4 = {"users": [], "checkpoints": [], "last_step": 20, "others_key": f"{P}:p2.py"}
check("an earlier build's key naming the same agents: no repeat", msg2(st4) is None and "others_key" not in st4
      and set(st4.get("others_shown") or {}) == {P}, st4)

shutil.rmtree(ws); [os.remove(p) for p in (reg, reg + ".lock") if os.path.exists(p)]
reg2 = os.path.join(coord.COORD_DIR, coord.slug(ws2) + ".json")
shutil.rmtree(ws2); [os.remove(p) for p in (reg2, reg2 + ".lock") if os.path.exists(p)]
for d in (coord._snap_root(ws), coord._snap_root(ws2)):
    shutil.rmtree(d, ignore_errors=True)
if os.path.exists(coord.INDEX_DB): os.remove(coord.INDEX_DB)
for c in (A, B, S, L, F, D, E, M, P, Q):
    for suffix in (".json", ".checks.json", ".checks.json.lock"):
        p = os.path.join(conv.STATE_DIR, c + suffix)
        if os.path.exists(p): os.remove(p)
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
