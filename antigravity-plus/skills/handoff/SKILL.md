---
name: handoff
description: >-
  Hands work over to the next conversation (or Claude Code) and picks it up there: a handoff note in the project
  memory with the goal, what's done, what's left, decisions, the user's restrictions and how to verify. Use when
  the user asks («сохрани прогресс», «продолжим потом», /handoff) or says «продолжи», «на чём остановились» in a
  new conversation, and on your own when the user wraps up with work left or the conversation was compacted twice
  and a lot remains.
---

# /handoff — continue in another conversation

Helper: `python3 "<skill_dir>/scripts/handoff.py"`. It only reads data, but some of it lives outside the workspace
(Antigravity's conversation index and transcripts), so run it with `BypassSandbox: true`.

Your conversation id is the `<id>` segment of your artifact directory path (`…/brain/<id>/`).
Paths may contain spaces (e.g. `~/Downloads/Antigravity Plus`): always quote them in commands.

## Save (default: /handoff, "сохрани прогресс", "продолжим потом")

On your own: when the user wraps up with work left («на сегодня всё», «потом доделаем»), or the conversation was compacted twice and a lot remains, save the note without asking, then tell the user in one line where it is.

1. **Collect the facts:**
   ```bash
   python3 "<skill_dir>/scripts/handoff.py" collect --conversation <your id> --workspace "<workspace-root>"
   ```
   It prints the user's messages, the restrictions still in force, the files written or edited, commits and
   deploys run, git state, the conversation's `task.md` checklist, the existing handoff notes, and the path to
   save to. If the facts are thin, check the live state yourself: `git status`, `git log --oneline -5`, which
   tests you ran.

2. **Write the note** with `write_file` to the suggested path. Shorten the topic part of the name (e.g.
   `handoff-2026-11-04-checkout-redesign.md`). If this conversation continued an existing handoff note, update that
   note instead of adding another. Format:
   ```markdown
   ---
   name: handoff-<date>-<topic>
   description: "Передача <ДД.ММ>: <тема> — <что готово / что осталось, одной фразой>"
   metadata:
     type: project
     conversation: <your conversation id>
   ---
   ## Цель
   <what the user wants, close to their words>

   ## Сделано
   - <result> — <files, commit, deployed or not>

   ## Осталось
   - [ ] <next concrete step: file, function, command>

   ## Решения и ограничения
   - <decision and why>
   - <the user's restriction, e.g. «не пушить на сервер без команды»: still in force or lifted>

   ## Как проверить
   - <commands, URLs, test names>

   ## Источник
   Antigravity, разговор <id>, <date>. Подробности: `handoff.py collect --conversation <id>`.
   ```
   - `name:` is the file name without `.md`.
   - Write it in the user's language. Be specific: names of files, functions, commands, servers.
   - No secrets, passwords, tokens or keys. Refer to where they live (e.g. "пароль в .env").
   - Don't edit `MEMORY.md`: the index line is added automatically.

3. **Reply** in 2–3 lines:
   `💾 Передача сохранена: [<file>](<path>)`, then one line on what's left, then: «Откройте новый разговор и
   напишите «продолжи» — я начну с этого места. Claude Code эту заметку тоже увидит.»

## Resume ("продолжи", "на чём остановились", "доделай" in a new conversation)

1. **Find the starting point.** The session context ("Continuing earlier work in this project") marks it with ▶:
   a handoff note, or a conversation when it's newer than the notes. Antigravity conversations marked "running now"
   are still working, so don't continue them. Without the block, run `handoff.py recent --workspace "<ws>"`.
   If the user hinted at a topic and it fits another item better, take that one. If it's unclear, ask with the
   options.
2. **Read it.** For a note, read the file. For a conversation, run
   `handoff.py collect --conversation <id> --workspace "<ws>"`; it works for Antigravity and Claude Code ids. Don't
   read whole transcripts.
3. **Check the current state:** `git status` and `git log`, plus the files and servers the note names. Things
   may have changed since the note was written.
4. **Tell the user in 2–4 lines** what you're picking up: the goal, what's left, the restrictions in force. Then
   continue with the first open item, or ask if it needs their decision.
5. The note's restrictions («не пушить без команды») apply here too, until the user lifts them.
6. **Keep the note current.** When part of the work is done, update «Сделано» / «Осталось». When everything is
   done, delete the note
   (`python3 "<skill_dir>/../memory/scripts/memory.py" forget <name> --scope project --workspace "<ws>"`) and say so
   in one line.
