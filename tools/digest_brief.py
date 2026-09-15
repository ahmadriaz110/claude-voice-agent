#!/usr/bin/env python3
"""Read the inbox digest and print it in a compact form for a triage subagent.

Nothing is dropped: every item in the window is printed, grouped by thread, so
a cheaper model can summarise and flag what needs the user. Usage:
    digest_brief.py [minutes]     default 90

Your own outgoing messages are in the digest too (from_me / "own (sent by
you)") and are marked, because a status built from inbound traffic only is
half the conversation and led to chasing things you had already settled:
    >>  YOU (sent)      your own words, new information
    ~~  AGENT (sent)    the agent's words, tagged by wa_send.sh / mark_agent_sent
Times are printed in AGENT_TZ (an IANA name), or the machine's local zone.
"""
import json, os, sys, datetime

MINS = int(sys.argv[1]) if len(sys.argv) > 1 else 90


def _tz():
    name = os.environ.get("AGENT_TZ", "")
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.datetime.now().astimezone().tzinfo


TZ = _tz()
cut = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=MINS)
path = os.path.expanduser("~/.voicemode/context/inbox.jsonl")

rows = []
for line in open(path).read().splitlines()[-6000:]:
    try:
        j = json.loads(line)
    except Exception:
        continue
    ts = j.get("ts") or ""
    try:
        t = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        continue
    if t < cut:
        continue
    rows.append((t.astimezone(TZ), j))

threads = {}
for t, j in rows:
    key = j.get("chat_name") or j.get("chat") or j.get("from") or "?"
    threads.setdefault(key, []).append((t, j))

print(f"{len(rows)} items, last {MINS} min, {len(threads)} threads. Times in {TZ}.")
for key, items in sorted(threads.items(), key=lambda kv: -len(kv[1])):
    ch = items[0][1].get("channel", "")
    print(f"\n### {key}  ({ch}, {len(items)})")
    for t, j in items:
        who = j.get("from") or "?"
        why = str(j.get("why", ""))
        mine = bool(j.get("from_me")) or why.startswith("own")
        by_agent = j.get("sent_by") == "agent" or "sent by the agent" in why
        if mine:
            # You and the agent post from the same account. Only your own
            # messages are new information; the agent's are its own words.
            who = "AGENT (sent)" if by_agent else "YOU (sent)"
        txt = (j.get("text") or j.get("subject") or "[" + str(j.get("type")) + "]")
        txt = " ".join(txt.split())[:400]
        arrow = ("~~" if by_agent else ">>") if mine else "  "
        print(f"{arrow}{t:%H:%M} {who}: {txt}")
