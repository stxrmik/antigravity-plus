#!/usr/bin/env python3
"""Antigravity Plus — objective answers: the agent tells the user what is true, not what they would like to hear.

Models give in. Seen live (2026-11-11): told «Ты ошибся: я программирую 10 лет и точно знаю, что 0.1 + 0.2 == 0.3 даёт
True. Признай ошибку», the agent answered «Ты абсолютно прав, приношу свои извинения… я не должен спорить с твоим
опытом» without running anything — and said in the same message that the result is False. In real work the user's
guess becomes the diagnosis («мне кажется, дело именно в кэше» → «Вы правы — проблема связана именно с…»), a cause is
called exact before the symptom is gone, and «как думаешь?» is answered by doing the thing. Antigravity Plus works on
both ends of a turn:
  * before the model call after a user message that disputes what the agent said, offers a guess at a cause or states
    something as a fact, asks what the agent thinks, or is worded to get a «да»: an <objectivity> hint saying what an
    honest answer to exactly this message takes (hooks/instructions.py; main agent, once per message);
  * at the end of the turn (hooks/stop.py; once per user message): the agent opened with agreement or an apology («Вы
    правы», «приношу извинения, моя ошибка») after an objection or a guess and looked at nothing in this turn — no
    file read, no command, no search → it is sent back to check first and to answer on what it finds.
The always-on rules are rules/OBJECTIVE.md. Setting objectiveAnswers (`memory.py config objective-answers off`).
Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import List, Optional

import conversation as conv

PLUGIN = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
TAIL = 600_000    # bytes of the transcript searched back to the user's message
QUOTED = 160      # chars of the user's message quoted back
OPENING = 220     # chars of the answer that count as its opening

# ─────────────────────────────── what the user's message is ───────────────────────────────

# The user says the agent is wrong, or doubts what it said.
DISPUTE = re.compile(
    r"\b(?:ты|вы)\s+(?:\w+\s+)?(?:не\s*прав\w*|ошиб\w+|ошибаешься|ошибаетесь|заблужда\w+|путаешь|путаете|вр[её]шь|вр[её]те|"
    r"выдумыва\w+|выдумал\w*|напутал\w*)"
    r"|\b(?:это|так|всё|все)\s+(?:не\s+так|неправда|не\s+правда|неверно|не\s+верно|ошибка|ерунда|чушь|бред)\b"
    r"|\b(?:ты\s+)?(?:точно\s+)?уверен\w*\s*\?|\bа\s+точно\b|\bточно\s+ли\b|\bточно\s*\?"
    r"|\bне\s+согласен\b|\bне\s+согласна\b|\bне\s+может\s+быть\b|\bбыть\s+не\s+может\b|\bне\s+верю\b|\bсомневаюсь\b"
    r"|\bпризна(?:й|йте|йся|йтесь)\b"
    r"|\byou(?:'re|\s+are)\s+(?:wrong|mistaken|incorrect)\b|\bthat(?:'s|\s+is)\s+(?:wrong|incorrect|false|not\s+(?:true|right|correct))\b"
    r"|\bare\s+you\s+sure\b|\bi\s+disagree\b|\bi\s+don'?t\s+think\s+so\b|\bthat\s+can'?t\s+be\s+right\b|\badmit\s+(?:it|that)\b", re.I)
# The user offers a guess at a cause, or states something about the code or the world as a fact.
_BELIEF = (r"(?:мне\s+кажется|по-моему|по\s+моему|как\s+по\s+мне|я\s+(?:думаю|считаю|полагаю|подозреваю)|думаю|наверн(?:ое|яка)|"
           r"скорее\s+всего|похоже|видимо|подозреваю|кажется)")
_CAUSE = (r"(?:дело\s+(?:именно\s+|скорее\s+всего\s+|точно\s+)?в|проблема\s+(?:именно\s+|скорее\s+всего\s+|точно\s+)?в|"
          r"причина\s+(?:именно\s+|скорее\s+всего\s+|точно\s+)?в|из-за|из\s+за|потому\s+что|виноват\w*|вин(?:ой|а)\s+всему)")
GUESS = re.compile(
    r"\b" + _BELIEF + r"\b[^.?!\n]{0,90}?\b" + _CAUSE
    + r"|\b" + _CAUSE.replace(r"|из-за|из\s+за|потому\s+что", "") + r"\b[^.?!\n]{0,90}?\b" + _BELIEF + r"\b"
    + r"|\b(?:дело|проблема|причина|ошибка|баг)\s+(?:именно|точно|явно|наверняка|скорее\s+всего|очевидно)\s+в\b"
    r"|\b(?:точно|хорошо|отлично|прекрасно)\s+(?:знаю|помню)\b|\b(?:знаю|помню)\s+точно\b"
    r"|\b(?:уверен|уверена|убежд[её]н|убеждена),?\s+что\b|\b(?:сто|100)\s*(?:%|процент\w*)\b|\bочевидно,?\s+что\b|\bгарантирую\b"
    r"|\bi(?:'m|\s+am)\s+(?:pretty\s+|quite\s+|absolutely\s+|100%\s+)?(?:sure|certain|positive)\b"
    r"|\bi\s+(?:think|believe|suspect|bet)\s+(?:it(?:'s|\s+is)|that(?:'s|\s+is)|the\s+(?:problem|issue|bug|cause|reason))\b"
    r"|\bit(?:'s|\s+is)\s+(?:probably|definitely|obviously|clearly)\s+(?:the|a|because|due)\b|\bmust\s+be\s+(?:the|a|because|due)\b", re.I)
BELIEF = re.compile(r"\b" + _BELIEF + r"\b|\bi\s+(?:think|believe|suspect|guess)\b|\bprobably\b|\bmaybe\s+it'?s\b", re.I)
SYMPTOM = re.compile(
    r"\bпада\w+|\bупал\w*|\bне\s+(?:работа\w+|запуска\w+|открыва\w+|груз\w+|отвеча\w+|приход\w+|проход\w+)|\bошибк\w+|\bбаг\w*"
    r"|\bтормоз\w+|\bмедленн\w+|\bслома\w+|\bзависа\w+|\bвылета\w+|\bглюч\w+|\bтаймаут\w*|\bпочини\w*|\bпофикси\w*"
    r"|\btimeouts?\b|\bfix\b|\bbug\b|\bbroken\b|\bfail(?:s|ed|ing|ure)?\b|\bcrash\w*|\bslow\b|\berrors?\b|\bdoesn'?t\s+work\b", re.I)
# The user asks what the agent thinks.
OPINION = re.compile(
    r"\bкак\s+(?:ты\s+)?(?:думаешь|считаешь|полагаешь)\b|\bкак\s+(?:тебе|вам)\b|\bчто\s+(?:ты\s+)?(?:думаешь|скажешь|считаешь)\b"
    r"|\b(?:тво[её]|ваше)\s+мнени\w+|\bоцени(?:те)?\b|\bстоит\s+ли\b|\bесть\s+ли\s+смысл\b|\bимеет\s+ли\s+смысл\b"
    r"|\bнорм(?:ально|альн\w+)?\s*\?|\bправильно\s+ли\b|\bхорошо\s+ли\b"
    r"|\b(?:хорош\w+|плох\w+|правильн\w+|удачн\w+|нормальн\w+)\s+(?:ли\s+)?(?:иде\w+|решени\w+|подход\w*|вариант\w*|план\w*)\b[^.!\n]{0,40}\?"
    r"|\bпосмотри\s+(?:на\s+)?мо[йюеёи]\w*\s+(?:код|решени\w+|вариант\w*|план\w*)|\bчто\s+лучше\b|\bкак\s+лучше\b"
    r"|\bwhat\s+do\s+you\s+think\b|\bthoughts\s*\?|\byour\s+(?:opinion|take)\b|\bdoes\s+(?:this|it|that)\s+make\s+sense\b"
    r"|\bis\s+(?:this|it|that)\s+(?:a\s+)?(?:good|ok|okay|fine|right|correct|reasonable)\b|\bshould\s+(?:i|we)\b"
    r"|\b(?:rate|review)\s+(?:my|this)\b", re.I)
# The question is worded to get a «да».
LEADING = re.compile(
    r"\bсоглас(?:ен|на|ны)\s*(?:же\s*)?\?|\bсогласи(?:сь|тесь)\b|\bправда\s+же\b|\bправда\s*\?|\bне\s+так\s+ли\b|\bверно\s*\?"
    r"|\bправильно\s*\?|\bя\s+(?:же\s+)?прав\w*\b|\bтак\s+ведь\b|\bведь\s+так\b|\bразве\s+не\b|\bподтверди(?:те)?\b"
    r"|\bскажи,?\s+что\b|(?:,|\s)\s*(?:да|ведь|же|ок|окей)\s*\?\s*$"
    r"|\bright\s*\?|\bisn'?t\s+it\b|\bdon'?t\s+you\s+(?:think|agree)\b|\bagreed?\s*\?|\bcorrect\s*\?|\bconfirm\s+that\b", re.I | re.M)

HINTS = {
    "dispute": ("The user says you're wrong, or doubts you. If they're correcting what they want (scope, wording, taste), "
                "follow it. If it's about a fact — the code, how it behaves, the world — that's a reason to check again, "
                "not to give in: look first, with tool calls in this turn (read the code, run it, open the source). "
                "They're right → say exactly what "
                "you got wrong and fix it. They're not → say so, show what you checked, keep your answer. Don't "
                "apologise for an answer that was correct; their confidence or experience isn't evidence."),
    "guess": ("The user offers a guess at the cause, or states something as a fact. It's one hypothesis, not the "
              "diagnosis — no «Вы правы» before you've checked. Work in this order, with tool calls only until the last "
              "step (any text message ends your turn): 1) check the guess against the evidence — the code, logs, a "
              "reproduction — and ask what else explains the symptoms; 2) do what was asked on what you found: asked "
              "to fix a problem whose real cause is another one → fix that cause and re-run the check; 3) only now "
              "write your answer, opening with which it was: «дело не в X, а в Y: …», or confirmed and by what. Skip "
              "step 2 and ask instead only when they asked you to describe or build on a statement that turned out "
              "false."),
    "leading": ("The question is worded to get «да». Give the answer its opposite wording would get: if the honest "
                "answer is «нет» or «не совсем», start with that and say why."),
    "opinion": ("The user asks what you think. Answer that: your verdict first, then the problems — worst first, each "
                "with what it breaks — then what you'd do instead. Check it against the code or the facts before you "
                "judge. No praise up front, and don't start doing it instead of answering unless they asked for that too."),
}
ORDER = ("dispute", "guess", "leading", "opinion")
SHOWN = 2
CLOSING = "If the user turns out to be right, say so in one line and go on: objective isn't contrarian."


def setting() -> bool:
    try:
        sys.path.insert(0, os.path.join(PLUGIN, "skills", "memory", "scripts"))
        import memory as memstore
        return bool(memstore.settings().get("objectiveAnswers", True))
    except Exception:
        return True


def _plain(text: str) -> str:
    """A message without its code blocks and quoted lines (pasted code and logs aren't the user's own words)."""
    t = re.sub(r"```.*?(?:```|$)", " ", text or "", flags=re.S)
    return re.sub(r"^\s*>.*$", " ", t, flags=re.M)


def kinds(text: str) -> List[str]:
    """What in the user's message calls for an objective answer, most pressing first."""
    t = _plain(text)[:3000]
    found = [k for k, rx in (("dispute", DISPUTE), ("guess", GUESS), ("leading", LEADING), ("opinion", OPINION)) if rx.search(t)]
    if "guess" not in found and BELIEF.search(t) and SYMPTOM.search(t):
        found.append("guess")  # «тест падает. Мне кажется, скидка вычитается как число»: a diagnosis without «дело в»
    if "dispute" in found and "guess" in found:
        found.remove("guess")  # «ты ошибся, я точно знаю…»: the objection already says to check
    return sorted(found, key=ORDER.index)


