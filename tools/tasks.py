#!/usr/bin/env python3
"""Task ledger for tasks the user assigns by voice or chat.

  tasks.py add "text" [--note "..."]      -> new open task, prints id
  tasks.py start ID | done ID | block ID [--note "..."]
  tasks.py note ID "text"
  tasks.py list [open|done|all]           -> human list (default open)
  tasks.py prio ID high|medium|low        -> priority tier
Ledger: ~/.voicemode/context/tasks.json ; rendered: tasks.md next to it, and a
Pending.md for the user (open items grouped by priority, newest first) at
TASKS_PENDING_MD, default ~/.voicemode/context/Pending.md. Times in AGENT_TZ
(an IANA name) or the machine's zone.
"""
import json, os, sys, time
from datetime import datetime
from pathlib import Path
CTX = Path.home() / ".voicemode" / "context"
DB = CTX / "tasks.json"; MD = CTX / "tasks.md"
PENDING = Path(os.path.expanduser(os.environ.get("TASKS_PENDING_MD", "") or str(CTX / "Pending.md")))
TIERS = ("high", "medium", "low")


def local_now():
    name = os.environ.get("AGENT_TZ", "")
    if name:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(name))
        except Exception:
            pass
    return datetime.now()


def render_pending(d):
    """The user's live view: every open item, grouped by priority tier, newest first inside a tier."""
    rows = [t for t in d["tasks"] if t["status"] != "done"]
    out = [f"# Pending ({len(rows)} open), updated {local_now():%a %d %b %Y %H:%M}", "",
           "Kept by the agent from the task ledger. High = needs the user or due within days; "
           "Medium = waiting on others / this week; Low = backlog. Newest first inside a tier.", ""]
    labels = {"high": "High", "medium": "Medium", "low": "Low", None: "Unranked"}
    for tier in TIERS + (None,):
        sel = [t for t in rows if t.get("priority") == tier]
        if not sel: continue
        out.append(f"## {labels[tier]} ({len(sel)})"); out.append("")
        for t in sorted(sel, key=lambda t: -t["id"]):
            flag = "" if t["status"] == "open" else f" [{t['status']}]"
            out.append(f"- **#{t['id']}**{flag} {t['text']}")
            if t.get("notes"):
                out.append(f"  - latest: {t['notes'][-1]}")
        out.append("")
    try:
        PENDING.parent.mkdir(parents=True, exist_ok=True)
        PENDING.write_text("\n".join(out))
    except Exception:
        pass

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
    render_pending(d)

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
    if cmd == "prio":
        lvl = a[2].lower()
        if lvl not in TIERS: sys.exit("priority must be high|medium|low")
        t["priority"] = lvl
    if note: t.setdefault("notes", []).append(note)
    save(d); print(f"#{tid} {t['status']}")

if __name__ == "__main__":
    main(sys.argv[1:])
