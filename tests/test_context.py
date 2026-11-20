"""/context (skills/context/scripts/context.py): CHECKPOINT cutoff, Cyrillic/code estimation, read_file tips,
PLANNER_RESPONSE usage calibration (measured / proportional / estimate), live vs dropped hooks, top-10, --json,
and cross-platform path handling."""
import json
import os
import subprocess
import sys

PLUGIN, SP = sys.argv[1], sys.argv[2]
CTX_DIR = os.path.join(PLUGIN, "skills", "context", "scripts")
sys.path.insert(0, CTX_DIR)
import context  # noqa: E402

ok = True


def check(name: str, cond: object, detail: object = "") -> None:
    global ok
    ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + ("" if cond else f" → {str(detail)[:900]}"))


# 1. Estimation heuristics & Windows path / URI helpers
check("estimate: empty text is 0", context.estimate("") == 0.0)
en_prose = "This is a simple plain English sentence without symbols. " * 10
ru_prose = "Это простая русская фраза для проверки оценки кириллицы. " * 10
code_txt = "def fn(a, b): return {k: [a + b] for k in range(10)}\n" * 10
cjk_txt = "你好世界测试上下文计算" * 10
check("estimate: Cyrillic costs more tokens per char than Latin prose, code > Latin prose, CJK ≈ 1/char",
      context.estimate(ru_prose) / len(ru_prose) > context.estimate(en_prose) / len(en_prose)
      and context.estimate(code_txt) / len(code_txt) > context.estimate(en_prose) / len(en_prose)
      and abs(context.estimate(cjk_txt) - len(cjk_txt)) < 1e-6,
      (context.estimate(en_prose), context.estimate(ru_prose), context.estimate(code_txt), context.estimate(cjk_txt)))

win_msg = "## Workspace: C:\\Users\\dev\\my-repo\nBranch: main\n"
check("WORKSPACE_RE captures full Windows path with backslashes",
      context.WORKSPACE_RE.findall(win_msg) == ["C:\\Users\\dev\\my-repo"],
      context.WORKSPACE_RE.findall(win_msg))

ws = os.path.realpath(os.path.join(SP, "proj"))
os.makedirs(ws, exist_ok=True)
with open(os.path.join(ws, "AGENTS.md"), "w", encoding="utf-8") as f:
    f.write("# Project rules\n" + "Keep functions small and tested.\n" * 10)

ws_uri = "file://" + ("/" if not ws.startswith("/") else "") + ws.replace("\\", "/") + "/src/app.py"
check("rel() strips workspace prefix for both plain paths and file:// URIs",
      context.rel(os.path.join(ws, "src", "app.py"), [ws]) == os.path.join("src", "app.py")
      and context.rel(ws_uri, [ws]) == os.path.join("src", "app.py"),
      (context.rel(os.path.join(ws, "src", "app.py"), [ws]), context.rel(ws_uri, [ws])))

# 2. Synthetic transcript with CHECKPOINT, Cyrillic, large/repeated read_file, live/dropped hooks, calibration
cid = "aaaaaaaa-1111-4222-8333-bbbbbbbbbbbb"
root = os.path.join(SP, "ag_root")
logs = os.path.join(root, "brain", cid, ".system_generated", "logs")
os.makedirs(logs, exist_ok=True)

big_file_text = ("# большого размера модуль на русском и коде\n" + "def item(x):\n    return x * 2  # элемент\n") * 800
pre_ckpt_text = "Старое содержимое до сжатия контекста, которое не должно попасть в текущие категории. " * 200
tail_out_text = "результат чтения хвоста файла " * 60
write_code = "def generated():\n    return 'привет'\n" * 250

steps_pre = [
    {"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": "2026-11-17T10:00:00Z",
     "content": "<USER_REQUEST>Проверь старый код</USER_REQUEST>\nModel Selection` from None to Gemini 4 Carbon (High)."},
    {"step_index": 1, "type": "SYSTEM_MESSAGE", "source": "SYSTEM_SDK", "created_at": "2026-11-17T10:00:01Z",
     "content": f"<antigravity_plus_session_context>\n## Workspace: {ws}\n</antigravity_plus_session_context>"},
    {"step_index": 2, "type": "PLANNER_RESPONSE", "created_at": "2026-11-17T10:00:02Z",
     "input_tokens": 25_000, "cache_read_tokens": 0, "output_tokens": 100,
     "tool_calls": [{"name": "read_file", "args": {"Path": os.path.join(ws, "old_before_ckpt.py")}}]},
    {"step_index": 3, "type": "GENERIC", "created_at": "2026-11-17T10:00:03Z", "content": pre_ckpt_text},
]

