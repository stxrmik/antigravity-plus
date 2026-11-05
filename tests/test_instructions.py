"""PreInvocation: the user's messages come back on a new message, in a long turn and after compaction; a ban
(«ничего не пушь») is listed as in force."""
import json, os, subprocess, sys
PLUGIN, SP = sys.argv[1], sys.argv[2]; HOOKS = os.path.join(PLUGIN, "hooks")
sys.path.insert(0, HOOKS)
cid = "agp-selftest-0001"
tp = os.path.join(SP, "fake_transcript.jsonl")
step = 0
def add(kind, text=""):
    global step
    row = {"step_index": step, "source": "X", "type": kind, "status": "DONE"}
    if kind == "USER_INPUT": row["content"] = f"<USER_REQUEST>\n{text}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>t</ADDITIONAL_METADATA>"
    elif kind == "CHECKPOINT": row["content"] = "# Resuming from a compaction"
    with open(tp, "a", encoding="utf-8") as f: f.write(json.dumps(row, ensure_ascii=False) + "\n")
    step += 1
ok = True
def call(label, want, last=None, has=""):
    global ok
    payload = {"conversationId": cid, "transcriptPath": tp, "lastUserInput": last or ""}
    out = subprocess.run([sys.executable, os.path.join(HOOKS, "instructions.py")], input=json.dumps(payload), capture_output=True, text=True).stdout
    d = json.loads(out or "{}")
    msg = d["injectSteps"][0]["ephemeralMessage"] if d else None
    good = bool(msg) == want and (not has or has in (msg or ""))
    ok &= good
    print(f"{'✅' if good else '❌'} {label:<45} → {'reminder' if msg else '-'}")
add("USER_INPUT", "Сделай консоль VPN на FastAPI")
for _ in range(5): add("PLANNER_RESPONSE")
call("first message, 5 steps", False, "Сделай консоль VPN на FastAPI")
add("USER_INPUT", "И еще, делай все внутри Docker контейнера")
call("a second message arrives", True, "И еще, делай все внутри Docker контейнера", has="Docker")
call("same invocation again", False)
for _ in range(20): add("PLANNER_RESPONSE")
call("20 steps later", False)
for _ in range(15): add("PLANNER_RESPONSE")
call("35 steps since the message (long turn)", True)
for _ in range(10): add("PLANNER_RESPONSE")
call("10 more steps", False)
add("CHECKPOINT")
call("after context compaction", True)
add("USER_INPUT", "/usage")
call("a meta command (/usage)", False, "/usage")
add("USER_INPUT", "Самое главное, пока что ничего не пушь на сервер")
call("a ban («ничего не пушь»)", True, "Самое главное, пока что ничего не пушь на сервер", has="In force now")
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
