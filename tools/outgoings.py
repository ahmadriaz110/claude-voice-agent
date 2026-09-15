#!/usr/bin/env python3
"""Your own sends in the last N minutes, all channels, one line each.

Usage: python3 outgoings.py [--min 15]

Prints only what YOU sent: WhatsApp from-me rows that are not in the agent's
send log (agent_sent_ids.jsonl, written by wa_send.sh and inbox_hooks), Teams
messages from you without the agent tag, and Sent Items. The agent reads this
BEFORE answering anyone on a thread, and a cron/launchd job runs it every 12
minutes and hands the output to the session, so the agent never re-raises
something you already handled yourself ("that is a major issue, you don't
check outgoings").

Needs the same agent.env as inbox_hooks.py (INBOX_ME_EMAIL, INBOX_ME_NAMES)
and its cached Graph sign-in. Times are printed in AGENT_TZ (an IANA name),
or the machine's local zone when unset.
"""
import sys, os, json, re, html, sqlite3, shutil, glob, datetime, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dns_fallback  # noqa: F401

ap = argparse.ArgumentParser(); ap.add_argument("--min", type=int, default=15); a = ap.parse_args()
since_s = a.min * 60
now_utc = datetime.datetime.now(datetime.timezone.utc)
since_iso = (now_utc - datetime.timedelta(seconds=since_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
out = []

# Messages the agent itself posts are tagged so people know; those prefixes
# are skipped here because they are not your words.
AGENT_PREFIXES = tuple(p for p in os.environ.get("OUTGOINGS_AGENT_PREFIXES", "[AI],Claude:").split(",") if p)
ME_NAMES = tuple(n.strip().lower() for n in os.environ.get("INBOX_ME_NAMES", "").split(",") if n.strip())


def _is_me(name):
    v = (name or "").lower()
    return bool(ME_NAMES) and all(n in v for n in ME_NAMES)


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


def local(dt):
    return dt.astimezone(TZ).strftime("%H:%M")


# WhatsApp: the desktop app's own database (read from a copy, WAL included).
try:
    cands = glob.glob(os.path.expanduser("~/Library/Group Containers/*/ChatStorage.sqlite")) + \
        glob.glob(os.path.expanduser("~/Library/Containers/*/Data/Library/*/ChatStorage.sqlite"))
    src = next(p for p in cands if os.path.exists(p))
    d = "/tmp/outgoings_wa"; os.makedirs(d, exist_ok=True)
    for ext in ("", "-wal", "-shm"):
        if os.path.exists(src + ext): shutil.copy(src + ext, d + "/ChatStorage.sqlite" + ext)
    agent = set()
    try:
        for line in open(os.path.expanduser("~/.voicemode/agent_sent_ids.jsonl")):
            try: agent.add(json.loads(line).get("text", "")[:80])
            except Exception: pass
    except FileNotFoundError:
        pass
    c = sqlite3.connect(d + "/ChatStorage.sqlite")
    # ZMESSAGEDATE is seconds since 2001-01-01 (Core Data), hence the 978307200 offset.
    q = """SELECT m.ZMESSAGEDATE, s.ZPARTNERNAME, s.ZCONTACTJID, m.ZMESSAGETYPE, m.ZTEXT FROM ZWAMESSAGE m
           JOIN ZWACHATSESSION s ON s.Z_PK=m.ZCHATSESSION WHERE m.ZISFROMME=1 AND m.ZMESSAGEDATE > strftime('%s','now')-978307200-? ORDER BY m.ZMESSAGEDATE"""
    for ts, who, jid, typ, txt in c.execute(q, (since_s,)):
        txt = txt or ""
        if txt[:80] in agent or txt.startswith(AGENT_PREFIXES):
            continue
        t = datetime.datetime.fromtimestamp(ts + 978307200, datetime.timezone.utc)
        kind = {3: "voice note", 1: "image", 59: "call", 14: "deleted", 15: "reaction"}.get(typ, "")
        # never echo credentials: a short message with a password-looking token
        if re.search(r"(?=\S*[A-Za-z])(?=\S*\d)(?=\S*[^A-Za-z0-9\s])\S{8,}", txt) and len(txt.split()) <= 6:
            txt = "[credentials, not shown]"
        out.append(f"{local(t)} | WhatsApp | YOU -> {who or jid} | {kind + ' ' if kind and not txt else ''}{txt[:220]}")
except StopIteration:
    out.append("WhatsApp check skipped: no ChatStorage.sqlite (WhatsApp desktop not installed?)")
except Exception as e:
    out.append(f"WhatsApp check failed: {e}")


def _iso(v):
    # Graph timestamps carry 0-7 fractional digits; fromisoformat (3.9) wants 3 or 6
    v = v.replace("Z", "+00:00")
    v = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], v)
    return datetime.datetime.fromisoformat(v)


# Graph: sent mail + Teams
try:
    import inbox_hooks, requests
    tok = inbox_hooks.token(); H = {"Authorization": "Bearer " + tok}
    r = requests.get("https://graph.microsoft.com/v1.0/me/mailFolders/sentitems/messages?$top=15&$orderby=sentDateTime desc&$select=subject,toRecipients,sentDateTime,bodyPreview", headers=H, timeout=40).json()
    for m in r.get("value", []):
        if m["sentDateTime"] >= since_iso:
            t = _iso(m["sentDateTime"])
            out.append(f"{local(t)} | Email | YOU -> {', '.join(x['emailAddress']['address'] for x in m['toRecipients'])[:80]} | {m['subject'][:60]} | {m['bodyPreview'][:140]}")
    r = requests.get("https://graph.microsoft.com/v1.0/me/chats?$top=40&$orderby=lastMessagePreview/createdDateTime desc&$expand=lastMessagePreview", headers=H, timeout=40).json()
    for ch in r.get("value", []):
        lp = ch.get("lastMessagePreview") or {}
        if (lp.get("createdDateTime") or "") < since_iso:
            continue
        msgs = requests.get(f"https://graph.microsoft.com/v1.0/me/chats/{ch['id']}/messages?$top=15", headers=H, timeout=40).json().get("value", [])
        label = ch.get("topic") or ch.get("chatType")
        if not ch.get("topic") and ch.get("chatType") == "oneOnOne":
            try:
                mem = requests.get(f"https://graph.microsoft.com/v1.0/me/chats/{ch['id']}/members", headers=H, timeout=40).json().get("value", [])
                other = [x.get("displayName") for x in mem if not _is_me(x.get("displayName") or "")]
                if other: label = "1:1 " + other[0]
            except Exception:
                pass
        for m in msgs:
            if m.get("createdDateTime", "") < since_iso: continue
            frm = ((m.get("from") or {}).get("user") or {}).get("displayName") or ""
            if not _is_me(frm): continue
            body = html.unescape(re.sub(r"<[^>]+>", " ", (m.get("body") or {}).get("content", ""))); body = re.sub(r"\s+", " ", body).strip()
            if body.startswith(AGENT_PREFIXES): continue
            t = _iso(m["createdDateTime"])
            out.append(f"{local(t)} | Teams | YOU in {label} | {body[:200]}")
except Exception as e:
    out.append(f"Graph check failed: {str(e)[:120]}")

out.sort()
print(f"Your sends, last {a.min} min ({TZ}):" if out else f"No sends by you in the last {a.min} min.")
for l in out: print(" ", l)
