---
name: git-init
description: >-
  Creates a git repository for the project safely: a .gitignore for its stack, secrets and huge files kept out, and
  a first commit with the current state, so every later change can be reviewed (git diff) and undone. Run it
  yourself, without asking, before your first change in a project folder that has no git repository (the session
  context says «not a git repository»), and when the user asks («создай репозиторий», «инициализируй git»,
  /git-init). Helper: scripts/repo_init.py --path <project folder>.
---

# /git-init — a git repository for the project

Run it on your own before your first change in a project without git: run the command, then go on with the
user's task in the same turn. Don't stop to announce it; mention it in your final report («Создал git-репозиторий:
все правки можно посмотреть и откатить»). Don't run it when the user only asked a question, when they said not to
use git here, or from a subagent.

1. Run it:
   ```bash
   python3 "<skill_dir>/scripts/repo_init.py" --path "<project folder>"
   ```
   The project folder is the workspace. If the workspace holds several projects, use the folder of the one you're
   working on. Keep the quotes: paths may contain spaces.
   - It creates the repository and a `.gitignore` (an existing one only gets the rules it lacks).
   - It commits the current state as "Initial snapshot". Files with secrets and files over 50 MB stay out of
     that commit.
   - Nothing leaves the computer.
2. **If it refuses** (exit 2: home folder, a folder of projects, another version control system), don't force it.
   Say why in one line and work without git.
3. **If the commit failed** because git has no name or email, ask the user for them once. Don't invent them.
4. In your report, mention what it left out (secrets, huge files) and that nothing was pushed.

Never add a remote or push on your own: that's `/pr`, or the user's call.
