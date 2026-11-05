"""Edit checks end to end: PostToolUse records a syntax error, PreInvocation shows it once, Stop won't let the
agent finish with it, and the fix clears it."""
import json, os, shutil, subprocess, sys, time
PLUGIN, SP = sys.argv[1], sys.argv[2]; HOOKS = os.path.join(PLUGIN, "hooks")
sys.path.insert(0, HOOKS); import conversation as conv
cid = "agp-checktest-0001"
ws = os.path.join(SP, "pipe_ws"); art = os.path.join(SP, "pipe_art")
for d in (ws, art): shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
open(os.path.join(ws, "Dockerfile"), "w").write("FROM python:3.12-slim\n")
tp = os.path.join(art, "transcript_full.jsonl")
ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
open(tp, "w").write(json.dumps({"step_index": 0, "type": "USER_INPUT", "created_at": ts, "content": "<USER_REQUEST>Почини api</USER_REQUEST>"}, ensure_ascii=False) + "\n"
                    + json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "content": "Готово."}, ensure_ascii=False) + "\n")
base = {"conversationId": cid, "transcriptPath": tp, "artifactDirectoryPath": art, "workspacePaths": [ws]}
def hook(name, extra):
    r = subprocess.run([sys.executable, os.path.join(HOOKS, name)], input=json.dumps({**base, **extra}), capture_output=True, text=True)
    if r.stderr.strip(): print("   stderr:", r.stderr.strip()[:200])
    return json.loads(r.stdout or "{}")
def post(path): return hook("check_edit.py", {"toolCall": {"name": "write_file", "args": {"Path": path}}, "error": ""})
def pre():
    out = hook("instructions.py", {"lastUserInput": "Почини api"})
    return [s["ephemeralMessage"] for s in out.get("injectSteps", []) if "edit_check" in s["ephemeralMessage"]]
def stop(n=0): return hook("stop.py", {"terminationReason": "NO_TOOL_CALL", "fullyIdle": True, "executionNum": n})
ok = True
def check(name, cond, got=""):
    global ok; ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + ("" if cond else f" → {str(got)[:200]}"))
api = os.path.join(ws, "app", "api.py"); os.makedirs(os.path.dirname(api))
open(api, "w").write("def handler(req):\n    return respond(req, {'ok': True)\n")
check("PostToolUse answers {}", post(api) == {})
m = pre(); check("PreInvocation shows the syntax error", m and os.path.join("app", "api.py") in m[0], m)
check("…only once", pre() == [])
s = stop(); check("Stop sends the agent back to fix it", s and "syntax errors" in s.get("reason", ""), s)
check("…not twice for the same state", not stop(1))
open(api, "w").write("def handler(req):\n    return respond(req, {'ok': True})\n")
post(api); m = pre(); check("the fix is reported as fixed", m and "fixed" in m[0], m)
check("Stop lets it finish after the fix", not stop())
md = os.path.join(ws, "notes.md"); open(md, "w").write("# x\n"); post(md); check("markdown has no checker", pre() == [])
nm = os.path.join(ws, "node_modules", "x.js"); os.makedirs(os.path.dirname(nm)); open(nm, "w").write("function(\n"); post(nm)
check("node_modules is skipped", pre() == [])
out = os.path.join(SP, "outside.py"); open(out, "w").write("x = (\n"); post(out); check("files outside the workspace are skipped", pre() == [])
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
