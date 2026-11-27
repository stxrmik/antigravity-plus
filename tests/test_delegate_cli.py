"""Offline tests for the Antigravity CLI backend (`--via cli`) of delegate.py against tests/probe/fake_agy.py:
basic run/diff/result/list, safe flags (--mode accept-edits, never --dangerously-skip-permissions), brief fidelity
(quotes, newline, Cyrillic), --via auto fallback without a language server, missing agy binary, unknown model close
matches, ask/reply conversation resume, auto-resume on error (--retries 1 vs 0), --timeout / print_timeout, denied
actions, mid-line crash, huge 20k-delta stream, send follow-up after done, cancel running task, concurrent tasks
overlap note, non-git folder diff, legacy Desktop record detection, and watch / watch --owner isolation."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

PLUGIN, SP = sys.argv[1], sys.argv[2]
ROOT = os.path.dirname(PLUGIN)
SCRIPTS = os.path.join(ROOT, "claude-code", "skills", "antigravity", "scripts")
DELEGATE = os.path.join(SCRIPTS, "delegate.py")
FAKE_AGY = os.path.join(ROOT, "tests", "probe", "fake_agy.py")
HOME = os.path.expanduser("~")
TASKS = os.path.join(HOME, ".gemini", "antigravity-plus", "delegate")
LOG_FILE = os.path.join(SP, "fake_agy.jsonl")
STATE_DIR = os.path.join(SP, "fake_agy_state")

ok = True


def check(name, cond, got=""):
    global ok
    ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + ("" if cond else f" → {str(got)[-1500:]}"))


def make_git_repo(name: str) -> str:
    ws = os.path.realpath(os.path.join(SP, name))
    os.makedirs(ws, exist_ok=True)
    with open(os.path.join(ws, "src.py"), "w", encoding="utf-8") as f:
        f.write("x = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    subprocess.run(["git", "add", "-A"], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=ws, check=True)
    return ws


def base_env(**extra) -> dict:
    env = dict(os.environ)
    for var in ("ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN", "ANTIGRAVITY_CONVERSATION_ID",
                "CLAUDE_CODE_SESSION_ID", "AGP_DELEGATE_VIA", "AGP_DELEGATE_OWNER"):
        env.pop(var, None)
    env.update(
        AGP_AGY=FAKE_AGY,
        AGP_DELEGATE_NO_LAUNCH="1",
        AGP_DELEGATE_NO_SUPERVISOR="1",
        FAKE_AGY_LOG=LOG_FILE,
        FAKE_AGY_STATE=STATE_DIR,
    )
    env.update(extra)
    return env


def run_del(args, env=None, timeout=45, cwd=None):
    r = subprocess.run(
        [sys.executable, "-X", "utf8", DELEGATE] + list(args),
        env=env or base_env(),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return r.returncode, (r.stdout or "") + (r.stderr or ""), r.stdout or "", r.stderr or ""


def read_log():
    if not os.path.isfile(LOG_FILE):
        return []
    with open(LOG_FILE, encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def extract_id(text: str) -> str:
    m = re.search(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b", text)
    return m.group(1) if m else ""


def load_record(tid: str) -> dict:
    with open(os.path.join(TASKS, tid + ".json"), encoding="utf-8") as f:
        return json.load(f)


# ── 1. Basic run --via cli --model fast, diff, result, list, flags ──
ws1 = make_git_repo("repo1")
rc, out, stdout, _ = run_del(["run", "--via", "cli", "--model", "fast", "--cwd", ws1, "Write hello.txt"])
tid1 = extract_id(out)
check("run --via cli --model fast exits 0", rc == 0, out)
check("report has ✅", "✅" in out, out)
check("report names Gemini 4 Hydrogen (Low)", "Gemini 4 Hydrogen (Low)" in out, out)
check("report lists command run", "python3 -m unittest" in out and "Commands it ran" in out, out)
check("report lists hello.txt in changed files", "A hello.txt" in out, out)
check("hello.txt was really written", os.path.isfile(os.path.join(ws1, "hello.txt")), ws1)

rc_d, out_d, _, _ = run_del(["diff", tid1, "--stat"])
check("diff <id> --stat shows A hello.txt", rc_d == 0 and "A hello.txt" in out_d, out_d)

rc_res, out_res, _, _ = run_del(["result", tid1])
check("result <id> exits 0 with report", rc_res == 0 and "✅" in out_res and "hello.txt" in out_res, out_res)

rc_ls, out_ls, _, _ = run_del(["list"])
check("list marks task as CLI", rc_ls == 0 and tid1[:8] in out_ls and "CLI" in out_ls, out_ls)

calls = read_log()
check("fake log has --mode accept-edits",
      bool(calls) and "--mode" in calls[-1]["argv"] and "accept-edits" in calls[-1]["argv"], calls)
check("fake log never has --dangerously-skip-permissions",
      all("--dangerously-skip-permissions" not in c["argv"] for c in calls), calls)

# ── 2. Brief with quotes, newline and Cyrillic arrives unchanged ──
ws2 = make_git_repo("repo2")
tricky_brief = 'Проверь "кавычки" и \'одинарные\'\nВторая строка с кириллицей: Привет!'
before_n = len(read_log())
rc, out, _, _ = run_del(["run", "--via", "cli", "--model", "fast", "--cwd", ws2, tricky_brief])
after_calls = read_log()[before_n:]
check("tricky brief exits 0", rc == 0, out)
check("brief with quotes, newline and Cyrillic arrives unchanged",
      bool(after_calls) and tricky_brief in after_calls[-1]["prompt"], after_calls)

ws2s = make_git_repo("repo2s")
before_n = len(read_log())
rc, out, _, _ = run_del(["run", "--via", "cli", "--model", "fast", "--cwd", ws2s, tricky_brief],
                        env=base_env(AGP_AGY_STDIN="1"))
after_calls = read_log()[before_n:]
check("AGP_AGY_STDIN=1: the brief goes on stdin (--input-format stream-json, no -p) and arrives unchanged",
      rc == 0 and bool(after_calls) and "--input-format" in after_calls[-1]["argv"]
      and "-p" not in after_calls[-1]["argv"] and tricky_brief in after_calls[-1]["prompt"], (out, after_calls))

# ── 3. --via auto without a language server picks the CLI ──
ws3 = make_git_repo("repo3")
before_n = len(read_log())
rc, out, _, _ = run_del(["run", "--via", "auto", "--model", "fast", "--cwd", ws3, "Auto pick test"])
after_calls = read_log()[before_n:]
check("--via auto without LS picks CLI",
      rc == 0 and "Antigravity CLI" in out and len(after_calls) == 1, out)

# ── 4. --via cli with AGP_AGY pointing to a missing file → exit 1, clear message ──
missing_agy = os.path.join(SP, "does-not-exist-agy")
rc, out, _, _ = run_del(["run", "--via", "cli", "--model", "fast", "--cwd", ws3, "Missing binary"],
                        env=base_env(AGP_AGY=missing_agy))
check("missing AGP_AGY exits 1 with clear message",
      rc == 1 and ("can't run agy" in out.lower() or "no such file" in out.lower() or "not found" in out.lower()), out)

# ── 5. Unknown model → exit 1 with close matches ──
rc, out, _, _ = run_del(["run", "--via", "cli", "--model", "gemini-4-hydrogn-low", "--cwd", ws3, "Bad model"])
check("unknown model exits 1 with close matches",
      rc == 1 and "closest" in out.lower() and "gemini-4-hydrogen-low" in out, out)

# ── 6. Question → wait exit 4 + question text, reply → new call with --conversation + answer, wait exit 0 ──
ws6 = make_git_repo("repo6")
before_n = len(read_log())
rc, out, _, _ = run_del(["run", "--via", "cli", "--model", "fast", "--no-wait", "--cwd", ws6, "SCENARIO_QUESTION"])
tid6 = extract_id(out)
check("question task started", rc == 0 and bool(tid6), out)

rc_w, out_w, _, _ = run_del(["wait", tid6, "--timeout", "15"])
check("question wait exits 4 with question text", rc_w == 4 and "Which name?" in out_w, f"rc={rc_w}\n{out_w}")

rec6 = load_record(tid6)
cid6 = rec6.get("cid") or ""
check("question task recorded conversation id", bool(cid6), rec6)

before_reply = len(read_log())
rc_rep, out_rep, _, _ = run_del(["reply", tid6, "Keep it"])
check("reply exits 0", rc_rep == 0, out_rep)

rc_w2, out_w2, _, _ = run_del(["wait", tid6, "--timeout", "15"])
reply_calls = read_log()[before_reply:]
check("reply spawned new fake call with --conversation <cid> and answer",
      bool(reply_calls)
      and "--conversation" in reply_calls[-1]["argv"]
      and cid6 in reply_calls[-1]["argv"]
      and "Keep it" in reply_calls[-1]["prompt"],
      reply_calls)
check("wait after reply exits 0", rc_w2 == 0 and "✅" in out_w2, f"rc={rc_w2}\n{out_w2}")

# ── 7. Error with --retries 1 → resumed once with --conversation, succeeds ──
ws7 = make_git_repo("repo7")
before_n = len(read_log())
rc, out, _, _ = run_del(["run", "--via", "cli", "--model", "fast", "--retries", "1", "--cwd", ws7, "SCENARIO_ERROR"])
tid7 = extract_id(out)
err_calls = read_log()[before_n:]
check("error with --retries 1 succeeds after resume", rc == 0 and "✅" in out, f"rc={rc}\n{out}")
check("error with --retries 1 made 2 calls, second with --conversation",
      len(err_calls) == 2 and "--conversation" in err_calls[1]["argv"], err_calls)

# ── 8. Error with --retries 0 → wait exit 2 with «stopped by an error» ──
ws8 = make_git_repo("repo8")
rc, out, _, _ = run_del(["run", "--via", "cli", "--model", "fast", "--retries", "0", "--no-wait",
                         "--cwd", ws8, "SCENARIO_ERROR"])
tid8 = extract_id(out)
rc_w, out_w, _, _ = run_del(["wait", tid8, "--timeout", "15"])
check("error with --retries 0 wait exits 2 with stopped by an error",
      rc_w == 2 and "stopped by an error" in out_w, f"rc={rc_w}\n{out_w}")

# ── 9. --timeout 5 with SCENARIO_SLOW → exit 2 and «time limit» ──
ws9 = make_git_repo("repo9")
rc, out, _, _ = run_del(["run", "--via", "cli", "--model", "fast", "--retries", "0", "--timeout", "5",
                         "--cwd", ws9, "SCENARIO_SLOW"], timeout=30)
tid9 = extract_id(out)
check("SCENARIO_SLOW with --timeout 5 exits 2 and mentions time limit",
      rc == 2 and "time limit" in out.lower(), f"rc={rc}\n{out}")
if rc == 3 and tid9:
    rc9w, out9w, _, _ = run_del(["wait", tid9, "--timeout", "15"])
    check("SCENARIO_SLOW subsequent wait exits 2 and mentions time limit",
          rc9w == 2 and "time limit" in out9w.lower(), f"rc={rc9w}\n{out9w}")

# ── 10. Denied → exit 2 and denied action listed ──
ws10 = make_git_repo("repo10")
rc, out, _, _ = run_del(["run", "--via", "cli", "--model", "fast", "--retries", "0", "--cwd", ws10, "SCENARIO_DENIED"])
check("SCENARIO_DENIED exits 2 and lists denied action",
      rc == 2 and ("guard says no" in out or "Bash" in out) and "Not allowed" in out, f"rc={rc}\n{out}")

# ── 11. Crash → exit 2, «ended without a result» and boom ──
ws11 = make_git_repo("repo11")
rc, out, _, _ = run_del(["run", "--via", "cli", "--model", "fast", "--retries", "0", "--cwd", ws11, "SCENARIO_CRASH"])
check("SCENARIO_CRASH exits 2 with ended without a result and boom",
      rc == 2 and "ended without a result" in out and "boom" in out, f"rc={rc}\n{out}")

# ── 12. Huge (20000 text_delta events) → finishes, report fine ──
ws12 = make_git_repo("repo12")
rc, out, _, _ = run_del(["run", "--via", "cli", "--model", "fast", "--retries", "0", "--cwd", ws12, "SCENARIO_HUGE"])
check("SCENARIO_HUGE finishes with exit 0 and clean report",
      rc == 0 and "✅" in out and "hello.txt" in out, f"rc={rc}\n{out}")

# ── 13. send <id> "more" after done → new turn with --conversation ──
cid1 = load_record(tid1).get("cid") or ""
before_send = len(read_log())
rc_s, out_s, _, _ = run_del(["send", tid1, "more"])
send_calls = read_log()[before_send:]
check("send after done exits 0 with new turn using --conversation",
      rc_s == 0
      and bool(send_calls)
      and "--conversation" in send_calls[-1]["argv"]
      and cid1 in send_calls[-1]["argv"]
      and "more" in send_calls[-1]["prompt"],
      f"rc={rc_s}\ncalls={send_calls}\n{out_s}")

# ── 14. cancel on a SCENARIO_SLEEP=30 task → stopped, record canceled ──
ws14 = make_git_repo("repo14")
rc, out, _, _ = run_del(["run", "--via", "cli", "--model", "fast", "--no-wait",
                         "--cwd", ws14, "SCENARIO_SLEEP=30"])
tid14 = extract_id(out)
pid_file = os.path.join(TASKS, tid14 + ".cli", "turn-0.pid")
deadline = time.time() + 10.0
while time.time() < deadline and not os.path.exists(pid_file):
    time.sleep(0.1)
rc_c, out_c, _, _ = run_del(["cancel", tid14])
rec14 = load_record(tid14)
check("cancel stops running SCENARIO_SLEEP=30 task",
      rc_c == 0 and "Stopped Antigravity CLI task" in out_c, f"rc={rc_c}\n{out_c}")
check("canceled task record has state=canceled", rec14.get("state") == "canceled", rec14)

# ── 15. Two tasks in the same folder at once → each diff lists its own file + overlap note ──
ws15 = make_git_repo("repo15")
rc_a, out_a, _, _ = run_del(["run", "--via", "cli", "--model", "fast", "--no-wait",
                             "--cwd", ws15, "SCENARIO_SLEEP=2 SCENARIO_FILE=file_a.txt"])
tid_a = extract_id(out_a)
rc_b, out_b, _, _ = run_del(["run", "--via", "cli", "--model", "fast", "--no-wait",
                             "--cwd", ws15, "SCENARIO_SLEEP=2 SCENARIO_FILE=file_b.txt"])
tid_b = extract_id(out_b)
rc_wa, out_wa, _, _ = run_del(["wait", tid_a, "--timeout", "20"])
rc_wb, out_wb, _, _ = run_del(["wait", tid_b, "--timeout", "20"])
rc_da, out_da, _, _ = run_del(["diff", tid_a, "--stat"])
rc_db, out_db, _, _ = run_del(["diff", tid_b, "--stat"])
check("concurrent tasks both finish", rc_wa == 0 and rc_wb == 0, f"A={rc_wa} B={rc_wb}")
check("concurrent diff A lists file_a.txt and overlap note",
      "file_a.txt" in out_da and "other tasks in this folder changed meanwhile" in out_da, out_da)
check("concurrent diff B lists file_b.txt and overlap note",
      "file_b.txt" in out_db and "other tasks in this folder changed meanwhile" in out_db, out_db)

# ── 16. Non-git folder → diff lists new file with not-a-git-folder note ──
ws16 = os.path.realpath(os.path.join(SP, "nongit"))
os.makedirs(ws16, exist_ok=True)
with open(os.path.join(ws16, "existing.txt"), "w", encoding="utf-8") as f:
    f.write("before\n")
rc16, out16, _, _ = run_del(["run", "--via", "cli", "--model", "fast", "--cwd", ws16, "Non-git task"])
tid16 = extract_id(out16)
rc_d16, out_d16, _, _ = run_del(["diff", tid16, "--stat"])
check("non-git task finishes", rc16 == 0 and bool(tid16), out16)
check("non-git diff lists hello.txt with not-a-git-folder note",
      rc_d16 == 0 and "A hello.txt" in out_d16 and "not a git folder" in out_d16, out_d16)

# ── 17. Old-format record (no via) is treated as Desktop ──
sys.path.insert(0, SCRIPTS)
import delegate  # noqa: E402
check("legacy record without via is not CLI (delegate.agy_cli.is_cli({}) is False)",
      delegate.agy_cli.is_cli({}) is False and delegate.agy_cli.is_cli({"id": "old"}) is False,
      delegate.agy_cli.is_cli({}))

# ── 18. watch <id> prints one ✅ end line for a CLI task ──
ws18 = make_git_repo("repo18")
rc18, out18, _, _ = run_del(["run", "--via", "cli", "--model", "fast", "--no-wait",
                             "--cwd", ws18, "SCENARIO_SLEEP=1"])
tid18 = extract_id(out18)
rc_wt, out_wt, stdout_wt, _ = run_del(["watch", tid18, "--timeout", "15"])
ok_lines = [ln for ln in stdout_wt.splitlines() if "✅" in ln]
check("watch <id> prints one ✅ end line for a CLI task",
      rc_wt == 0 and len(ok_lines) == 1 and tid18[:8] in ok_lines[0], f"rc={rc_wt}\n{out_wt}")

# ── 19. watch --owner a doesn't take a task run with --owner b ──
ws19 = make_git_repo("repo19")
rc19, out19, _, _ = run_del(["run", "--via", "cli", "--model", "fast", "--no-wait",
                             "--owner", "b", "--cwd", ws19, "SCENARIO_SLEEP=2"], cwd=ws19)
tid19 = extract_id(out19)
rc_wa_own, out_wa_own, stdout_wa_own, _ = run_del(["watch", "--owner", "a", "--timeout", "5"], cwd=ws19)
check("watch --owner a does not take --owner b task",
      rc_wa_own == 0 and tid19[:8] not in stdout_wa_own and "No running Antigravity tasks" in stdout_wa_own,
      f"rc={rc_wa_own}\n{out_wa_own}")
rc_wb_own, out_wb_own, stdout_wb_own, _ = run_del(["watch", "--owner", "b", "--timeout", "15"], cwd=ws19)
check("watch --owner b takes its own task",
      rc_wb_own == 0 and "✅" in stdout_wb_own and tid19[:8] in stdout_wb_own,
      f"rc={rc_wb_own}\n{out_wb_own}")

# the report of a turn: a repeated report once, a short note after the Stop check under the full report, a single
# report as before — Desktop's steps and the CLI's events alike
REPORT = "✅ Empty passwords are rejected now; all 14 tests pass.\n\n- `app/forms.py` — a check in `clean()`\n- `pytest -q` → 14 passed"
AGAIN = "✅ Empty passwords are rejected now; all 14 tests pass.\n\n- `app/forms.py` — a check in clean()\n- `pytest -q` → 14 passed."
NOTE = "Checked again: the 14 tests ran after the last edit."
HOOK_TEXT = "Antigravity Plus: before you finish, check the numbers in your report"
pstep = lambda text: {"type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE", "plannerResponse": {"response": text}}
sysstep = {"type": "CORTEX_STEP_TYPE_SYSTEM_MESSAGE", "systemMessage": {"content": HOOK_TEXT}}
user = {"type": "CORTEX_STEP_TYPE_USER_INPUT"}
got = delegate.assemble_report(delegate.turn_messages([user, pstep("Looking at forms.py"), pstep(REPORT), sysstep,
                                                       pstep(AGAIN)], 0))
check("report: a near-repeat after the Stop check → only the later copy, no hook text", got == AGAIN, got)
got = delegate.assemble_report(delegate.turn_messages([user, pstep(REPORT), sysstep, pstep(NOTE)], 0))
check("report: a short note after the Stop check → the full report, the note below it",
      got.startswith(REPORT) and got.endswith(delegate.CLARIFIED + NOTE) and HOOK_TEXT not in got, got)
got = delegate.assemble_report(delegate.turn_messages([pstep("old turn's report"), user, pstep("Working…"),
                                                       pstep(REPORT)], 0))
check("report: a single report as before; what came before the last message to the agent stays out", got == REPORT, got)
got = delegate.assemble_report(delegate.turn_messages([user, pstep(REPORT), pstep(NOTE)], 0))
check("report: a later message with no hook between is the report (as before)", got == NOTE, got)
ev = lambda k, **b: json.dumps({"event": k, k: b})
fake_task = {"id": "rep-cli-0000", "turns": [{"n": 0, "at": 0}], "turn": 0}
os.makedirs(delegate.agy_cli.folder(fake_task), exist_ok=True)
with open(delegate.agy_cli.turn_path(fake_task, 0, "ndjson"), "w", encoding="utf-8") as f:
    f.write("\n".join([ev("init"), ev("step_update", step_index=0, state="DONE", step_type="user_input"),
                       ev("step_update", step_index=2, state="ACTIVE", step_type="agent_response", text_delta=REPORT[:20]),
                       ev("step_update", step_index=2, state="DONE", step_type="agent_response", text_delta=REPORT[20:]),
                       ev("step_update", step_index=3, state="DONE", step_type="system_message"),
                       ev("step_update", step_index=4, state="DONE", step_type="agent_response", text_delta=NOTE),
                       ev("result", status="SUCCESS", response=NOTE)]) + "\n")
open(delegate.agy_cli.turn_path(fake_task, 0, "exit"), "w").write('{"code": 0, "reason": ""}')
got = delegate.agy_cli.report_text(fake_task)
check("report (CLI events): the full report with the note after the Stop check, not the note alone",
      got.startswith(REPORT) and got.endswith(delegate.CLARIFIED + NOTE), got)

sys.exit(0 if ok else 1)
