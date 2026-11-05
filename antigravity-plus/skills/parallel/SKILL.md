---
name: parallel
description: >-
  Run a task as parallel work: split it into independent parts, launch subagents (explorer, researcher, worker,
  test-writer, code-reviewer, security-auditor) or a run_workflow fan-out in one step, keep working on the critical
  path yourself, then check and merge their results. Use on your own when a task has 2+ independent parts that each
  take more than a few tool calls (research + code, several modules or pages, implementation + tests + review, a broad
  investigation), and when the user asks to parallelize («распараллель», «сделай быстрее», «подключи субагентов»,
  /parallel).
---

# /parallel — split the work, keep everyone busy

Subagents start at once and run in the background; each one messages you when it's done. You finish sooner only if
you keep working while they do.

## 1. Map the work
List the parts of the task and, for each: what it needs first (another part's result, a decision, an interface) and
which files it touches.
- **Critical path** — the part everything else waits on (the core change, a shared interface, the data model). It's
  yours: start it right after the launch.
- **Independent parts** — they don't need what you do next, have their own files and take more than a few tool
  calls. They go to subagents.
- **Keep for yourself** — small items, anything on the same files as your part, decisions the user cares about,
  outward steps (push, deploy).

If nothing is independent, don't force it: do the task yourself, and still put independent tool calls (reads,
searches, commands) into one step.

## 2. Pick who does each part
| Part | Who | `Model` |
|---|---|---|
| where is X, how does Y flow, which files use Z | `explorer`, one per area | `flash` |
| docs, APIs, error messages, versions, formats, deeplinks | `researcher` | `flash` (`inherit` for hard questions) |
| a module, page, endpoint or script in its own files | `worker` | `inherit` |
| tests for code whose interface is settled | `test-writer` | `inherit` |
| review of a finished change | `code-reviewer`, `security-auditor` | `inherit` |
| a failure you can't explain in a few steps | `debugger` | `inherit` |
| a long build, test suite or deploy check | a background `run_command` | — |
| 4+ similar pieces (each file, module, bug, page) | `run_workflow` (built-in skill `run-workflow`) | per agent |

Keep `Workspace` at its default (`inherit`): agents that edit at the same time get disjoint files, and Antigravity
Plus stops anyone from overwriting a file another agent changed.

## 3. Write the briefs
A subagent starts with nothing but its prompt. Use this shape (English is fine; quote the user's words when they
matter):
```
Goal: <the part, and how it fits the whole task — one or two sentences>
You own: <files/folders you may change>. Other agents are changing <…> right now — don't touch them.
Context: <what's decided: interfaces, names, data formats; the user's constraints («не пушь», «без новых зависимостей»)>
Done when: <the observable result + the check to run>
Report: Done / Changed (file — what) / Verified (command → result) / Needs from others
```
For `explorer` and `researcher`: the exact questions, what the answer is for, and the breadth (quick / medium / very
thorough).

## 4. Launch, then work
- Put all `invoke_subagent` calls in ONE step, together with your own first reads if they're independent.
- Mark the items in task.md: `- [/] Research INCY deeplinks — researcher`.
- Tell the user in one line who does what, e.g. «Запустил: researcher — формат ссылок INCY, explorer — где
  собираются подписки; сам правлю шаблон.»
- Start your own part right away, in the same turn. Also useful while they run: read the files their results will
  land in, write the glue code against the agreed interface, run the existing tests.
- Don't poll and don't announce waiting while there's work left. When everything left needs their results, end your
  turn with one line on who you're waiting for — their message wakes you up.

## 5. Merge the results
- Use a result only after its message has arrived, and read it critically: a report isn't proof.
- `worker`, `test-writer`, `debugger`: open the changed files and run their check yourself before you build on it.
  Fix small issues yourself; for bigger ones `send_message` the same subagent — it keeps its context.
- `researcher`: when the code depends on a fact, check it against the cited source.
- Conflicts between parts (an interface drifted, two parts needed the same file): resolve them yourself, then run the
  full checks.
- Mark each item done in task.md as you merge it; kill subagents you no longer need (`manage_subagents`, Action
  `kill`).

## 6. Finish
Run the project's checks on the merged result (and `/review` for a substantial change), then report: what each part
did (one line each), how the whole was verified, what's left.

## Many similar pieces: run_workflow
Instead of a dozen manual calls, write a short workflow script (see the built-in `run-workflow` skill):
```python
files = args["files"]
reviews = await parallel(files, lambda f: agent(
    f"Review {f} for misuse of the cache API. Reply in 3 bullets or 'OK'.",
    schema={"type": "string"}, role=f"Review {f}", model="flash"))
for f, r in zip(files, reviews):
    log(f"{f}: {r}")
```
It runs in the background and reports when it ends; keep working meanwhile, as above.
