---
name: export
description: >-
  Exports a conversation to readable Markdown (prompts, answers, a line per tool call; optionally tool output and
  thinking) or copies it to the clipboard. Use when the user asks (/export, «сохрани переписку», «выгрузи
  разговор»). Not on your own unless the user granted full access.
---

# /export — conversation → Markdown

> **Only on the user's request, or under full access:** the command, the same request in their own words, or full access the user granted (rules: "Full access"). Otherwise, if you got here on your own, don't run it; offer it to the user in one line instead.

The script [export.py](scripts/export.py) reads the conversation transcript and writes Markdown.

## Steps
1. **Conversation id:** the current one is in your system context (also the `<id>` segment of your
   artifact directory path). Use another id only if the user gave one.
2. **Destination:**
   - user named a path → `--out <path>`;
   - "в буфер", "clipboard" → `--clipboard`;
   - otherwise → `--out <artifact_dir>/conversation.md`.
   - "полностью", "с выводом команд", "full" → add `--full` (tool outputs, truncated, + thinking).
3. **Run** (read-only):
   ```bash
   python3 <skill_dir>/scripts/export.py --conversation <conversation-id> --out <path> [--full] [--clipboard]
   ```
   If it reports no read access (sandbox) for a path outside your artifact directory, re-run the same
   command with `BypassSandbox: true`.
4. **Report** in one or two lines (in the user's language): where the file is (clickable link) and its size; mention
   `--full` if the user may want tool outputs too. Don't paste the exported content into the chat.
