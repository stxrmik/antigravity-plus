---
name: deploy
description: >-
  Safe deploy of the project to its servers: what goes out since the last deploy and how risky it is (data, config,
  dependencies, infrastructure), tests, a backup and a rollback plan, the deploy itself, a health check afterwards
  and a rollback if it broke; one deploy at a time per project, even with several agents. Use when the user asks to
  deploy («задеплой», «залей на сервер», «выкати», «пушь на прод», /deploy), or asks whether it's safe («если сейчас
  запушим, ничего не поломается?» — then only the check). The deploy itself never on your own unless the user granted
  full access.
---

# /deploy — check, deploy, verify, roll back

> **Only on the user's request, or under full access** — for the deploy itself. The check alone («что уйдёт на
> сервер», «ничего не сломается?») is read-only: run it whenever it helps.

Helper: `python3 "<skill_dir>/scripts/deploy.py"` (run with `BypassSandbox: true`: it keeps its state in
`~/.gemini/antigravity-plus/deploys/`). Your conversation id is the `<id>` segment of your artifact directory path.

## 1. What goes out
```bash
python3 "<skill_dir>/scripts/deploy.py" plan --workspace "<ws>" --conversation <your id>
```
It lists what changed since the last recorded deploy (by file snapshot, so it works without git too), sorted by risk:
data / schema, configuration / env, dependencies, infrastructure, code. It also finds how the project deploys (deploy
scripts, compose, CI) and shows the last deploys and whether another agent is deploying right now.

## 2. How this project deploys
Find the real procedure, never guess: `AGENTS.md`, the project memory
(`memory.py search "deploy|деплой|сервер" --workspace "<ws>"`), the deploy scripts the plan found. If it's still
unclear, ask the user once and save the answer to the project memory (type `project`) for next time.
The user's restrictions apply: after «не пушь» / «не деплой» there is no deploy until they lift it.

## 3. Before deploying
- Run the project's tests and build. If they fail, stop and report: don't deploy broken code.
- For a risky change (data, config, dependencies), say in one line what can break and how you'll check it.
- Take the lock: `deploy.py lock --workspace "<ws>" --conversation <your id>`. If another agent holds it, wait and retry,
  or coordinate with it through send_message.
- Back up what the deploy can damage: the database (a dump, or a copy of the SQLite file / volume) and the configs
  on the server (a copy into a dated backup folder). Note the current version (commit, image tag) to roll back to.

## 4. Deploy and verify
- Deploy with the project's own command.
- Health check right after: service and container status, logs since the deploy (errors), the main endpoints and
  pages (`curl`), on every server or node. Read-only commands over ssh are fine.
- **If something broke:** roll back right away (the previous version or the backup), check again, and tell the user
  what happened, what you rolled back and why.

## 5. Finish
- `deploy.py record --workspace "<ws>" --note "<what went out>" --conversation <your id>` (also releases the lock;
  if you stop without deploying, run `deploy.py unlock …`).
- If you learned something about the servers or the procedure, save it to the project memory.
- Report in 3–5 lines: what went out, checks before and after, where the backup is, how to roll back.

## Rules
- Never print secrets from `.env` / `server.env` into the conversation; use them in commands without showing them.
- Don't force-push, drop data or skip failing tests to get a deploy through.
- One deploy at a time per project: always take the lock, and record or unlock at the end.
