---
name: hooks
description: >-
  Sets up the project's own automation in Antigravity (like Claude Code's hooks in settings.json): run tests or a
  formatter after edits, block changes to certain files, keep a checklist before finishing, add a reminder before
  each step, as .agents/hooks.json plus small scripts in the repository. Use when the user asks for something to
  happen automatically («пусть после каждой правки…», «всегда перед завершением…», «не давай трогать…», /hooks).
  Not on your own unless the user granted full access.
---

# /hooks — automation for this project

> **Only on the user's request, or under full access:** it changes the project's configuration.

Antigravity runs hooks from `.agents/hooks.json` at the project root (and from plugins). A hook is a command that
gets a JSON payload on stdin and prints a JSON answer on stdout. The command runs via `sh -c` from the folder that
holds `hooks.json`.

## Events and answers
| Event | When | Answer (stdout) |
|---|---|---|
| `PreToolUse` (with `matcher`: tool name regex, e.g. `run_command`, `write_file\|edit_file`) | before a tool runs; payload has `toolCall {name, args}` (`args.Path`, `args.CommandLine`) | `{"decision": "allow"}` = no objection; `"deny"` + `"reason"` blocks (the agent sees the reason); `"ask"` / `"force_ask"` asks the user |
| `PostToolUse` (with `matcher`) | after a tool ran; payload adds `error` | `{}` |
| `PreInvocation` | before every model call | `{"injectSteps": [{"ephemeralMessage": "…"}]}` or `{}` |
| `Stop` | the agent ends its turn; payload has `terminationReason` (`NO_TOOL_CALL` = normal), `fullyIdle`, `executionNum` | `{"decision": "continue", "reason": "…"}` sends it back to work; `{}` lets it stop |
| `SessionStart` | a conversation starts | `{"injectSteps": [{"systemMessage": {"systemMessage": "…"}}]}` |

Every payload also has `conversationId`, `workspacePaths`, `transcriptPath`, `lastUserInput`.
**Pitfall:** for `PreToolUse`, an empty `{}` means **deny**. A hook that should only watch must print
`{"decision": "allow"}`.

## Steps
1. **Pin down the behavior** in one sentence: event, trigger, effect. If it's ambiguous, ask one question.
2. **Prefer the cheap event.** A full test run belongs in `Stop` (once per turn), not `PostToolUse` (every edit).
   A formatter on the changed file belongs in `PostToolUse`.
3. **Write the script** in `.agents/hooks/<name>.sh` or `.py`. Read the payload from stdin, act, print valid JSON, and
   exit 0 even on failure: a crashing hook must never block the agent. Keep it fast and set a `timeout` (seconds).
4. **Register it**: create or extend `.agents/hooks.json` (keep the other hooks), e.g.
   ```json
   {
     "project-format": {
       "PostToolUse": [{"matcher": "write_file|edit_file",
                        "hooks": [{"type": "command", "command": "sh hooks/format.sh", "timeout": 20}]}]
     },
     "project-tests-before-done": {
       "Stop": [{"type": "command", "command": "python3 hooks/tests_gate.py", "timeout": 300}]
     }
   }
   ```
   Paths in `command` are relative to `.agents/`. Add `"enabled": false` to switch a hook off.
5. **Test it locally** before reporting: pipe a sample payload into the script and check the JSON, e.g.
   `echo '{"toolCall": {"name": "edit_file", "args": {"Path": "app/main.py"}}}' | sh .agents/hooks/format.sh`.
6. **Report** in 2–3 lines: what runs when, how to switch it off, that it applies to new conversations.

## Recipes
- **Format after an edit** (PostToolUse): take `args.Path`; if it's a `.py` file, run `ruff format`, for `.ts`/`.js`
  run `npx prettier --write`. Print `{}`. The file changes after the agent's edit, so it has to re-read it before the
  next edit (Antigravity Plus asks it to).
- **Tests must pass before finishing** (Stop): if `terminationReason` is `NO_TOOL_CALL`, run the test command. On
  failure print `{"decision": "continue", "reason": "tests fail: <last lines>"}`. Count attempts in a file under
  `/tmp` keyed by `conversationId` and give up after 2, so the agent can't loop forever.
- **Protect files** (PreToolUse on `write_file|edit_file`): for paths matching `migrations/` or `*.lock`, print
  `{"decision": "deny", "reason": "<why and what to do instead>"}`; otherwise `{"decision": "allow"}`.
- **A reminder before each step** (PreInvocation): print an `ephemeralMessage`. Keep it short: it costs tokens on
  every call.
