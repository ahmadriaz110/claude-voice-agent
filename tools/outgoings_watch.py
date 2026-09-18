#!/usr/bin/env python3
"""Continuous outgoings watch: every INTERVAL seconds, while the user is AT DESK
(presence.json written by the menu bar toggle), run outgoings.py over a short
window and print only the sends not printed before, one line each. Away: sleep
(the second phone's sends arrive through the inbox relay instead). Meant to be
run as a long-lived monitor from the agent session: every printed line becomes
an event the agent sees, so it never answers on top of something the user just
sent himself.

  usage: outgoings_watch.py [interval_seconds]   (default 60)
"""
import subprocess, time, json, hashlib, os, sys, datetime
HERE = os.path.dirname(os.path.abspath(__file__))
PRES = os.path.expanduser(os.environ.get("PRESENCE_FILE", "~/.voicemode/presence.json"))
STATE = os.path.expanduser("~/.voicemode/context/outgoings_seen.json")
INTERVAL = int(sys.argv[1]) if len(sys.argv) > 1 else 60
WINDOW_MIN = int(os.environ.get("OUTGOINGS_WATCH_WINDOW_MIN", "4"))


def at_desk():
    try:
        return bool(json.load(open(PRES)).get("at_desk"))
    except Exception:
        return True   # no presence file: always watch


def load_seen():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def save_seen(s):
    cutoff = time.time() - 6 * 3600
    s = {k: v for k, v in s.items() if v > cutoff}
    json.dump(s, open(STATE, "w"))
    return s


seen = load_seen()
print(f"outgoings watch started {datetime.datetime.now().strftime('%H:%M:%S')} interval {INTERVAL}s", flush=True)
while True:
    if at_desk():
        try:
            out = subprocess.run([sys.executable, os.path.join(HERE, "outgoings.py"), "--min", str(WINDOW_MIN)],
                                 capture_output=True, text=True, timeout=110).stdout
        except Exception as e:
            out = f"  watch error: {e}"
        for line in out.splitlines():
            if not line.startswith("  ") or "No sends" in line:
                continue
            key = hashlib.sha1(line.strip().encode()).hexdigest()
            if key in seen:
                continue
            seen[key] = time.time(); seen = save_seen(seen)
            print(line.strip(), flush=True)
    time.sleep(INTERVAL)