steps_post = [
    {"step_index": 4, "type": "CHECKPOINT", "created_at": "2026-11-17T10:01:00Z",
     "content": "Сводка после сжатия: старый модуль проверен, переходим к main_module.py."},
    {"step_index": 5, "type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": "2026-11-17T10:01:05Z",
     "content": "<USER_REQUEST>Прочитай весь main_module.py, поправь и запиши out.py на кириллице.</USER_REQUEST>"},
    {"step_index": 6, "type": "EPHEMERAL_MESSAGE", "created_at": "2026-11-17T10:01:06Z",
     "content": '<sandbox source="Antigravity Plus">Старая подсказка песочницы (должна исчезнуть)</sandbox>'},
    {"step_index": 7, "type": "PLANNER_RESPONSE", "created_at": "2026-11-17T10:01:07Z",
     "content": "Читаю файлы и записываю результат.",
     "thinking": "Размышляю над структурой модуля " * 40,
     "tool_calls": [
         {"name": "read_file", "args": {"Path": os.path.join(ws, "main_module.py")}},
         {"name": "read_file", "args": {"Path": os.path.join(ws, "main_module.py"), "StartLine": 1, "EndLine": 120}},
         {"name": "read_file", "args": {"Path": os.path.join(ws, "helper.py"), "StartLine": 10, "EndLine": 40}},
         {"name": "run_command", "args": {"CommandLine": "python3 -m unittest -v"}},
         {"name": "write_file", "args": {"Path": os.path.join(ws, "out.py"), "Content": write_code}},
         {"name": "read_file", "args": {"Path": os.path.join(ws, "extra_a.py"), "StartLine": 1, "EndLine": 20}},
         {"name": "read_file", "args": {"Path": os.path.join(ws, "extra_b.py"), "StartLine": 1, "EndLine": 20}},
         {"name": "read_file", "args": {"Path": os.path.join(ws, "extra_c.py"), "StartLine": 1, "EndLine": 20}},
     ]},
    {"step_index": 8, "type": "GENERIC", "created_at": "2026-11-17T10:01:08Z", "content": big_file_text},
    {"step_index": 9, "type": "GENERIC", "created_at": "2026-11-17T10:01:09Z", "content": big_file_text[:8000]},
    {"step_index": 10, "type": "GENERIC", "created_at": "2026-11-17T10:01:10Z", "content": "def helper():\n    return 1\n" * 60},
    {"step_index": 11, "type": "GENERIC", "created_at": "2026-11-17T10:01:11Z", "content": "Ran 12 tests in 0.1s\nOK\n" * 80},
    {"step_index": 12, "type": "GENERIC", "created_at": "2026-11-17T10:01:12Z", "content": "Wrote out.py"},
    {"step_index": 13, "type": "GENERIC", "created_at": "2026-11-17T10:01:13Z", "content": "x = 1\n" * 40},
    {"step_index": 14, "type": "GENERIC", "created_at": "2026-11-17T10:01:14Z", "content": "y = 2\n" * 40},
    {"step_index": 15, "type": "GENERIC", "created_at": "2026-11-17T10:01:15Z", "content": "z = 3\n" * 40},
    {"step_index": 16, "type": "EPHEMERAL_MESSAGE", "created_at": "2026-11-17T10:01:16Z",
     "content": '<context_budget source="Antigravity Plus">Подсказка прошлого вызова (выпала)</context_budget>'},
]

# Compute exact realistic input_tokens for step 17 so measured calibration hits scale ~1.10
before_first = context.estimate(steps_pre[0]["content"]) + context.estimate(steps_pre[1]["content"])
measured_overhead = 25_000 - before_first
est_in_call = sum(
    context.estimate(st.get("content") or "")
    for st in steps_post if st["type"] != "EPHEMERAL_MESSAGE" or st["step_index"] == 16
) + context.estimate(steps_post[3]["thinking"]) * context.THINKING_WEIGHT + sum(
    context.estimate(tc["name"] + json.dumps(tc["args"], ensure_ascii=False))
    for tc in steps_post[3]["tool_calls"]
)
real_last_prompt = round(measured_overhead + est_in_call * 1.12)

steps_tail = [
    {"step_index": 17, "type": "PLANNER_RESPONSE", "created_at": "2026-11-17T10:01:17Z",
     "input_tokens": 5_000, "cache_read_tokens": real_last_prompt - 5_000, "output_tokens": 90,
     "content": "Читаю последний фрагмент.",
     "tool_calls": [{"name": "read_file", "args": {"Path": os.path.join(ws, "tail.py"), "StartLine": 1, "EndLine": 30}}]},
    {"step_index": 18, "type": "GENERIC", "created_at": "2026-11-17T10:01:18Z", "content": tail_out_text},
    {"step_index": 19, "type": "EPHEMERAL_MESSAGE", "created_at": "2026-11-17T10:01:19Z",
     "content": '<quota source="Antigravity Plus">Текущая подсказка квоты (живая сейчас)</quota>'},
]

