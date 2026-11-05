"""Full access: granted only by the user's own message, revoked on request."""
import json, os, shutil, subprocess, sys, time
PLUGIN, SP = sys.argv[1], sys.argv[2]; HOOKS = os.path.join(PLUGIN, "hooks")
sys.path.insert(0, HOOKS); import conversation as conv
ws = os.path.realpath(os.path.join(SP, "access_ws")); shutil.rmtree(ws, ignore_errors=True); os.makedirs(ws)
art = os.path.join(SP, "access_art"); shutil.rmtree(art, ignore_errors=True); os.makedirs(art)
cid = "agp-access-test-0001"; tp = os.path.join(art, "transcript_full.jsonl")
Q = "Чтобы не дёргать вас на каждом коммите — дадите мне полный доступ в этом разговоре?"
def setup(turns):
    for f in os.listdir(conv.STATE_DIR):
        if f.startswith(cid): os.remove(os.path.join(conv.STATE_DIR, f))
    rows, i = [], 0
    for who, text in turns:
        if who == "u": rows.append({"step_index": i, "type": "USER_INPUT", "created_at": "2026-11-04T10:00:00Z", "content": f"<USER_REQUEST>{text}</USER_REQUEST>"})
        else: rows.append({"step_index": i, "type": "PLANNER_RESPONSE", "content": text})
        i += 1
    open(tp, "w").write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
def run(script, extra):
    r = subprocess.run([sys.executable, os.path.join(HOOKS, script)], input=json.dumps({"conversationId": cid, "transcriptPath": tp, "workspacePaths": [ws], **extra}), capture_output=True, text=True)
    if r.stderr.strip(): print("   stderr:", r.stderr.strip()[:200])
    return json.loads(r.stdout or "{}")
def guard(cmd): return run("guard.py", {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}}).get("decision")
def pre():
    out = run("instructions.py", {})
    return " | ".join(s["ephemeralMessage"] for s in out.get("injectSteps", []))
ok = True
def check(name, got, want):
    global ok
    good = got == want; ok &= good
    print(f"{'✅' if good else '❌'} {name:<52} → {got}")
setup([("u", "Сделай фичу и закоммить")])
check("no grant: git status", guard("git status"), "allow")
setup([("u", "Сделай фичу"), ("a", Q), ("u", "да")])
msg = pre()
check("«да» to the question → ledger says access ON", "Full access is ON" in msg, True)
check("full access: git push", guard("git push origin main"), "auto_approve")
check("full access: write_file", run("guard.py", {"toolCall": {"name": "write_file", "args": {"Path": os.path.join(ws, "a.py")}}}).get("decision"), "auto_approve")
check("full access: git reset --hard still asks", guard("git reset --hard"), "force_ask")
check("full access: rm -rf ~ still asks", guard("rm -rf ~"), "force_ask")
setup([("u", "Сделай фичу, но ничего не пушь"), ("a", Q), ("u", "да")])
check("full access + «не пушь»: git push asks", guard("git push"), "force_ask")
setup([("u", "Сделай фичу"), ("a", Q), ("u", "да"), ("u", "стоп, верни вопросы")])
msg = pre()
check("revoked: ledger says revoked", "revoked" in msg, True)
check("revoked: git push back to default policy", guard("git push"), "allow")
setup([("u", "Даю полный доступ навсегда для этого проекта")])
pre()
check("project grant stored in settings", bool(conv._project_grant(ws, None)), True)
cid_saved = cid
cid = "agp-access-test-0002"; setup([("u", "Почини баг")])
check("another conversation in the project: git push", guard("git push"), "auto_approve")
r = subprocess.run([sys.executable, os.path.join(HOOKS, "session_start.py")], input=json.dumps({"conversationId": cid, "transcriptPath": tp, "workspacePaths": [ws], "lastUserInput": "Почини баг"}), capture_output=True, text=True)
check("its session context mentions the project grant", "Full access: ON for this project" in r.stdout, True)
setup([("u", "Почини баг"), ("u", "отзываю полный доступ")])
pre()
check("revoked in a conversation → project grant removed", conv._project_grant(ws, None), None)
# subagent inherits from its parent
cid = cid_saved; setup([("u", "Сделай фичу"), ("a", Q), ("u", "да")]); pre()
real_parent = conv._parent
conv._parent = lambda c: cid_saved if c == "agp-sub-0001" else ""
got = conv.full_access({"conversationId": "agp-sub-0001", "workspacePaths": [ws]}, {"users": [], "checkpoints": [], "last_step": 0})
conv._parent = real_parent
check("subagent inherits the parent's full access", bool(got) and got.get("source") == "parent", True)
for c in ("agp-access-test-0001", "agp-access-test-0002"):
    for f in os.listdir(conv.STATE_DIR):
        if f.startswith(c): os.remove(os.path.join(conv.STATE_DIR, f))
conv._project_grant(ws, False, drop=True)
shutil.rmtree(ws); shutil.rmtree(art)
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
