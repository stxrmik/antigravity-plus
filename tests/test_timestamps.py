"""The fractional-seconds normalisation shared by usage, insights, handoff and ci (Python 3.9's fromisoformat takes
only 3 or 6 digits): it rewrites the fraction after the seconds — «.5Z», «.123456789Z», «.12345+03:00» — and nothing
else: no fraction, a «+03:00» offset and dots elsewhere in the text stay as they are."""
import datetime as dt, importlib.util, os, sys

PLUGIN, SP = sys.argv[1], sys.argv[2]
ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + ("" if cond else f" → {detail}"))


def load(name, *parts):
    path = os.path.join(PLUGIN, "skills", *parts)
    sys.path.insert(0, os.path.dirname(path))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, os.path.join(PLUGIN, "hooks"))
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


usage = load("usage", "usage", "scripts", "usage.py")
insights = load("insights", "insights", "scripts", "insights.py")
handoff = load("handoff", "handoff", "scripts", "handoff.py")
ci = load("ci", "ci", "scripts", "ci.py")

UTC = dt.timezone.utc
base = dt.datetime(2026, 11, 25, 1, 2, 3, tzinfo=UTC)
cases = [  # (text, the instant it means)
    ("2026-11-25T01:02:03Z", base),
    ("2026-11-25T01:02:03+00:00", base),
    ("2026-11-25T01:02:00Z", base - dt.timedelta(seconds=3)),
    ("2026-11-25T01:02:03.5Z", base.replace(microsecond=500000)),
    ("2026-11-25T01:02:03.123Z", base.replace(microsecond=123000)),
    ("2026-11-25T01:02:03.12345Z", base.replace(microsecond=123450)),
    ("2026-11-25T01:02:03.123456Z", base.replace(microsecond=123456)),
    ("2026-11-25T01:02:03.123456789Z", base.replace(microsecond=123456)),
    ("2026-11-25T04:02:03+03:00", base),
    ("2026-11-25T04:02:03.5+03:00", base.replace(microsecond=500000)),
    ("2026-11-25T04:02:03.123456789+03:00", base.replace(microsecond=123456)),
    ("2026-11-24T20:02:03.000000001-05:00", base),
]
for text, want in cases:
    got_u = usage.parse_ts(text)
    got_i = insights.parse_ts(text)
    got_h = handoff.parse_time(text)
    check(f"usage.parse_ts({text})", got_u is not None and got_u.astimezone(UTC) == want, got_u)
    check(f"insights.parse_ts({text})", got_i is not None and got_i.astimezone(UTC) == want, got_i)
    check(f"handoff.parse_time({text})", got_h is not None and got_h.astimezone(UTC) == want, got_h)
# a «+03:00» offset survives (it is not a fraction), and a naive time stays naive in usage
check("the +03:00 offset is kept", usage.parse_ts("2026-11-25T04:02:03.5+03:00").utcoffset() == dt.timedelta(hours=3),
      usage.parse_ts("2026-11-25T04:02:03.5+03:00"))
# dots that are not a fraction of the seconds are never touched (so the text stays invalid, not "repaired")
for junk in ("2026-11-25.5T01:02:03Z", "2026.11.25T01:02:03Z", "1.5", "2026-11-25T01.5:02:03Z", "not a time.5"):
    check(f"usage.parse_ts({junk}) → None", usage.parse_ts(junk) is None, usage.parse_ts(junk))
    check(f"insights.parse_ts({junk}) → None", insights.parse_ts(junk) is None, insights.parse_ts(junk))
    check(f"handoff.parse_time({junk}) → None", handoff.parse_time(junk) is None, handoff.parse_time(junk))
check("empty / None → None everywhere", all(f(v) is None for f in (usage.parse_ts, insights.parse_ts, handoff.parse_time) for v in ("", None)))

# ci._elapsed uses the same normalisation for the timestamps `gh` / `glab` print
check("ci._elapsed handles a short fraction, nanoseconds and an offset",
      ci._elapsed("2026-11-25T01:00:00.5Z", "2026-11-25T01:00:05.123456789Z") == "4s"
      and ci._elapsed("2026-11-25T04:00:00+03:00", "2026-11-25T01:01:05.9Z") == "1m05s"
      and ci._elapsed("2026-11-25T01:00:00Z", "2026-11-25T01:00:07.25+00:00") == "7s",
      (ci._elapsed("2026-11-25T01:00:00.5Z", "2026-11-25T01:00:05.123456789Z"),
       ci._elapsed("2026-11-25T04:00:00+03:00", "2026-11-25T01:01:05.9Z"),
       ci._elapsed("2026-11-25T01:00:00Z", "2026-11-25T01:00:07.25+00:00")))

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