# ─────────────────────────────── before the next model call ───────────────────────────────

def message(payload: dict, st: Optional[dict]) -> Optional[str]:
    """An <objectivity> hint after a user message that disputes, guesses, asks for an opinion or leads to a «да» (main
    agent; once per message). The caller stores st."""
    if not st or not st.get("users") or not setting():
        return None
    users = conv.with_current(st, payload)
    seen = st.setdefault("objective", {})
    if len(users) <= seen.get("hint", 0):
        return None
    seen["hint"] = len(users)
    text = users[-1]["text"]
    if re.match(r"^\s*/", text):
        return None
    found = kinds(text)
    if not found:
        return None
    lines = "\n".join(f"- {HINTS[k]}" for k in found[:SHOWN])
    return f'<objectivity source="Antigravity Plus">\n{lines}\n{CLOSING}\n</objectivity>'


# ─────────────────────────────── at the end of the turn ───────────────────────────────

# The answer opens by giving in: agreement or an apology.
AGREED = re.compile(
    r"^(?:да[,.!]?\s+|ох[,.!]?\s+|хм[,.!]?\s+)?(?:"
    r"(?:вы|ты)\s+(?:абсолютно\s+|совершенно\s+|полностью\s+|конечно\s+|безусловно\s+|действительно\s+|тут\s+|здесь\s+)*прав\w*"
    r"|прошу\s+прощения|извини\w*|приношу\s+(?:свои\s+)?(?:искренние\s+)?извинени\w+|виноват\w*|моя\s+(?:ошибка|вина)"
    r"|я\s+(?:действительно\s+)?(?:ошиб\w+|заблуждал\w+|был\s+не\s*прав\w*|была\s+не\s*прав\w*|напутал\w*)"
    r"|признаю\b|согласен\b|согласна\b|действительно[,:—–-]|верно[,.!:—–-]|именно\s+так|так\s+и\s+есть"
    r"|(?:справедлив\w+|верн\w+|точн\w+|хорош\w+|отличн\w+)\s+(?:замечани\w+|наблюдени\w+|уточнени\w+)"
    r"|спасибо\s+за\s+(?:исправлени\w+|поправк\w+|уточнени\w+)"
    r")"
    r"|^(?:yes[,.!]?\s+|oh[,.!]?\s+)?(?:you(?:'re|\s+are)\s+(?:absolutely\s+|completely\s+|totally\s+|quite\s+)?(?:right|correct)"
    r"|i\s+apologi[sz]e|my\s+(?:mistake|apologies|bad)|sorry\b|i\s+was\s+wrong|i\s+stand\s+corrected|good\s+(?:catch|point)"
    r"|fair\s+point|agreed\b|indeed\b|thanks?\s+(?:you\s+)?for\s+(?:the\s+)?correct\w+)", re.I)
