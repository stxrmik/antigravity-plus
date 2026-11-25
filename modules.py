"""Antigravity Plus — the modules you can choose at install time (install.py --choose, --modules, --list).

A module is what the user decides about: a set of skills, subagents, rule files and side panels that are installed
or left out, plus the hook features it owns (settings keys forced off while it isn't installed). The hooks
themselves are always installed — they import each other — and features of a missing module stay silent.

Each module: id, title, summary (one line) and details (what it does, what you lose without it, what it needs) in
Russian and English; skills (folder names under skills/), agents (agents/<name>.md), rules (rules/<file>), sidecars
(sidecars/<name>), off (settings forced while the module is missing), needs (tools it uses, checked to warn), claude
(Claude Code's side, installed into ~/.claude); keep (paths installed even without the module — scripts the
hooks need). Stdlib only, Python 3.8+.
"""
from __future__ import annotations

from typing import Dict, List

MODULES: List[Dict] = [
    {
        "id": "core",
        "required": True,
        "title": {"ru": "Основа", "en": "Core"},
        "summary": {"ru": "безопасность, контекст проекта, ваши указания, правила работы",
                    "en": "safety, project context, your instructions, the working rules"},
        "details": {
            "ru": "Guard останавливает необратимое и опасное (rm -rf, git reset --hard, force-push, sudo, curl | sh, "
                  "DROP TABLE…), не выводит секреты в разговор (cat .env) и не даёт агенту менять собственные "
                  "разрешения; при запрете на пуш различает read-only SSH-проверки на Python (AST-разбор). "
                  "Bypass Mode: один раз «/bypass» в чате — и агенты во всех разговорах работают без "
                  "запросов разрешения, пока вы не напишете «/bypass off»; спрашивают только о системном (sudo, "
                  "системные папки, диски, автозапуск, ключи, права агента). В начале разговора агент получает снимок git и правила проекта — AGENTS.md, а также "
                  "CLAUDE.md с @импортами и .claude/rules, — а ваши указания («ничего не пушь») снова видит после "
                  "каждого сжатия контекста и при передаче плана в Orchestrator. Правила работы: понять запрос, "
                  "спланировать, сделать, проверить; несколько агентов в одном проекте не затирают правки друг друга "
                  "(при конфликте версий агент сразу получает компактный diff). Уведомления о Bypass, лимитах и "
                  "обновлениях выводятся плашками в UI без лишнего расхода токенов (`user_markdown`). "
                  "Встроенный MCP-сервер (`antigravity-plus_tools`) отдаёт 19 типизированных инструментов (`lsp_*`, "
                  "`ci_*`, `deps_audit`, `ui_check`, `mobile_*`, `memory_search`, `git_state`, `quota`). "
                  "Команды агентов можно запускать с пониженным приоритетом и ограничить число тяжёлых программ "
                  "одновременно (ffmpeg, blender, xcodebuild…), чтобы они не тормозили вашу работу (/settings). "
                  "Команды: /init (правила проекта в AGENTS.md), /remember, /settings, /doctor, /hooks, /update (новая версия "
                  "одной командой: раз в сутки агент сам скажет, что она вышла; откат одной командой). "
                  "Обязательна: остальные модули работают поверх неё.",
            "en": "The guard stops what is irreversible or dangerous (rm -rf, git reset --hard, force-push, sudo, "
                  "curl | sh, DROP TABLE…), keeps secrets out of the conversation (cat .env), never lets the agent "
                  "change its own permissions, and uses AST inspection to tell read-only SSH Python probes apart under a push ban. "
                  "Bypass Mode: type «/bypass» once and agents in every conversation work "
                  "without permission prompts until «/bypass off»; only system-level actions still ask (sudo, system "
                  "folders, disks, autostart, keys, the agent's permissions). At the start of a conversation the agent gets a git snapshot and the "
                  "project's rules — AGENTS.md, plus CLAUDE.md with its @imports and .claude/rules — and sees your "
                  "instructions («don't push anything») again after context compaction and Orchestrator plan handoffs. "
                  "Working rules: understand the request, plan, do, verify; several agents on "
                  "one project don't overwrite each other's edits (returning compact diffs on stale-write conflicts). "
                  "Status banners for Bypass, limits, and updates render inline via `user_markdown` with zero extra model tokens. "
                  "A bundled MCP server (`antigravity-plus_tools`) exposes 19 typed tools (`lsp_*`, `ci_*`, `deps_audit`, "
                  "`ui_check`, `mobile_*`, `memory_search`, `git_state`, `quota`). Agents' commands can run at a lower "
                  "priority, and the number of heavy programs at once (ffmpeg, blender, xcodebuild…) can be capped, so "
                  "they don't slow your own work (/settings). Commands: /init (project rules in AGENTS.md), "
                  "/remember, /settings, /doctor, /hooks, /update (a new release in one command: once a day the agent tells you "
                  "one is out; rolls back in one command). Required: every other module builds on it.",
        },
        "skills": ["doctor", "settings", "init", "remember", "hooks", "update"],
        "rules": ["AGENTS.md"],
    },
    {
        "id": "quality",
        "title": {"ru": "Качество и доведение до конца", "en": "Quality and finishing the work"},
        "summary": {"ru": "проверка каждой правки, ревью, агент не бросает задачу на середине",
                    "en": "every edit checked, reviews, the agent doesn't stop halfway"},
        "details": {
            "ru": "Каждая правка проверяется сразу: синтаксис Python, JS/TS, Swift, shell, JSON, YAML, plist и линтеры "
                  "проекта (ruff, eslint, swiftlint, shellcheck) — ошибка доходит до агента следующим шагом, а не через "
                  "три. Агент не закончит ход, пока в task.md остались пункты, изменённый код не проверен тестами или "
                  "сборкой, проверяет планы Orchestrator (`exit_plan_mode`) на наличие шага верификации, а после крупной "
                  "правки сам запускает ревью. Команды: /review (два ревьюера с перепроверкой находок), "
                  "/security-review (аудит с проверкой эксплуатируемости), /simplify, /deps (аудит уязвимостей и "
                  "устаревших зависимостей), /diff-cover (покрытие тестами именно изменённых строк), /perf (честные "
                  "A/B-замеры скорости и размера бандла); субагенты code-reviewer, security-auditor, debugger, "
                  "test-writer. Без модуля правки не проверяются, и агент может остановиться на полпути.",
            "en": "Every edit is checked at once: Python, JS/TS, Swift, shell, JSON, YAML and plist syntax plus the "
                  "project's linters (ruff, eslint, swiftlint, shellcheck) — an error reaches the agent at the next "
                  "step, not three steps later. The agent can't end its turn while task.md has open items or changed "
                  "code hasn't been run through tests or a build, Orchestrator plans (`exit_plan_mode`) are checked for "
                  "verification steps, and it reviews big changes on its own. Commands: /review (two reviewers, "
                  "findings re-checked), /security-review (an audit that checks exploitability), /simplify, /deps "
                  "(vulnerability and outdated dependency audit), /diff-cover (test coverage of git-changed lines), "
                  "/perf (statistical A/B and worktree benchmarks); subagents code-reviewer, security-auditor, "
                  "debugger, test-writer. Without it edits aren't checked and the agent may stop halfway.",
        },
        "skills": ["review", "security-review", "simplify", "deps", "diff-cover", "perf"],
        "agents": ["code-reviewer", "security-auditor", "debugger", "test-writer"],
        "off": {"checkEdits": False, "finishTasks": False, "autoReview": False, "autoVerify": False},
    },
    {
        "id": "facts",
        "title": {"ru": "Факты и честные ответы", "en": "Facts and honest answers"},
        "summary": {"ru": "отчёт сверяется с тем, что было; возражение — повод проверить, а не поддакнуть",
                    "en": "the report is held to what happened; an objection means checking, not agreeing"},
        "details": {
            "ru": "Финальный отчёт агента сверяется с разговором: «тесты проходят», «N passed», «CI зелёный», "
                  "«задеплоил», «ускорил в N раз», пути, номера строк, ссылки, версии и цены должны чем-то подтверждаться — иначе агент "
                  "возвращается проверить. Команда, которая только печатает «ok», проверкой не считается. На вопрос "
                  "о фактах агент ищет источник и ссылается на него. На возражение он сначала перепроверяет, а не "
                  "отвечает «Вы абсолютно правы»; ваша догадка о причине — гипотеза, а не диагноз. Правила FACTS.md и "
                  "OBJECTIVE.md.",
            "en": "The agent's final report is held to the conversation: «tests pass», «N passed», «CI is green», "
                  "«deployed», «N× faster», paths, line numbers, links, versions and prices need something behind them — otherwise "
                  "the agent goes back to check. A command that only prints «ok» isn't a check. Questions about facts "
                  "get looked up and cited. An objection makes the agent check again instead of answering «You're "
                  "absolutely right»; your guess at a cause is a hypothesis, not the diagnosis. Rules FACTS.md and "
                  "OBJECTIVE.md.",
        },
        "rules": ["FACTS.md", "OBJECTIVE.md"],
        "off": {"factChecks": False, "objectiveAnswers": False},
    },
    {
        "id": "memory",
        "title": {"ru": "Память и продолжение работы", "en": "Memory and continuing work"},
        "summary": {"ru": "агент помнит важное между разговорами; память общая с Claude Code",
                    "en": "the agent remembers what matters between conversations; shared with Claude Code"},
        "details": {
            "ru": "Агент сам сохраняет то, что пригодится в следующих разговорах: ваши поправки и предпочтения, как "
                  "устроены серверы и деплой, причину упрямой ошибки. Память проекта общая с Claude Code "
                  "(~/.claude/projects/…/memory), секреты в неё не пропускаются. С каждым запросом агент видит "
                  "относящиеся к нему воспоминания. /handoff подводит итог для продолжения в новом разговоре, а "
                  "«продолжи» его подхватывает; в начале разговора показываются последние разговоры проекта. Команды "
                  "/memory, /handoff. Без модуля каждый разговор начинается с нуля.",
            "en": "The agent saves on its own what later conversations will need: your corrections and preferences, "
                  "how the servers and deploys work, the cause of a stubborn failure. Project memory is shared with "
                  "Claude Code (~/.claude/projects/…/memory), and no secrets get in. With each request the agent sees "
                  "the memories it is about. /handoff sums up the work for a new conversation and «continue» picks it "
                  "up; the latest conversations of the project are listed at the start. Commands /memory, /handoff. "
                  "Without it every conversation starts from scratch.",
        },
        "skills": ["memory", "handoff"],
        "keep": ["skills/memory/scripts"],  # memory.py holds the plugin's settings every hook reads
        "rules": ["MEMORY.md"],
        "off": {"autoMemory": False, "recentConversations": 0},
    },
    {
        "id": "parallel",
        "title": {"ru": "Параллельная работа и субагенты", "en": "Parallel work and subagents"},
        "summary": {"ru": "независимые части задачи уходят субагентам, агент не простаивает",
                    "en": "independent parts go to subagents while the agent keeps working"},
        "details": {
            "ru": "Агент сам раздаёт независимые части задачи субагентам одним шагом и продолжает свою часть, а не "
                  "ждёт: explorer ищет по коду, researcher — документацию и API (оба на быстрой модели), worker делает "
                  "часть реализации в своих файлах. Для долгой многоролевой работы понимает нативный Swarm (`swarm`) "
                  "и доску сообщений. Пока помощники работают, агент их не опрашивает и не простаивает; "
                  "отчёты перепроверяет, прежде чем на них опираться. Команда /parallel с шаблонами заданий. Тратит "
                  "больше квоты в час, зато заканчивает быстрее.",
            "en": "The agent hands independent parts of a task to subagents in one step and keeps working on its own "
                  "part instead of waiting: explorer searches the code, researcher the docs and APIs (both on a fast "
                  "model), worker builds a part in its own files; for long multi-role work it also drives native Swarm "
                  "(`swarm`) on the shared board. Meanwhile it neither polls nor idles, and it checks "
                  "their reports before building on them. Command /parallel with brief templates. Spends more quota "
                  "per hour, finishes sooner.",
        },
        "skills": ["parallel"],
        "agents": ["explorer", "researcher", "worker"],
        "rules": ["PARALLEL.md"],
        "off": {"parallelHints": False},
    },
    {
        "id": "speed",
        "title": {"ru": "Скорость, контекст и квота", "en": "Speed, context and quota"},
        "summary": {"ru": "меньше лишних шагов: песочница, бюджет контекста, циклы ошибок, лимиты плана",
                    "en": "fewer wasted steps: the sandbox, context budget, loops of errors, plan limits"},
        "details": {
            "ru": "Команды, которые падали в песочнице терминала, запоминаются по проекту и дальше сразу подставляют "
                  "`BypassSandbox: true` на лету (`PreToolUse` overwrite). Агент видит, сколько контекста уже занято, "
                  "и читает большие файлы по частям; после трёх неудачных команд подряд меняет подход, а не повторяет "
                  "одно и то же; а когда квота текущей группы моделей подходит к концу, получает подсказку с остатком "
                  "и советами сэкономить. Только подсказки агенту, своих команд нет.",
            "en": "Commands that failed inside the terminal sandbox are remembered per project and automatically get "
                  "`BypassSandbox: true` injected on the fly via `PreToolUse` overwrite. The agent sees how full its "
                  "context is and reads big files in parts; after three failing commands in a row it changes its approach "
                  "instead of repeating itself; and when the current model group's quota gets short, it hears the "
                  "remaining numbers and ways to stretch them. Hints to the agent only, no commands of its own.",
        },
        "off": {"speedHints": False, "quotaHints": False},
    },
    {
        "id": "git",
        "title": {"ru": "Git и GitHub", "en": "Git and GitHub"},
        "summary": {"ru": "ветки, синхронизация, конфликты, история, откат, GitHub: issues, PR, ревью, CI",
                    "en": "branches, syncing, conflicts, history, undo, GitHub: issues, PRs, reviews, CI"},
        "details": {
            "ru": "Агент уверенно работает с git и GitHub: смотрит состояние репозитория одной командой, ведёт ветки, "
                  "синхронизируется с удалённым репозиторием, разрешает конфликты слияния, ищет по истории (log -S, "
                  "blame, bisect), безопасно откатывает изменения и работает с GitHub через gh — issues, PR, ревью "
                  "и комментарии, релизы, Actions. Команды, которые подвесили бы агента (редактор, пейджер, запрос "
                  "пароля), автоматически исправляются через `PreToolUse` overwrite (`GIT_EDITOR=true`, `--no-edit`, "
                  "`GIT_TERMINAL_PROMPT=0`) или останавливаются с рабочей формой, а незаконченный rebase или merge и "
                  "маркеры конфликтов не остаются после хода. Команды: /git (справочник и снимок состояния), /commit, "
                  "/pr, /pr-comments (замечания ревью в PR с диффом, ответ и resolve), /fix-issue, /ci (ждёт CI после "
                  "пуша без опроса и достаёт логи падений), /release (поднятие версии, заметки, тег и GitHub-релиз), "
                  "/git-init (репозиторий с безопасным первым коммитом). Нужен git; для GitHub — gh с выполненным gh auth login.",
            "en": "The agent handles git and GitHub with confidence: one command shows the repository's state; it "
                  "manages branches, syncs with the remote, resolves merge conflicts, searches history (log -S, "
                  "blame, bisect), undoes changes safely and works with GitHub through gh — issues, PRs, reviews and "
                  "comments, releases, Actions. Commands that would fail or hang on Antigravity's terminal are "
                  "auto-fixed via `PreToolUse` overwrite (`GIT_EDITOR=true`, `--no-edit`, `GIT_TERMINAL_PROMPT=0`) or "
                  "stopped with the working form, and an unfinished rebase or merge or leftover conflict markers don't "
                  "survive its turn. Commands: /git (playbook and state snapshot), /commit, /pr, /pr-comments (PR review "
                  "threads with diff hunks, reply and resolve), /fix-issue, /ci (waits for CI after a push without polling "
                  "and pulls failure logs), /release (version bump, release notes, tag and GitHub release), /git-init "
                  "(a repository with a safe first commit). Needs git; for GitHub, gh after gh auth login.",
        },
        "skills": ["git", "git-init", "commit", "pr", "pr-comments", "fix-issue", "ci", "release"],
        "rules": ["GIT.md"],
        "off": {"autoCi": False, "gitChecks": False},
        "needs": ["git", "gh?"],
    },
    {
        "id": "lsp",
        "title": {"ru": "Навигация по коду (LSP)", "en": "Code navigation (LSP)"},
        "summary": {"ru": "где определено, кто вызывает, все места, тип — от языкового сервера, а не по rg",
                    "en": "definitions, callers, every use, types — from the language server, not rg"},
        "details": {
            "ru": "Агент спрашивает языковой сервер проекта, а не ищет текстом: где определён символ, все места, где он "
                  "используется (и в какой функции каждое), кто вызывает функцию — на несколько уровней вверх — и что "
                  "вызывает она, тип и сигнатуру, оглавление большого файла, символ по имени, реализации, ошибки "
                  "типов, переименование сразу во всех файлах. Перед сменой сигнатуры, переименованием или удалением "
                  "агент сначала находит все места; места, которые сервер связать не смог (импорт через sys.path, "
                  "устаревший индекс), называются отдельно — их агент проверяет сам. После правки уже запущенный "
                  "сервер сообщает о новых ошибках типов. Серверы ставятся отдельно: pyright (Python), sourcekit-lsp "
                  "(Swift; для приложения Xcode — xcode-build-server), kotlin-lsp (Kotlin), typescript-language-server, "
                  "gopls, rust-analyzer, clangd и другие — `/lsp servers` подскажет команду. Сервер работает в фоне и "
                  "останавливается после 10 минут без запросов (pyright — до 1–2 ГБ памяти, kotlin-lsp — 1–3 ГБ). "
                  "Команда /lsp.",
            "en": "The agent asks the project's language server instead of searching text: where a symbol is defined, "
                  "every place it is used (and the function each one is in), who calls a function — several levels "
                  "up — and what it calls, its type and signature, a big file's outline, a symbol by name, "
                  "implementations, type errors, and a rename across all files at once. Before changing a signature, "
                  "renaming or deleting, the agent finds every place first; places the server couldn't link (imports "
                  "through sys.path, a stale index) are named separately for the agent to check. After an edit, a "
                  "server that is already running reports the new type errors. Servers are installed separately: "
                  "pyright (Python), sourcekit-lsp (Swift; an Xcode app also needs xcode-build-server), kotlin-lsp "
                  "(Kotlin), typescript-language-server, gopls, rust-analyzer, clangd and more — `/lsp servers` "
                  "gives the command. A server runs in the background and stops after 10 minutes without requests "
                  "(pyright takes up to 1–2 GB of memory, kotlin-lsp 1–3 GB). Command /lsp.",
        },
        "skills": ["lsp"],
        "rules": ["LSP.md"],
        "off": {"lspChecks": False},
        "needs": ["lsp"],
    },
    {
        "id": "deploy",
        "title": {"ru": "Деплой, БД и окружение", "en": "Deploy, DB and environment"},
        "summary": {"ru": "риски деплоя, миграций и БД (/deploy, /db); .env и диагностика Docker (/env)",
                    "en": "deploy, migration & DB checks (/deploy, /db); .env parity & Docker diagnostics (/env)"},
        "details": {
            "ru": "/deploy показывает, что именно уйдёт на сервер и чем это рискованно (миграции, конфиги, "
                  "зависимости), делает бэкап, деплоит, проверяет, что сервис жив, и откатывает при ошибке (держа "
                  "замок от двух одновременных деплоев и проверяя CI). /db — безопасный read-only инспектор схем "
                  "PostgreSQL/SQLite/Redis, EXPLAIN в BEGIN READ ONLY и статический анализ миграций (DROP, блокировки "
                  "таблиц, индексы без CONCURRENTLY) без утечки паролей из .env. /env — сверка .env ↔ .env.example "
                  "без печати секретов и неблокирующий 2-секундный сокет-пинг Docker и портов.",
            "en": "/deploy shows what exactly goes to the server and what is risky about it (migrations, configs, "
                  "dependencies), takes a backup, deploys, checks the service is alive and rolls back on failure (holding "
                  "a deploy lock and checking CI). /db inspects PostgreSQL/SQLite/Redis schemas read-only, runs EXPLAIN "
                  "inside BEGIN READ ONLY, and scans migrations for dangerous locks/drops without printing .env secrets. "
                  "/env checks .env ↔ .env.example parity without leaking values and runs a 2s non-hanging Docker socket "
                  "and port health probe.",
        },
        "skills": ["deploy", "db", "env"],
    },
    {
        "id": "web",
        "title": {"ru": "Проверка веб-интерфейса", "en": "Web UI checks"},
        "summary": {"ru": "скриншоты страниц на телефоне и ноутбуке, ошибки консоли",
                    "en": "page screenshots at phone and laptop width, console errors"},
        "details": {
            "ru": "/ui-check снимает страницы в headless-браузере на ширине телефона и ноутбука, собирает ошибки "
                  "консоли и тяжёлые эффекты. Изменив вёрстку, агент сам смотрит на результат, а не считает работу "
                  "сделанной, раз синтаксис в порядке. Нужен Chrome, Chromium, Edge или Brave.",
            "en": "/ui-check takes headless-browser screenshots of pages at phone and laptop width and collects "
                  "console errors and heavy effects. After changing a layout the agent looks at the result itself "
                  "instead of calling it done because the syntax is fine. Needs Chrome, Chromium, Edge or Brave.",
        },
        "skills": ["ui-check"],
        "needs": ["browser"],
    },
    {
        "id": "mobile",
        "title": {"ru": "Мобильные приложения", "en": "Mobile apps"},
        "summary": {"ru": "агент сам запускает приложение в iOS-симуляторе или Android-эмуляторе и смотрит",
                    "en": "the agent runs the app in the iOS simulator or Android emulator and looks"},
        "details": {
            "ru": "/mobile: агент сам ставит и запускает сборку в iOS-симуляторе или Android-эмуляторе, смотрит "
                  "скриншоты, видит список элементов на экране, нажимает по подписям, листает, вводит текст, читает "
                  "ошибки и падения приложения, проверяет тёмную тему и крупный шрифт. Изменив экран, не закончит, "
                  "пока не посмотрел на него. Нужны Xcode (iOS, только macOS) и/или Android SDK.",
            "en": "/mobile: the agent installs and launches the build in the iOS simulator or the Android emulator, "
                  "looks at screenshots, lists what is on the screen, taps by label, swipes, types, reads the app's "
                  "errors and crashes, checks dark mode and large text. After changing a screen it isn't done until "
                  "it has looked at it. Needs Xcode (iOS, macOS only) and/or the Android SDK.",
        },
        "skills": ["mobile"],
        "needs": ["mobile"],
    },
    {
        "id": "usage",
        "title": {"ru": "Расход токенов, панель и отчёты", "en": "Token usage, Control Center and reports"},
        "summary": {"ru": "токены и стоимость, панель «Центр управления», /context, /insights, /standup, /export, /schedule-task",
                    "en": "tokens and cost, Control Center sidecar, /context, /insights, /standup, /export, /schedule-task"},
        "details": {
            "ru": "/usage: токены, кэш, заполнение контекста и стоимость по ценам API, разбивка по ходам и "
                  "субагентам, HTML-отчёт. Боковая панель 2.0 «Центр управления» показывает расходы, 5-часовой и "
                  "недельный лимиты Antigravity, активных агентов в проектах, задачи моста Claude ↔ Antigravity, "
                  "запущенные LSP-серверы (с RAM и остановкой), переключатели настроек и журнал guard. /context — что "
                  "занимает окно контекста; /insights — статистика хуков и guard; /standup — сводка за день/неделю; "
                  "/export — разговор в Markdown со скрытыми секретами; /schedule-task — квото-зависимые расписания Antigravity (включаются в панели Automations).",
            "en": "/usage: tokens, cache, context fill and the API-price cost, broken down by turn and subagent, with "
                  "an HTML report. The Control Center 2.0 side panel shows usage, Antigravity's 5-hour and weekly limits, "
                  "active workspace agents, Claude ↔ Antigravity tasks, running LSP daemons (RAM + Stop), coaching "
                  "toggles, and the guard feed. /context shows what fills context; /insights reports hook/guard stats; "
                  "/standup — work across projects; /export — redacted Markdown export; /schedule-task — quota-gated Antigravity automations (enabled in the Automations dashboard).",
        },
        "skills": ["usage", "standup", "export", "context", "insights", "schedule-task"],
        "sidecars": ["usage"],
    },
    {
        "id": "claude",
        "title": {"ru": "Мост Claude Code ↔ Antigravity", "en": "Claude Code ↔ Antigravity bridge"},
        "summary": {"ru": "двусторонний мост: Claude Code ↔ Antigravity по оставшимся лимитам",
                    "en": "two-way bridge: Claude Code ↔ Antigravity based on remaining limits"},
        "details": {
            "ru": "Навык antigravity для Claude Code (~/.claude/skills) и команда /claude в Antigravity: оба умеют "
                  "передавать подзадачи друг другу, сверяя квоты обеих сторон. Claude Code отдаёт лёгкую и объёмную "
                  "рутину агентам Antigravity (Gemini 4 Hydrogen, Neon, Carbon, Claude) и переговаривается с ними по ходу "
                  "работы. Задачи сами продолжаются после ошибок Antigravity, ждут в очереди при лимите одновременных "
                  "задач, идут сериями в одной беседе; отчёт показывает фоновые команды с PID и открытые картинки, "
                  "а цену задачи видно до запуска. Агент Antigravity через /claude (claude_task.py) может запустить `claude -p` на подзадачу "
                  "и принять diff и отчёт (только по вашей просьбе или при полном доступе). Ставится, только если "
                  "на компьютере есть Claude Code.",
            "en": "The antigravity skill for Claude Code (~/.claude/skills) plus /claude in Antigravity: both sides "
                  "can hand sub-tasks to each other based on remaining limits. Claude Code sends light and bulky "
                  "routine work to Antigravity agents (Gemini 4 Hydrogen, Neon, Carbon, Claude) and talks while they run. "
                  "Tasks resume by themselves after Antigravity's errors, wait in a queue under a limit of tasks at once "
                  "and run as series in one conversation; reports show background commands with PIDs and the images "
                  "opened, and a task's cost shows before it starts. An "
                  "Antigravity agent can use /claude (claude_task.py) to launch `claude -p` on a sub-task and review "
                  "its diff and report (only on your request or under full access). Installed only when Claude Code is "
                  "on the computer.",
        },
        "skills": ["claude"],
        "claude": True,
        "needs": ["claude"],
    },
]

IDS = [m["id"] for m in MODULES]
BY_ID = {m["id"]: m for m in MODULES}