tf = os.path.join(logs, "transcript_full.jsonl")
with open(tf, "w", encoding="utf-8") as f:
    for st in steps_pre + steps_post + steps_tail:
        f.write(json.dumps(st, ensure_ascii=False) + "\n")

# Also add a subagent transcript for the same workspace (newer mtime, no USER_INPUT) to verify newest_of skips it
sub_cid = "cccccccc-2222-4333-8444-dddddddddddd"
sub_logs = os.path.join(root, "brain", sub_cid, ".system_generated", "logs")
os.makedirs(sub_logs, exist_ok=True)
with open(os.path.join(sub_logs, "transcript_full.jsonl"), "w", encoding="utf-8") as f:
    f.write(json.dumps({"step_index": 0, "type": "SYSTEM_MESSAGE",
                        "content": f"<antigravity_plus_session_context>\n## Workspace: {ws}\n</antigravity_plus_session_context>"}) + "\n")

script = os.path.join(CTX_DIR, "context.py")
r_json = subprocess.run([sys.executable, script, "--conversation", cid, "--root", root, "--json"],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
check("context.py --conversation --json exits 0", r_json.returncode == 0, r_json.stderr)
data = json.loads(r_json.stdout)

check("calibration uses 'measured' method and scale ≈ 1.12",
      data["calibration"]["method"] == "measured" and 1.0 <= data["calibration"]["scale"] <= 1.25,
      data["calibration"])
check("checkpoint metadata tracked (step 4, count 1)",
      data["since_checkpoint"] == {"step": 4, "checkpoints": 1},
      data["since_checkpoint"])
check("real prompt matches last PLANNER_RESPONSE (step 17)",
      data["real"] == {"tokens": real_last_prompt, "step": 17},
      data["real"])
check("after_last_call and hooks_dropped both positive",
      data["after_last_call"] > 0 and data["hooks_dropped"] > 0,
      (data["after_last_call"], data["hooks_dropped"]))

cat_keys = {c["key"]: c["tokens"] for c in data["categories"]}
check("categories include fixed overhead, checkpoint, user, thinking, tool_args, tool_out, and live quota hook",
      all(k in cat_keys for k in ("rules", "checkpoint", "user", "thinking", "tool_args:write_file",
                                  "tool_out:read_file", "hooks:<quota>")),
      list(cat_keys))
check("gone (sandbox) and dropped (context_budget) hooks are NOT in live categories",
      "hooks:<sandbox>" not in cat_keys and "hooks:<context_budget>" not in cat_keys,
      list(cat_keys))
cat_sum = sum(c["tokens"] for c in data["categories"])
check("sum of category tokens equals total 'now' within rounding",
      abs(cat_sum - data["now"]) <= len(data["categories"]),
      (cat_sum, data["now"]))

top_items = data["top"]
check("top items limited to 10, sorted descending, exclude fixed rules/system, exclude pre-checkpoint read",
      len(top_items) == 10
      and all(top_items[i]["tokens"] >= top_items[i + 1]["tokens"] for i in range(9))
      and all(t["category"] not in ("system", "rules", "skills") for t in top_items)
      and all("old_before_ckpt.py" not in t["label"] for t in top_items)
      and any("main_module.py" in t["label"] for t in top_items),
      top_items[:5])
check("model window loaded from pricing.json (Antigravity compacts when a call won't fit it: no separate threshold)",
      data["model"] == "Gemini 4 Carbon (High)" and data["window"] == 512_000 and data["compaction"] is None,
      (data["model"], data["window"], data["compaction"]))
check("tips mention whole-file read and repeated read of main_module.py",
      any("main_module.py" in t and "раз" in t for t in data["tips"])
      and any("Чтение файлов целиком" in t for t in data["tips"]),
      data["tips"])

# 3. Markdown output + --workspace skips subagent
r_md = subprocess.run([sys.executable, script, "--workspace", ws, "--root", root],
                      capture_output=True, text=True, encoding="utf-8", errors="replace")
check("context.py --workspace selects main conversation (skipping newer subagent) and renders Markdown",
      r_md.returncode == 0 and "## Что занимает контекст" in r_md.stdout
      and "### 10 самых крупных элементов" in r_md.stdout
      and "Считается с последнего сжатия (шаг 4, всего сжатий: 1)" in r_md.stdout
      and "main_module.py" in r_md.stdout,
      r_md.stdout[:500] + r_md.stderr)

# 4. Proportional fallback & estimate mode
prop_dir = os.path.join(SP, "prop_logs")
os.makedirs(prop_dir, exist_ok=True)
tf_prop = os.path.join(prop_dir, "transcript_full.jsonl")
with open(tf_prop, "w", encoding="utf-8") as f:
    for st in [
        {"step_index": 0, "type": "USER_INPUT", "content": "Short prompt"},
        {"step_index": 1, "type": "PLANNER_RESPONSE", "input_tokens": 15_000, "cache_read_tokens": 0,
         "content": "First response"},
        {"step_index": 2, "type": "USER_INPUT", "content": "Follow up"},
        {"step_index": 3, "type": "PLANNER_RESPONSE", "input_tokens": 500_000, "cache_read_tokens": 0,
         "content": "Huge input tokens vs tiny text -> scale > 3 -> proportional."},
    ]:
        f.write(json.dumps(st) + "\n")
r_prop = subprocess.run([sys.executable, script, "--transcript", tf_prop, "--json"],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
d_prop = json.loads(r_prop.stdout)
check("calibration falls back to 'proportional' when scale is outside [0.33, 3]",
      d_prop["calibration"]["method"] == "proportional" and d_prop["real"]["tokens"] == 500_000,
      d_prop["calibration"])

est_dir = os.path.join(SP, "est_logs")
os.makedirs(est_dir, exist_ok=True)
tf_est = os.path.join(est_dir, "transcript_full.jsonl")
with open(tf_est, "w", encoding="utf-8") as f:
    f.write(json.dumps({"step_index": 0, "type": "USER_INPUT", "content": "No usage yet"}) + "\n")
r_est = subprocess.run([sys.executable, script, "--transcript", tf_est, "--json"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
d_est = json.loads(r_est.stdout)
check("no PLANNER_RESPONSE usage falls back to 'estimate' and marks model_guessed=True",
      d_est["calibration"]["method"] == "estimate" and d_est["real"] is None and d_est["model_guessed"] is True,
      (d_est["calibration"], d_est["model_guessed"]))

r_miss = subprocess.run([sys.executable, script, "--conversation", "00000000-0000-4000-8000-000000000000", "--root", root],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
check("missing conversation exits 2", r_miss.returncode == 2, r_miss.returncode)

if hasattr(os, "symlink"):
    ws_link = os.path.join(SP, "ws_link")
    try:
        os.symlink(ws, ws_link)
        r_sym = subprocess.run([sys.executable, script, "--workspace", ws_link, "--root", root, "--json"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
        d_sym = json.loads(r_sym.stdout)
        check("--workspace normalizes symlinks", r_sym.returncode == 0
              and d_sym.get("workspaces") == [ws]
              and any("main_module.py" in t["label"] for t in d_sym.get("top", [])), (r_sym.returncode, d_sym))
    except OSError:
        pass

# 5. New SYSTEM_MESSAGE PreInvocation hints & zero-token user_markdown steps
sys_dir = os.path.join(SP, "sys_logs")
os.makedirs(sys_dir, exist_ok=True)
tf_sys = os.path.join(sys_dir, "transcript_full.jsonl")
with open(tf_sys, "w", encoding="utf-8") as f:
    for st in [
        {"step_index": 0, "type": "USER_INPUT", "content": "Hello"},
        {"step_index": 1, "type": "SYSTEM_MESSAGE", "content": "",
         "metadata": {"user_markdown": "📦 **Доступно обновление Antigravity Plus** — `/update`"}},
        {"step_index": 2, "type": "SYSTEM_MESSAGE", "content": "<SYSTEM_MESSAGE>\n</SYSTEM_MESSAGE>",
         "metadata": {"user_markdown": "🔴 **Квота на исходе**"}},
        {"step_index": 3, "type": "SYSTEM_MESSAGE",
         "content": '<SYSTEM_MESSAGE>\n<user_instructions source="Antigravity Plus">Remember rule X.</user_instructions>\n</SYSTEM_MESSAGE>'},
        {"step_index": 4, "type": "SYSTEM_MESSAGE",
         "content": "The user turned Bypass Mode ON (message 1)."},
    ]:
        f.write(json.dumps(st, ensure_ascii=False) + "\n")
r_sys = subprocess.run([sys.executable, script, "--transcript", tf_sys, "--json"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
d_sys = json.loads(r_sys.stdout)
sys_cats = {c["key"]: c["tokens"] for c in d_sys["categories"]}
check("SYSTEM_MESSAGE PreInvocation hints counted under hooks & zero-token user_markdown skipped",
      "hooks:<user_instructions>" in sys_cats and "hooks:bypass_notice" in sys_cats
      and "system_msg" not in sys_cats,
      sys_cats)

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