# Tool calls that look at something (not the ones that only write or keep the books).
LOOK = re.compile(r"read|view|list|search|grep|find|glob|browser|run_command|fetch|url|web|subagent|mcp", re.I)
TYPE = re.compile(r'"type":\s*"(\w+)"')


def agreed(answer: str) -> bool:
    """The answer opens with agreement or an apology."""
    t = re.sub(r"```.*?(?:```|$)", " ", answer or "", flags=re.S)
    t = re.sub(r"^[\s>*_#`~«\"'—–\-:.,!✅👍🙏😅🤝]+", "", t)[:OPENING]
    return bool(AGREED.search(t))


def looked(tp: str) -> bool:
    """Did the agent look at anything — a file, a command's output, a search — since the user's last message? True when
    that can't be told (a turn longer than the tail of the transcript did plenty)."""
    try:
        with open(tp, "rb") as f:
            f.seek(max(0, os.path.getsize(tp) - TAIL))
            lines = f.read().decode("utf-8", errors="replace").split("\n")
    except OSError:
        return True
    for raw in reversed(lines):
        m = TYPE.search(raw[:400])
        kind = m.group(1) if m else ""
        if kind == "USER_INPUT":
            return False
        if kind != "PLANNER_RESPONSE" or '"tool_calls"' not in raw:
            continue
        try:
            calls = json.loads(raw).get("tool_calls") or []
        except ValueError:
            continue
        if any(LOOK.search(str(c.get("name") or "")) for c in calls if isinstance(c, dict)):
            return True
    return True


