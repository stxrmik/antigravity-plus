"""Stop hook: project setup (/git-init, /init) and announce-and-stop answers."""
import json, os, shutil, subprocess, sys
PLUGIN, SP = sys.argv[1], sys.argv[2]; HOOKS = os.path.join(PLUGIN, "hooks")
sys.path.insert(0, HOOKS); import conversation as conv, session_start  # noqa: E402
cid = "agp-setuptest-0001"
art = os.path.join(SP, "su_art"); tp = os.path.join(art, "transcript_full.jsonl")
def clear_state():
    for f in os.listdir(conv.STATE_DIR):
        if f.startswith(cid): os.remove(os.path.join(conv.STATE_DIR, f))
def setup(ws, steps, answer="Готово.", fresh=True, files=None, repo=False):
    if fresh:
        for d in (ws, art): shutil.rmtree(d, ignore_errors=True)
        os.makedirs(ws, exist_ok=True); os.makedirs(art, exist_ok=True); clear_state()
        for rel, text in (files or {}).items(): open(os.path.join(ws, rel), "w").write(text)
        if repo: subprocess.run(["git", "init", "-q"], cwd=ws)
    rows = [{"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-04T10:00:00Z", "content": "<USER_REQUEST>Сделай фичу</USER_REQUEST>"}]
    for i, (name, args) in enumerate(steps, 1):
        rows.append({"step_index": i, "type": "PLANNER_RESPONSE", "content": "", "tool_calls": [{"name": name, "args": args}]})
    rows.append({"step_index": len(rows), "type": "PLANNER_RESPONSE", "content": answer})
    open(tp, "w").write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
def stop(ws):
    p = {"conversationId": cid, "transcriptPath": tp, "artifactDirectoryPath": art, "workspacePaths": [ws], "terminationReason": "NO_TOOL_CALL", "fullyIdle": True, "executionNum": 0}
    r = subprocess.run([sys.executable, os.path.join(HOOKS, "stop.py")], input=json.dumps(p), capture_output=True, text=True)
    if r.stderr.strip(): print("   stderr:", r.stderr.strip()[:300])
    d = json.loads(r.stdout or "{}"); return d.get("reason") if d.get("decision") == "continue" else None
ws = os.path.join(SP, "su_ws")
w = lambda n: ("write_file", {"Path": os.path.join(ws, n), "Content": "x=1\n"})
cmd = lambda c: ("run_command", {"CommandLine": c})
ok = True
def check(name, got, want_git, want_init, want_offer=False, silent=False):
    global ok
    if silent: good = got is None
    else:
        good = bool(got) and ("create the git repository" in (got or "")) == want_git and ("run /init" in (got or "")) == want_init \
               and (("you offered" in (got or "")) == want_offer)
    ok &= good
    print(f"{'✅' if good else '❌'} {name:<52} → {(got or 'stop').replace(chr(10), ' ')[:150]}")
setup(ws, [w("a.py"), cmd("pytest -q")]); check("code changed, no git, no rules", stop(ws), True, True)
got = stop(ws); check("second stop: no repeat", got, False, False, silent=True)
setup(ws, [w("a.py"), cmd("pytest -q"), cmd(f'python3 "/x/skills/git-init/scripts/repo_init.py" --path "{ws}"'),
           ("read_file", {"Path": "/x/skills/init/SKILL.md"})]); check("agent did both itself", stop(ws), False, False, silent=True)
setup(ws, [], "Ответил на вопрос. При желании можно один раз запустить `/init`, чтобы сохранить команды.")
check("no changes, but offered /init", stop(ws), True, True, want_offer=True)
setup(ws, [w("a.py"), cmd("pytest -q")], "Готово. Хотите, чтобы я создал AGENTS.md и git-репозиторий?")
check("asked «Хотите, чтобы я создал AGENTS.md…?»", stop(ws), True, True, want_offer=True)
setup(ws, [w("a.py"), cmd("pytest -q")], "Готово. Какой цвет кнопки выбрать — синий или зелёный?")
check("genuine question to the user", stop(ws), False, False, silent=True)
setup(ws, [w("a.py"), cmd("pytest -q")], files={"AGENTS.md": "# rules\n"}, repo=True)
check("repo with AGENTS.md: nothing to set up", stop(ws), False, False, silent=True)
setup(ws, [w("a.py"), cmd("pytest -q")], files={"CLAUDE.md": "# rules\n"})
check("CLAUDE.md counts as rules: only git", stop(ws), True, False)
setup(ws, [], "Ответил на вопрос.")
check("no changes, no offer", stop(ws), False, False, silent=True)
dl = os.path.expanduser("~/Downloads")
os.makedirs(dl, exist_ok=True)
setup(ws, [("write_file", {"Path": os.path.join(dl, "x.py"), "Content": "1"}), cmd("pytest")])
check("workspace ~/Downloads: never", stop(dl), False, False, silent=True)
setup(ws, [w("a.py")])
got = stop(ws); good = bool(got) and "ran no check" in got and "Set up the project" in got; ok &= good
print(f"{'✅' if good else '❌'} {'verify + setup in one message':<52} → {(got or 'stop').replace(chr(10), ' ')[:150]}")
def ann(name, answer, steps, want):
    global ok
    setup(ws, steps, answer, files={"AGENTS.md": "# r\n"}, repo=True)
    got = stop(ws); good = (bool(got) and "ended your turn without doing it" in got) == want; ok &= good
    print(f"{'✅' if good else '❌'} {name:<52} → {(got or 'stop').replace(chr(10), ' ')[:150]}")
