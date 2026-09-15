#!/usr/bin/env python3
"""Emit new messages from one WhatsApp group as they arrive, one line each,
by polling the desktop app's ChatStorage.sqlite read-only.

Usage: watch_group.py <group_jid> [poll_seconds] [label]

  group_jid     the group's jid (<digits>@g.us), from the bridge's /chats
  poll_seconds  default 30
  label         a short tag for the lines, default "group"

Meant to be run under a monitor that feeds its stdout to the agent, so a
live group (a site job, an incident channel) is followed without subscribing
the whole inbox to it. Output lines look like:

  [label HH:MM] Name: text

Members show as their WhatsApp id (<digits>@lid) unless mapped in NAMES; fill
that map from the ids you see, or leave it empty. Your own messages show as
YOU. Times are printed in AGENT_TZ (an IANA name), or the machine's local
zone when unset.
"""
import sqlite3, glob, os, sys, time, datetime

jid = sys.argv[1]
poll = float(sys.argv[2]) if len(sys.argv) > 2 else 30
label = sys.argv[3] if len(sys.argv) > 3 else "group"
paths = glob.glob(os.path.expanduser('~/Library/Group Containers/*/ChatStorage.sqlite')) + \
    glob.glob(os.path.expanduser('~/Library/Containers/*/Data/Library/*/ChatStorage.sqlite'))
db = next(p for p in paths if os.path.exists(p))

# member jid -> display name, e.g. {"12345678901234@lid": "Jane (site lead)"}
NAMES = {}


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
EPOCH = datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc)   # Core Data dates


def q(sql, args=()):
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


last = q("select coalesce(max(m.Z_PK),0) from ZWAMESSAGE m join ZWACHATSESSION s on m.ZCHATSESSION=s.Z_PK where s.ZCONTACTJID=?", (jid,))[0][0]
while True:
    time.sleep(poll)
    try:
        rows = q("select m.Z_PK, m.ZISFROMME, m.ZMESSAGEDATE, gm.ZMEMBERJID, m.ZTEXT, m.ZMESSAGETYPE from ZWAMESSAGE m "
                 "join ZWACHATSESSION s on m.ZCHATSESSION=s.Z_PK left join ZWAGROUPMEMBER gm on m.ZGROUPMEMBER=gm.Z_PK "
                 "where s.ZCONTACTJID=? and m.Z_PK>? order by m.Z_PK", (jid, last))
    except Exception:
        continue
    for pk, me, ts, who, txt, typ in rows:
        last = pk
        t = (EPOCH + datetime.timedelta(seconds=ts)).astimezone(TZ).strftime('%H:%M')
        w = 'YOU' if me else NAMES.get(who, who or '?')
        body = (txt or {1: '[image]', 3: '[voice note]', 59: '[call]', 15: '[reaction]'}.get(typ, f'[type {typ}]')).replace('\n', ' / ')
        print(f"[{label} {t}] {w}: {body[:300]}", flush=True)