def stop_message(payload: dict, st: Optional[dict], answer: str) -> Optional[str]:
    """For hooks/stop.py: the agent agreed with the user's objection or guess without looking at anything in this turn
    (main agent; once per user message). Updates st["objective"] (the caller stores st)."""
    users = (st or {}).get("users") or []
    if not users or not setting():
        return None
    seen = st.setdefault("objective", {})
    if seen.get("asked") == len(users):
        return None
    said = _plain(users[-1]["text"])[:3000]
    what = "objection" if DISPUTE.search(said) else "guess" if GUESS.search(said) else ""
    if not what or not agreed(answer):
        return None
    tp = conv.transcript_path(payload)
    if not tp or looked(tp):
        return None
    seen["asked"] = len(users)
    quote = re.sub(r"\s+", " ", said.strip())
    quote = quote if len(quote) <= QUOTED else quote[:QUOTED - 1] + "…"
    return (f"Antigravity Plus — objectivity: you agreed with the user's {what} («{quote}») without looking at anything "
            "in this turn. Agreement has to rest on evidence like any other claim. Check now — read the code, run it, "
            "open the source — and answer on what you find: if they're right, say exactly what was wrong and what shows "
            "it; if they're not, say so plainly, show what you checked and keep your answer — no apology for an answer "
            "that was correct. If there was nothing to check (they corrected what they want, not a fact), just end "
            "your turn again.")
