---
name: usage
description: >-
  Token usage and API-equivalent cost of this conversation or all of them: tokens, cache, context fill, per-turn
  breakdown, subagents, plan limits, plus a live side panel. Use when the user asks about tokens, context, cost or
  limits («сколько потратили», «сколько осталось лимита», /usage, /cost), and on your own when a long conversation
  may be near its context limit, to decide whether to suggest /handoff.
---

# /usage — token & cost report

> **On your own:** in a long conversation, check the context fill (`usage.py --conversation <id>`, read only the summary). Say nothing unless it's above ~70%; then tell the user in one line and suggest /handoff.

The script [usage.py](scripts/usage.py) reads Antigravity transcripts (each model call records
`input_tokens`, `cache_read_tokens`, `output_tokens`) and prices them with [pricing.json](pricing.json).

## Steps

1. **Pick the mode from the user's request:**
   - default → the current session. Your conversation id is in your system context (also the
     `<id>` segment of your artifact directory path).
   - "all", "total", "за неделю", "по дням" → `--all` (add `--days N` if a period is mentioned).
   - If the user names a model to price against ("посчитай как Sonnet"), add `--model <id>` using an `id` from pricing.json.
   - If the user names a Gemini API tier ("по Batch", "Flex", "Priority", "Contributor"), add `--tier <name>` (lower-case); models without that tier in pricing.json stay on Standard.

2. **Run it** (sandboxed first):
   ```bash
   python3 <skill_dir>/scripts/usage.py --conversation <conversation-id> --html <artifact_dir>/usage.html
   ```
   For all sessions:
   ```bash
   python3 <skill_dir>/scripts/usage.py --all --days 7
   ```
   - `--all` and subagent transcripts need read access to the whole `~/.gemini/antigravity/brain/`
     directory. If the output says access is denied (песочница), re-run the **same** command with
     `BypassSandbox: true` — it's read-only.

3. **Present the result:**
   - Paste the script's markdown output **verbatim** (do not recompute or re-round numbers; drop only the trailing `<!-- html: … -->` line).
   - In single-session mode, also embed the dashboard on its own line right after the markdown:
     `<agent-embed src="file://<artifact_dir>/usage.html"></agent-embed>`
   - Then add at most 2–3 short, concrete observations in the user's language, only if useful — e.g. context is above 70% → suggest finishing the task and starting a new conversation; output dominates cost → long generations; low cache hit rate; one subagent dominates cost.
   - End with the link to the live side panel on its own line (it updates every few seconds while the agent works):
     `[Расход токенов](sidecar://antigravity-plus/usage/)`

## Notes
- Costs are an **API-equivalent estimate**, not the user's actual subscription bill.
- The report includes the call that is running `/usage` itself only partially — that's expected.
- If a model shows "Нет цены", add an entry to `pricing.json` (USD per 1M tokens, `match` is a regex on the lower-cased model name). Dated price changes go in `price_changes`, other Gemini API tiers in `service_tiers` — see the Gemini 4 Argon entry.
