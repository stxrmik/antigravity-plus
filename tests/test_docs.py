"""The 0.28.0 documentation: component counts match the repository, AGENTS.md keeps what an engineer needs and stays
under Antigravity's rules budget, and claims that turned out unsupported don't come back."""
import json, os, re, sys
PLUGIN = sys.argv[1]
ROOT = os.path.dirname(PLUGIN)
ok = True
def check(name, cond, detail=""):
    global ok; ok &= bool(cond); print(f"{'✅' if cond else '❌'} {name}" + (f" — {str(detail)[:900]}" if not cond and detail != "" else ""))
def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()
agents_md = read("AGENTS.md")
readme_ru, readme_en = read("README.md"), read("README.en.md")
skill = read("claude-code", "skills", "antigravity", "SKILL.md")
manifest = read("antigravity-plus", "plugin.json")
docs = {"AGENTS.md": agents_md, "README.md": readme_ru, "README.en.md": readme_en, "SKILL.md": skill, "plugin.json": manifest}

# --- counts ---
skills = len([n for n in os.listdir(os.path.join(PLUGIN, "skills")) if os.path.isfile(os.path.join(PLUGIN, "skills", n, "SKILL.md"))])
agents = len([n for n in os.listdir(os.path.join(PLUGIN, "agents")) if n.endswith(".md")])
with open(os.path.join(PLUGIN, "hooks.json"), encoding="utf-8") as f:
    hooks = json.load(f)  # {hook name: {event: [...]}}: one entry per hook
tools = len(re.findall(r'^    \{"name": "\w+", "script":', read("antigravity-plus", "mcp", "server.py"), re.M))
check("counted 36 skills, 7 subagents, 6 hooks, 19 tools", (skills, agents, len(hooks), tools) == (36, 7, 6, 19), (skills, agents, len(hooks), tools))
want = f"{skills} skills, {agents} subagents, {len(hooks)} hooks"
check("AGENTS.md and plugin.json state the counts", want in agents_md and want in manifest and f"{tools}-tool" in agents_md and f"{tools}-tool" in manifest)
check("README.md states the counts", f"{skills} команд, {agents} субагентов, {len(hooks)} хуков" in readme_ru and f"{tools} инструментов" in readme_ru)
check("README.en.md states the counts", f"{skills} commands, {agents} subagents, {len(hooks)} hooks, a {tools}-tool" in readme_en)

# --- AGENTS.md size and what it keeps ---
size = len(agents_md.encode("utf-8"))
check("AGENTS.md stays under the 18.5 KB rules budget", size <= 18400, size)
keep = ["SYSTEM_RULES", "LOCAL_RULES", "kill -9 -1", "update.py background", "~/.gemini/antigravity-plus/source", "ROOTS", "edit_problems()",
        "search_hint()", "text cross-check", "code-reviewer", "security-auditor", "explorer", "researcher", "worker", "debugger", "test-writer",
        "LSP.md", "`off`", "`keep`", "listen()", "agentMessage", "`background`", "--model orchestrator", "httpsPort", "lsVersion", "csrfToken",
        "ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CONVERSATION_ID", "PLUGIN_DATA", "\"CRON_TZ=<zone> <cron>\"", "/bypass off", "enter_plan_mode",
        "Plan mode is off: this turn runs on the execution model", "[plan review]", "stepIndices", "group_of()", "forceRefresh",
        "10–15%", "Hydrogen 1, Neon 2, Carbon ~4", "2026-11-20", "2026-11-24", "2.5% of its week", "511.8k of 512k",
        "use-model-reported-token-count", "modules.json", "budget"]
missing = [k for k in keep if k not in agents_md]
check("AGENTS.md keeps the restored engineering facts", not missing, missing)
check("AGENTS.md `fast` lists the real order",
      "`fast` Hydrogen Low → Hydrogen Medium → Neon Low → Claude Sonnet 5.5 Low" in agents_md)

# --- claims that were unsupported ---
bad = {
    r"-\s?0[.,]95\s?%": "weekly-quota figure that no record supports",
    r"(?i)(AI Pro[^.]{0,80}(includes|включает)[^.]{0,40}Carbon)|(Neon (и|and|&) Carbon)[^.]{0,40}(AI Pro|с 2\.34|since 2\.34)":
        "AI Pro gets Neon and Orchestrator (Neon), not Carbon",
    r"9[–-]26": "mixes two runs (11–26 min and the 9-minute cap)",
    r"(?i)keep `--timeout`[^.]{0,20}(>=|at `?1800)|ставьте `--timeout`": "--timeout already defaults to 1800 s: don't lower it",
    r"(?i)solved every task in 1[–-]2 min": "Hydrogen (Medium) took 0.7–3.8 min",
    r"(?i)Carbon (и|and) Argon \$2": "only Carbon's price doubles on 2026-12-01",
    r"(?i)default effort (shifted|is `?High`? \(was)|усилие Orchestrator по умолчанию снижено": "only the configurable Orchestrator defaults to High",
}
for pattern, why in bad.items():
    hits = [name for name, text in docs.items() if re.search(pattern, text)]
    check(f"no «{why}»", not hits, hits)
for name, text in docs.items():
    sentences = re.split(r"(?<=[.;])\s+", text)
    wrong = [s[:120] for s in sentences if "Orchestrator (Argon)" in s and re.search(r"(?i)leav|уходящ|removes|убран|removed", s)
             and not re.search(r"(?i)(says nothing|has not said|не говорит|nothing like|not Orch|hasn't said)", s)]
    check(f"{name} doesn't say Orchestrator (Argon) leaves", not wrong, wrong)
check("SKILL.md `standard` says ~1–4 min", "~1–4 min" in skill)
check("every doc says AGY_CLI_AUTO_COMPACT_OVERRIDE is an Antigravity CLI setting",
      all("Antigravity CLI" in t for n, t in docs.items() if "AGY_CLI_AUTO_COMPACT_OVERRIDE" in t), [n for n, t in docs.items() if "AGY_CLI_AUTO_COMPACT_OVERRIDE" in t and "Antigravity CLI" not in t])
sys.exit(0 if ok else 1)