ann("announced /git-init and stopped (live failure)", "Инициализирую репозиторий Git для проекта с помощью `/git-init`.", [], True)
ann("«Сейчас запущу тесты.» after writing code", "Сейчас запущу тесты.", [w("a.py")], True)
ann("English «Let me run the tests.»", "Let me run the tests.", [], True)
ann("a real final report (long)", "Сделал утилиту wordcount.py: считает слова в файле, путь — аргумент. " * 6, [w("a.py"), cmd("pytest")], False)
ann("short done message", "Готово: wordcount.py и тест, тест проходит.", [w("a.py"), cmd("pytest")], False)
ann("announce with a question", "Запускаю тесты? Или сначала посмотрите код?", [], False)
DIAGNOSIS = ("Дело не в округлении float. В [calc.py](file:///x/proj/calc.py#L7) скидка вычитается напрямую как абсолютное "
             "число (`subtotal - discount`, то есть `300 - 10 = 290`), а тест ожидает вычитание процента (`300 - 10% = 270`), "
             "поэтому `test_discount` и падает на сравнении с 270.\n\n")
ann("a diagnosis that ends «Исправляем…» (live failure)", DIAGNOSIS + "Исправляем расчёт скидки как процента в "
    "[calc.py](file:///x/proj/calc.py).", [], True)
ann("…«Сейчас внесу правку и перезапущу тест.»", DIAGNOSIS + "Сейчас внесу правку и перезапущу тест.", [], True)
ann("…«I'll fix the discount now.»", DIAGNOSIS + "I'll fix the discount calculation now.", [], True)
ann("a diagnosis that ends with a question", DIAGNOSIS + "Исправить расчёт скидки как процента в calc.py?", [], False)
ann("a long report that ends «Теперь всё работает.»", DIAGNOSIS + "Исправил расчёт, тест проходит. Теперь всё работает.",
    [w("a.py"), cmd("pytest")], False)
ann("a long report that ends in the past tense", DIAGNOSIS + "Исправил расчёт скидки в calc.py и перезапустил тест: 2 passed.",
    [w("a.py"), cmd("pytest")], False)

# session_start.project_files(): a rules file Antigravity may leave out of its inline rules gets ONE summary line (size,
# limits, the read command) — never inlined: Antigravity itself tells the model which files it excluded
import importlib.util  # noqa: E402
small_ws = os.path.realpath(os.path.join(SP, "su_small_agents"))
big_ws = os.path.realpath(os.path.join(SP, "su_big_agents"))
mid_ws = os.path.realpath(os.path.join(SP, "su_mid_agents"))
cc_ws = os.path.realpath(os.path.join(SP, "su_cc_agents"))
for d in (small_ws, big_ws, mid_ws, cc_ws):
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
def put(d, name, text):
    with open(os.path.join(d, name), "w", encoding="utf-8") as f:
        f.write(text)
put(small_ws, "AGENTS.md", "# Rules\nUNIQUE_SMALL_MARKER\n")
put(big_ws, "AGENTS.md", "# Rules\nUNIQUE_BIG_RULE_MARKER\n" + ("- keep commands compact and clear.\n" * 800))
put(mid_ws, "AGENTS.md", "# Rules\nUNIQUE_MID_MARKER\n" + ("- keep commands compact and clear.\n" * 300))  # ~10 KB: fits on its own
put(cc_ws, "AGENTS.md", "# Rules\nUNIQUE_CC_RULE\n")
put(cc_ws, "CLAUDE.md", "# Claude rules\nUNIQUE_CLAUDE_ONLY_MARKER\n")
s_sum, s_br = session_start.project_files(small_ws, small_ws)
b_sum, b_br = session_start.project_files(big_ws, big_ws)
s_sum_txt, b_sum_txt = "\n".join(s_sum), "\n".join(b_sum)
big_path = os.path.join(big_ws, "AGENTS.md")
big_size = os.path.getsize(big_path)
small_ok = s_sum_txt == "Project rules: `AGENTS.md` (loaded by Antigravity)." and not s_br
big_one = len(b_sum) == 1 and b_sum[0].startswith("Project rules: `AGENTS.md`")
big_facts = (f"{big_size:,} bytes" in b_sum_txt and "24,000" in b_sum_txt and "20,000" in b_sum_txt and "~12.8k" in b_sum_txt
             and f"`read_file {big_path}`" in b_sum_txt and "leave it out" in b_sum_txt)
