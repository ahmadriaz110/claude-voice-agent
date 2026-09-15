#!/bin/bash
# Read what the receiver logged while injection was quiet (quiet.json) or
# while you were away: one line per digest item in the last N minutes.
# usage: digest.sh [minutes]   default 60
# Times in AGENT_TZ (an IANA name) or the machine's local zone.
m=${1:-60}
python3 - "$m" <<'PY'
import json, os, sys, datetime
mins = int(sys.argv[1])
name = os.environ.get("AGENT_TZ", "")
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(name) if name else datetime.datetime.now().astimezone().tzinfo
except Exception:
    TZ = datetime.datetime.now().astimezone().tzinfo
cut = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=mins)
p = os.path.expanduser("~/.voicemode/context/inbox.jsonl")
rows = []
for l in open(p).read().splitlines()[-4000:]:
    try: j = json.loads(l)
    except Exception: continue
    ts = j.get("ts", "")
    if not ts: continue
    try: t = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception: continue
    if t >= cut: rows.append((t.astimezone(TZ), j))
print(f"{len(rows)} items in the last {mins} min")
for t, j in rows:
    who = j.get("from") or j.get("chat_name") or j.get("chat") or "?"
    txt = (j.get("text") or j.get("subject") or "[" + str(j.get("type")) + "]").replace("\n", " ")[:150]
    print(f"  {t:%H:%M} {j.get('channel','')[:2]} {who[:22]:22} {txt}")
PY
