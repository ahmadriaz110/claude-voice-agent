#!/usr/bin/env python3
"""Pull WhatsApp history held by the local Baileys daemon (full-history sync is
on) into ~/.voicemode/context/history/whatsapp.jsonl. Usage: [days] [per-chat cap]"""
import json, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
DAEMON = "http://127.0.0.1:47823"
H = Path.home() / ".voicemode" / "context" / "history"; H.mkdir(parents=True, exist_ok=True)
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 60
CAP = int(sys.argv[2]) if len(sys.argv) > 2 else 500
since = datetime.now(timezone.utc) - timedelta(days=DAYS)
def get(path):
    with urllib.request.urlopen(DAEMON + path, timeout=60) as r:
        return json.loads(r.read())
chats = get("/chats?limit=1000")
rows, skipped = [], 0
for c in chats:
    jid = c.get("id") or ""
    if jid == "status@broadcast" or not jid:
        skipped += 1; continue
    try:
        msgs = get(f"/messages?phone={urllib.parse.quote(jid)}&limit={CAP}")
    except Exception as e:
        print(f"{c.get('name')}: error {str(e)[:80]}", flush=True); continue
    n = 0
    for m in msgs:
        ts = m.get("timestamp") or ""
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if t < since: continue
        except Exception:
            pass
        rows.append({"channel": "whatsapp", "chat": c.get("name") or jid.split("@")[0], "chat_jid": jid,
                     "group": jid.endswith("@g.us"), "ts": ts, "from": m.get("from", ""),
                     "text": (m.get("text") or "")[:500], "backfill": True})
        n += 1
    time.sleep(0.05)
rows.sort(key=lambda r: r.get("ts") or "")
with open(H / "whatsapp.jsonl", "w") as f:
    for r in rows: f.write(json.dumps(r, ensure_ascii=False) + "\n")
print(f"whatsapp: {len(rows)} records from {len(chats) - skipped} chats (last {DAYS} days)", flush=True)