big_not_inlined = not b_br and "UNIQUE_BIG_RULE_MARKER" not in b_sum_txt
good_big = small_ok and big_one and big_facts and big_not_inlined
ok &= good_big
print(f"{'✅' if good_big else '❌'} {'AGENTS.md > 24000B: one summary line, not inlined':<52} → small={small_ok}, one={big_one}, facts={big_facts}, not_inlined={big_not_inlined}: {b_sum_txt[:200]}")

# the token budget is shared with the rule files Antigravity loads besides the project's (a global AGENTS.md, plugin rules)
glob = os.path.expanduser("~/.gemini/config/AGENTS.md")
os.makedirs(os.path.dirname(glob), exist_ok=True)
had_glob = os.path.isfile(glob)
if not had_glob:
    put(os.path.dirname(glob), "AGENTS.md", "- global rule that keeps the shared token budget busy.\n" * 700)
m_sum, m_br = session_start.project_files(mid_ws, mid_ws)
mid_txt = "\n".join(m_sum)
budget_line = ("20,000-token budget" in mid_txt and "estimated total" in mid_txt and not m_br
               and f"`read_file {os.path.join(mid_ws, 'AGENTS.md')}`" in mid_txt and "UNIQUE_MID_MARKER" not in mid_txt)
ok &= budget_line
print(f"{'✅' if budget_line else '❌'} {'small AGENTS.md, shared token budget exceeded: note':<52} → {mid_txt[:160]}")
# the estimate and both limits are doctor.py's (same files, same wrapper, same tokens)
dspec = importlib.util.spec_from_file_location("agp_doctor_for_test", os.path.join(PLUGIN, "skills", "doctor", "scripts", "doctor.py"))
if os.path.isfile(dspec.origin):
    doctor = importlib.util.module_from_spec(dspec); dspec.loader.exec_module(doctor)
    same = (session_start.RULES_TOKEN_BUDGET == doctor.RULES_TOKEN_BUDGET == 20000 and session_start.RULE_FILE_LIMIT == doctor.RULE_FILE_LIMIT == 24000
            and all(session_start._combined_rule_tokens(d) == doctor._combined_rule_tokens(d) for d in (small_ws, big_ws, mid_ws)))
    ok &= same
    print(f"{'✅' if same else '❌'} {'token estimate and limits equal doctor.py':<52} → {session_start._combined_rule_tokens(mid_ws)} vs {doctor._combined_rule_tokens(mid_ws)}")
if not had_glob:
    os.remove(glob)
m2_sum, _ = session_start.project_files(mid_ws, mid_ws)
fits = "\n".join(m2_sum) == "Project rules: `AGENTS.md` (loaded by Antigravity)." or "estimated total" in "\n".join(m2_sum)
print(f"{'✅' if fits else '❌'} {'…without the global file: still one line':<52} → {len(m2_sum)} line(s)")
ok &= fits and len(m2_sum) == 1

# «## Claude Code files in …» only when there are Claude Code files, and before them
def context(d, **extra):
    msg, _n = session_start._build_with_notices({"conversationId": "agp-setuptest-ctx1", "workspacePaths": [d], "lastUserInput": "hi", **extra})
    return msg
c_msg, b_msg = context(cc_ws), context(big_ws)
head_ok = ("## Claude Code files in" in c_msg and c_msg.index("## Claude Code files in") < c_msg.index("### CLAUDE.md")
           and "UNIQUE_CLAUDE_ONLY_MARKER" in c_msg and "## Claude Code files in" not in b_msg and "UNIQUE_BIG_RULE_MARKER" not in b_msg)
ok &= head_ok
print(f"{'✅' if head_ok else '❌'} {'Claude Code heading only with Claude Code files':<52} → {head_ok}")
# a parentConversationId equal to the conversation's own id is no parent
own = "agp-setuptest-own1"
subs = [({"conversationId": own, "parentConversationId": own, "lastUserInput": "hi"}, False),
        ({"conversationId": own, "parentConversationId": f" {own} ", "lastUserInput": "hi"}, False),
        ({"conversationId": own, "parentConversationId": "", "lastUserInput": "hi"}, False),
        ({"conversationId": own, "parentConversationId": "   ", "lastUserInput": "hi"}, False),
        ({"conversationId": own, "parentConversationId": 7, "lastUserInput": "hi"}, False),
        ({"conversationId": own, "parentConversationId": "agp-setuptest-parent", "lastUserInput": "hi"}, True)]
sub_ok = all(session_start.is_subagent(p) is want for p, want in subs)
ok &= sub_ok
print(f"{'✅' if sub_ok else '❌'} {'is_subagent: own id / empty / non-string parent is none':<52} → {[session_start.is_subagent(p) for p, _ in subs]}")
for d in (small_ws, big_ws, mid_ws, cc_ws, ws, art): shutil.rmtree(d, ignore_errors=True)
clear_state()
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
