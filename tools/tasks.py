#!/usr/bin/env python3
"""Task ledger for tasks Ahmad assigns by voice or chat.

  tasks.py add "text" [--note "..."]      -> new open task, prints id
  tasks.py start ID | done ID | block ID [--note "..."]
  tasks.py note ID "text"
  tasks.py list [open|done|all]           -> human list (default open)
Ledger: ~/.voicemode/context/tasks.json ; rendered: tasks.md
"""
import json, sys, time
from datetime import datetime
from pathlib import Path
CTX = Path.home() / ".voicemode" / "context"
DB = CTX / "tasks.json"; MD = CTX / "tasks.md"

def load():
    try: return json.loads(DB.read_text())
    except Exception: return {"next": 1, "tasks": []}

def save(d):
    DB.write_text(json.dumps(d, indent=2, ensure_ascii=False))
    lines = [f"# Task ledger (updated {datetime.now():%Y-%m-%d %H:%M})", ""]
    for status in ("in-progress", "open", "blocked", "done"):
        rows = [t for t in d["tasks"] if t["status"] == status]
        if not rows: continue
        lines.append(f"## {status} ({len(rows)})")
        for t in sorted(rows, key=lambda t: t["id"]):
            when = t.get("done_at") or t.get("created")
            note = f", {t['notes'][-1]}" if t.get("notes") else ""
            lines.append(f"- #{t['id']} {t['text']} ({when[:16]}){note}")
        lines.append("")
    MD.write_text("\n".join(lines))

def now(): return datetime.now().isoformat(timespec="minutes")

def main(a):
    d = load()
    if not a or a[0] == "list":
        which = a[1] if len(a) > 1 else "open"
        for t in d["tasks"]:
            if which == "all" or (which == "open" and t["status"] != "done") or t["status"] == which:
                print(f"#{t['id']:<3} {t['status']:<12} {t['text']}" + (f"  [{t['notes'][-1]}]" if t.get("notes") else ""))
        return
    cmd = a[0]
    note = None
    if "--note" in a:
        i = a.index("--note"); note = a[i+1]; a = a[:i] + a[i+2:]
    if cmd == "add":
        t = {"id": d["next"], "text": a[1], "status": "open", "created": now(), "notes": [note] if note else []}
        d["tasks"].append(t); d["next"] += 1; save(d); print(f"added #{t['id']}"); return
    tid = int(a[1]); t = next(x for x in d["tasks"] if x["id"] == tid)
    if cmd in ("start", "done", "block"):
        t["status"] = {"start": "in-progress", "done": "done", "block": "blocked"}[cmd]
        if cmd == "done": t["done_at"] = now()
    if cmd == "note": note = a[2]
    if note: t.setdefault("notes", []).append(note)
    save(d); print(f"#{tid} {t['status']}")

if __name__ == "__main__":
    main(sys.argv[1:])
