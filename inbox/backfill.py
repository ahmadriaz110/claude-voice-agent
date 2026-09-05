#!/usr/bin/env python3
"""Backfill the last N days of Outlook mail (Inbox + HCL) and Teams chat
messages (the subscribed chats) into ~/.voicemode/context/history/*.jsonl,
one record per message in the inbox.jsonl shape, flagged backfill=true.
Never injects anything into the session. Usage: backfill.py [days]"""
import json, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path.home() / ".voicemode" / "indicator"))
import inbox_hooks as ih

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 60
since = (datetime.now(timezone.utc) - timedelta(days=DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
H = ih.CTX / "history"; H.mkdir(exist_ok=True)

def say(msg): print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)

def dump(name, rows):
    rows.sort(key=lambda r: r.get("ts") or "")
    with open(H / f"{name}.jsonl", "w") as f:
        for r in rows: f.write(json.dumps(r, ensure_ascii=False) + "\n")
    say(f"{name}: {len(rows)} records -> {H / (name + '.jsonl')}")

def paged(url):
    while url:
        for attempt in range(4):
            try:
                res = ih.graph("GET", url); break
            except Exception as e:
                if "429" in str(e) and attempt < 3: time.sleep(5 * (attempt + 1)); continue
                raise
        for v in res.get("value", []): yield v
        url = res.get("@odata.nextLink")

# ---- email ------------------------------------------------------------------
rows = []
for name, res in ih.mail_resources():
    folder = res.split("/messages")[0]
    url = (f"{folder}/messages?$filter=receivedDateTime ge {since}&$orderby=receivedDateTime desc"
           "&$top=100&$select=id,subject,from,toRecipients,ccRecipients,receivedDateTime,bodyPreview,importance,conversationId,webLink").replace(" ", "%20")
    n = 0
    for m in paged(url):
        frm = (m.get("from") or {}).get("emailAddress") or {}
        rows.append({"channel": "email", "folder": name.split(":", 1)[1], "id": m["id"], "ts": m.get("receivedDateTime"),
                     "from": frm.get("address", ""), "from_name": frm.get("name", ""),
                     "to": [r["emailAddress"]["address"] for r in m.get("toRecipients", [])],
                     "cc": [r["emailAddress"]["address"] for r in m.get("ccRecipients", [])],
                     "subject": m.get("subject", ""), "text": (m.get("bodyPreview") or "")[:500],
                     "importance": m.get("importance"), "thread": m.get("conversationId"),
                     "link": m.get("webLink"), "backfill": True})
        n += 1
    say(f"email {name}: {n}")
dump("email", rows)

# ---- teams ------------------------------------------------------------------
rows = []
for chat_id, topic in ih.chat_targets():
    n = 0
    try:
        for m in paged(f"chats/{chat_id}/messages?$top=50"):
            if (m.get("createdDateTime") or "") < since: break
            if m.get("messageType") != "message": continue
            frm = ((m.get("from") or {}).get("user") or {})
            rows.append({"channel": "teams", "chat": topic, "chat_id": chat_id, "id": m["id"],
                         "ts": m["createdDateTime"], "from": frm.get("displayName", ""),
                         "text": ih._strip_html((m.get("body") or {}).get("content", ""))[:500],
                         "backfill": True})
            n += 1
    except Exception as e:
        say(f"teams {topic!r}: error {str(e)[:100]}")
    say(f"teams {topic!r}: {n}")
    time.sleep(0.3)
dump("teams", rows)
say("done")
