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
                  "разрешения. Bypass Mode: один раз «/bypass» в чате — и агенты во всех разговорах работают без "
                  "запросов разрешения, пока вы не напишете «/bypass off»; спрашивают только о системном (sudo, "
                  "системные папки, диски, автозапуск, ключи, права агента). В начале разговора агент получает снимок git и правила проекта — AGENTS.md, а также "
                  "CLAUDE.md с @импортами и .claude/rules, — а ваши указания («ничего не пушь») снова видит после "
                  "каждого сжатия контекста, так что они не тонут в длинных разговорах. Правила работы: понять запрос, "
                  "спланировать, сделать, проверить; несколько агентов в одном проекте не затирают правки друг друга. "
                  "Команды: /init (правила проекта в AGENTS.md), /remember, /settings, /doctor, /hooks, /update (новая версия "
                  "одной командой: раз в сутки агент сам скажет, что она вышла; откат одной командой). "
                  "Обязательна: остальные модули работают поверх неё.",
            "en": "The guard stops what is irreversible or dangerous (rm -rf, git reset --hard, force-push, sudo, "
                  "curl | sh, DROP TABLE…), keeps secrets out of the conversation (cat .env) and never lets the agent "
                  "change its own permissions. Bypass Mode: type «/bypass» once and agents in every conversation work "
                  "without permission prompts until «/bypass off»; only system-level actions still ask (sudo, system "
                  "folders, disks, autostart, keys, the agent's permissions). At the start of a conversation the agent gets a git snapshot and the "
                  "project's rules — AGENTS.md, plus CLAUDE.md with its @imports and .claude/rules — and sees your "
                  "instructions («don't push anything») again after every context compaction, so they don't get lost "
                  "in long conversations. Working rules: understand the request, plan, do, verify; several agents on "
                  "one project don't overwrite each other's edits. Commands: /init (project rules in AGENTS.md), "
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
                  "сборкой, а после крупной правки сам запускает ревью. Команды: /review (два ревьюера с "
                  "перепроверкой находок), /security-review (аудит с проверкой эксплуатируемости), /simplify; "
                  "субагенты code-reviewer, security-auditor, debugger, test-writer. Без модуля правки не "
                  "проверяются, и агент может остановиться на полпути.",
            "en": "Every edit is checked at once: Python, JS/TS, Swift, shell, JSON, YAML and plist syntax plus the "
                  "project's linters (ruff, eslint, swiftlint, shellcheck) — an error reaches the agent at the next "
                  "step, not three steps later. The agent can't end its turn while task.md has open items or changed "
                  "code hasn't been run through tests or a build, and it reviews big changes on its own. Commands: "
                  "/review (two reviewers, findings re-checked), /security-review (an audit that checks "
                  "exploitability), /simplify; subagents code-reviewer, security-auditor, debugger, test-writer. "
                  "Without it edits aren't checked and the agent may stop halfway.",
        },
        "skills": ["review", "security-review", "simplify"],
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
                  "«задеплоил», пути, номера строк, ссылки, версии и цены должны чем-то подтверждаться — иначе агент "
                  "возвращается проверить. Команда, которая только печатает «ok», проверкой не считается. На вопрос "
                  "о фактах агент ищет источник и ссылается на него. На возражение он сначала перепроверяет, а не "
                  "отвечает «Вы абсолютно правы»; ваша догадка о причине — гипотеза, а не диагноз. Правила FACTS.md и "
                  "OBJECTIVE.md.",
            "en": "The agent's final report is held to the conversation: «tests pass», «N passed», «CI is green», "
                  "«deployed», paths, line numbers, links, versions and prices need something behind them — otherwise "
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
                  "часть реализации в своих файлах. Пока они работают, агент их не опрашивает и не простаивает; "
                  "отчёты перепроверяет, прежде чем на них опираться. Команда /parallel с шаблонами заданий. Тратит "
                  "больше квоты в час, зато заканчивает быстрее.",
            "en": "The agent hands independent parts of a task to subagents in one step and keeps working on its own "
                  "part instead of waiting: explorer searches the code, researcher the docs and APIs (both on a fast "
                  "model), worker builds a part in its own files. Meanwhile it neither polls nor idles, and it checks "
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
        "title": {"ru": "Скорость и контекст", "en": "Speed and context"},
        "summary": {"ru": "меньше лишних шагов: песочница, бюджет контекста, циклы ошибок",
                    "en": "fewer wasted steps: the sandbox, the context budget, loops of errors"},
        "details": {
            "ru": "Команды, которые падали в песочнице терминала, запоминаются по проекту и дальше сразу запускаются "
                  "вне её. Агент видит, сколько контекста уже занято, и читает большие файлы по частям; после трёх "
                  "неудачных команд подряд меняет подход, а не повторяет одно и то же. Только подсказки агенту, "
                  "своих команд нет.",
            "en": "Commands that failed inside the terminal sandbox are remembered per project and run outside it "
                  "from then on. The agent sees how full its context is and reads big files in parts; after three "
                  "failing commands in a row it changes its approach instead of repeating itself. Hints to the agent "
                  "only, no commands of its own.",
        },
        "off": {"speedHints": False},
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
                  "пароля), заменяются неинтерактивными, а незаконченный rebase или merge и маркеры конфликтов не "
                  "остаются после хода. Команды: /git (справочник и снимок состояния), /commit, /pr, /fix-issue, "
                  "/ci (ждёт CI после пуша без опроса и достаёт логи падений), /git-init (репозиторий с безопасным "
                  "первым коммитом). Нужен git; для GitHub — gh с выполненным gh auth login.",
            "en": "The agent handles git and GitHub with confidence: one command shows the repository's state; it "
                  "manages branches, syncs with the remote, resolves merge conflicts, searches history (log -S, "
                  "blame, bisect), undoes changes safely and works with GitHub through gh — issues, PRs, reviews and "
                  "comments, releases, Actions. Commands that would hang the agent (an editor, a pager, a password "
                  "prompt) are replaced with non-interactive ones, and an unfinished rebase or merge or leftover "
                  "conflict markers don't survive its turn. Commands: /git (playbook and state snapshot), /commit, "
                  "/pr, /fix-issue, /ci (waits for CI after a push without polling and pulls failure logs), "
                  "/git-init (a repository with a safe first commit). Needs git; for GitHub, gh after gh auth login.",
        },
        "skills": ["git", "git-init", "commit", "pr", "fix-issue", "ci"],
        "rules": ["GIT.md"],
        "off": {"autoCi": False, "gitChecks": False},
        "needs": ["git", "gh?"],
    },
    {
        "id": "deploy",
        "title": {"ru": "Деплой", "en": "Deploy"},
        "summary": {"ru": "что уйдёт на сервер и чем рискованно; бэкап, проверка, откат",
                    "en": "what goes to the server and the risks; backup, checks, rollback"},
        "details": {
            "ru": "/deploy показывает, что именно уйдёт на сервер и чем это рискованно (миграции, конфиги, "
                  "зависимости), делает бэкап, деплоит, проверяет, что сервис жив, и откатывает при ошибке. Держит "
                  "замок, чтобы два агента не деплоили один проект одновременно, и не выкатывает коммит с красным CI. "
                  "Проверку агент запускает сам, сам деплой — только по вашей просьбе.",
            "en": "/deploy shows what exactly goes to the server and what is risky about it (migrations, configs, "
                  "dependencies), takes a backup, deploys, checks the service is alive and rolls back on failure. It "
                  "holds a lock so two agents never deploy one project at once, and won't ship a commit with red CI. "
                  "The agent runs the check on its own; the deploy itself only on your request.",
        },
        "skills": ["deploy"],
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
        "title": {"ru": "Расход токенов и отчёты", "en": "Token usage and reports"},
        "summary": {"ru": "токены и стоимость, панель с лимитами, /standup, /export",
                    "en": "tokens and cost, a live panel with limits, /standup, /export"},
        "details": {
            "ru": "/usage: токены, кэш, заполнение контекста и стоимость по ценам API, разбивка по ходам и "
                  "субагентам, HTML-отчёт. Боковая панель «Расход токенов» обновляется каждые 3 секунды и показывает "
                  "5-часовой и недельный лимиты Antigravity. /standup — что сделано за день или неделю по всем "
                  "проектам, /export — разговор в Markdown со скрытыми секретами.",
            "en": "/usage: tokens, cache, context fill and the API-price cost, broken down by turn and subagent, with "
                  "an HTML report. The side panel «Расход токенов» refreshes every 3 seconds and shows Antigravity's "
                  "5-hour and weekly limits. /standup — what got done today or this week across projects; /export — "
                  "a conversation as Markdown with secrets hidden.",
        },
        "skills": ["usage", "standup", "export"],
        "sidecars": ["usage"],
    },
    {
        "id": "claude",
        "title": {"ru": "Мост Claude Code → Antigravity", "en": "Claude Code → Antigravity bridge"},
        "summary": {"ru": "Claude Code сам отдаёт задачи агентам Antigravity, выбирая модель по лимитам",
                    "en": "Claude Code hands tasks to Antigravity agents, picking the model by the limits"},
        "details": {
            "ru": "Навык antigravity для Claude Code (~/.claude/skills): Claude Code сам отдаёт агентам Antigravity "
                  "лёгкие и объёмные задачи — на Gemini 4 Hydrogen, Gemini 4 Carbon или Claude в Antigravity, выбирая "
                  "модель по оставшимся лимитам обеих сторон, — и проверяет результат. По ходу работы они "
                  "переговариваются: агент спрашивает Claude Code и сообщает ему важное, Claude Code отвечает и "
                  "поправляет агента, не дожидаясь конца. Работа агента идёт из квоты Antigravity, а не из лимита "
                  "Claude Code. Ставится, только если на компьютере есть Claude Code.",
            "en": "The antigravity skill for Claude Code (~/.claude/skills): Claude Code hands light and bulky tasks "
                  "to Antigravity agents — on Gemini 4 Hydrogen, Gemini 4 Carbon or Claude in Antigravity, picking "
                  "the model from both sides' limits — and checks the result. They talk while they work: the agent "
                  "asks Claude Code and tells it what matters, Claude Code answers and steers it without waiting for "
                  "the end. The agent's work comes out of the Antigravity quota instead of Claude Code's limit. "
                  "Installed only when Claude Code is on the computer.",
        },
        "claude": True,
        "needs": ["claude"],
    },
]

IDS = [m["id"] for m in MODULES]
BY_ID = {m["id"]: m for m in MODULES}
