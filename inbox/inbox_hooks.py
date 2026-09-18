#!/usr/bin/env python3
"""
Inbox hooks: push-based awareness of Teams, Outlook mail and WhatsApp.

Receives Microsoft Graph change notifications (mail + Teams chat messages)
and Baileys WhatsApp webhook events on one local HTTP port, exposed to the
internet through the existing Cloudflare tunnel (public hostname -> this
Mac). Every event is appended to ~/.voicemode/context/inbox.jsonl and a
rolling ~/.voicemode/context/today.md is regenerated. Anything that passes
the importance rules is pushed into the live Claude session through the
barge-in daemon's inject.txt hook, so it is spoken immediately.

Why a separate daemon: the barge-in daemon owns the microphone and must
stay small and audio-only. This one owns the network. Both run under launchd
with KeepAlive, so they survive reboots and keep receiving with Claude closed.

Graph subscriptions expire (chat: 60 min, mail: ~3 days); a renewal thread
keeps them alive and recreates them if they vanish. Auth is a delegated
device-code sign-in with MSAL, cached to disk with refresh, so the sign-in
happens once.

Usage:
  inbox_hooks.py            run the service (launchd)
  inbox_hooks.py --login    interactive device-code sign-in, then exit
  inbox_hooks.py --status   print health as JSON
"""
import dns_fallback  # noqa: F401  DNS-over-HTTPS fallback when the system resolver dies
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
CTX = HOME / ".voicemode" / "context"
CTX.mkdir(parents=True, exist_ok=True)
INBOX = CTX / "inbox.jsonl"
TODAY = CTX / "today.md"
LOG = CTX / "inbox_hooks.log"
SECRET_FILE = CTX / "secret"
PUBLIC_URL_FILE = CTX / "public_url.txt"
TOKEN_CACHE = CTX / "msal_cache.json"
SUBS_FILE = CTX / "subscriptions.json"
SIGNIN_FILE = CTX / "signin.txt"
PENDING = CTX / "pending_important.jsonl"
DELIVERED = CTX / "delivered"          # touched by the daemon on a confirmed paste
DELIVERY_WATCH_S = float(os.environ.get("INBOX_DELIVERY_WATCH_S", 90))
INJECT_FILE = HOME / ".voicemode" / "indicator" / "inject.txt"
# Escalation: if an important item is pushed into the session and the user has
# not responded within ESCALATE_AFTER_S, the same line is sent to their other
# WhatsApp number through the local Baileys daemon (POST /send). "Responded"
# = ACK_FILE touched after the push: the barge-in daemon touches it whenever
# it verifies the user's voice, and any reply from that number touches it too.
ACK_FILE = CTX / "ack"
ESCALATE_TO = os.environ.get("INBOX_ESCALATE_TO", "")            # digits only, e.g. 4915551234567
# The user asked not to be notified for every message, only for urgent things
# and things that need them to get completed. Chat items are handled by the
# agent; only mail alerts, the sweep and Claude-not-running escalate on their
# own, and only after a real wait.
ESCALATE_AFTER_S = float(os.environ.get("INBOX_ESCALATE_AFTER", 300))
ESCALATE_MIN_GAP_S = 120
WA_DAEMON = os.environ.get("WA_DAEMON_URL", "http://127.0.0.1:47823")
MEDIA_DIR = CTX / "media"
WHISPER_URL = os.environ.get("INBOX_WHISPER_URL", "http://127.0.0.1:2022/v1/audio/transcriptions")
MEDIA_TYPES = {"imageMessage": "image", "audioMessage": "voice note", "videoMessage": "video",
               "documentMessage": "document", "stickerMessage": "sticker"}
_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "audio/ogg": "ogg",
        "audio/mpeg": "mp3", "audio/mp4": "m4a", "video/mp4": "mp4", "application/pdf": "pdf"}
_last_escalation = [0.0]
# Unanswered client requests. New quote/clarification requests from any
# client must not sit in the Inbox: if no reply from your own domain within
# QUOTE_SLA_H hours, flag it as important (so it is spoken and escalated).
QUOTE_SLA_H = float(os.environ.get("INBOX_QUOTE_SLA_H", 2))
QUOTE_WORDS = ("quote", "quotation", "rfq", "request", "pricing", "price", "availability",
               "engineer", "support", "site visit", "schedule", "confirm", "urgent", "ticket",
               "resource", "onsite", "on-site", "estimate", "proposal")
# Folders where every external mail counts as a client request (mail rules
# move a client's traffic there), and domains that are clients, never vendors.
CLIENT_FOLDERS = {f.strip() for f in os.environ.get("INBOX_CLIENT_FOLDERS", "").split(",") if f.strip()}
CLIENT_DOMAINS = {d.strip().lower() for d in os.environ.get("INBOX_CLIENT_DOMAINS", "").split(",") if d.strip()}
# Courtesy closings are not requests. "Thank you!", "Noted, will update the
# customer", "Feedback is well received", "Thanks for the update" were all
# flagged: a client folder counts every external mail, and elsewhere
# QUOTE_WORDS matched the quoted thread under the one-liner. A mail whose own
# words (see _own_text) are short and nothing but these phrases plus
# connectives is a closing, not a request.
COURTESY_WORDS = ("thank you", "thanks", "thx", "noted", "well received", "will update", "will revert",
                  "will check", "will get back", "will do", "ok", "okay", "sure", "great", "perfect",
                  "appreciated", "acknowledged", "understood", "received", "got it", "sounds good",
                  "no problem", "welcome")
COURTESY_MAX_CHARS = 120
# Connectives allowed around a courtesy phrase ("thanks for the update",
# "noted, will update the customer", "thank you in advance").
_COURTESY_FILLER = {"a", "an", "the", "for", "your", "you", "in", "advance", "all", "much", "so", "very",
                    "many", "and", "we", "i", "it", "this", "that", "is", "are", "of", "to", "on", "us",
                    "me", "again", "too", "also", "shortly", "soon", "kind", "well", "with", "by", "as",
                    "will", "be", "get", "back", "same", "then", "now", "regards", "lot"}
# Vendor / partner offers wait on OUR decision and carry no reply SLA: a
# supplier mailing a candidate profile and a monthly rate, a partner sending a
# rate card. List their domains in INBOX_VENDOR_DOMAINS. Mail from a client
# domain (INBOX_CLIENT_DOMAINS) is never a vendor mail, whatever it says about
# candidates.
VENDOR_DOMAINS = {d.strip().lower() for d in os.environ.get("INBOX_VENDOR_DOMAINS", "").split(",") if d.strip()}
# "per month", "candidate" and bare "resume" are deliberately not here: a
# client can write "your rate per month" or "we can resume the migration", so
# only phrases that mean an offer from a supplier stay. Extend with
# INBOX_VENDOR_WORDS (comma-separated, lower-case).
VENDOR_WORDS = ("profile attached", "please find the attached profile", "attached profile",
                "our rate", "rate card", "cv attached", "resume attached", "candidate profile") + tuple(
    w.strip().lower() for w in os.environ.get("INBOX_VENDOR_WORDS", "").split(",") if w.strip())
# Senders that are systems, not people (prefix match on the address), and
# phrases that mark a portal notice (a pre-billing upload confirmation, a
# referral notice) as system mail rather than a request to us.
SYSTEM_SENDERS = tuple(s.strip().lower() for s in os.environ.get(
    "INBOX_SYSTEM_SENDERS", "no-reply@,noreply@,mailer-daemon,donotreply@").split(",") if s.strip())
SYSTEM_PHRASES = tuple(p.strip().lower() for p in os.environ.get("INBOX_SYSTEM_PHRASES", "").split(",") if p.strip())
SWEPT_FILE = CTX / "escalated_requests.json"
_sweep_count = [0]
# One subscription pass at a time, and lifecycle events renew ONE subscription
# instead of triggering a full pass: with 200+ hourly chat subscriptions the
# old behaviour ran overlapping passes every few seconds and Graph throttled
# everything (6,759 errors in 25 minutes on 2026-09-06).
_pass_lock = threading.Lock()
_pass_requested = [False]
_throttled_until = [0.0]

PORT = int(os.environ.get("INBOX_PORT", 8898))
ME_EMAIL = os.environ.get("INBOX_ME_EMAIL", "").lower()          # your mailbox address
# Lower-case parts of your name; a display name or address containing ALL of
# them is you ("jane,doe").
ME_NAMES = tuple(n.strip().lower() for n in os.environ.get("INBOX_ME_NAMES", "").split(",") if n.strip())
# Your WhatsApp contact name(s) as seen from your own account, lower-case,
# comma-separated: a 1:1 message whose author is one of these came from your
# other phone.
ME_WA_NAMES = {n.strip().lower() for n in os.environ.get("INBOX_ME_WA_NAMES", "").split(",") if n.strip()}
# Extra phrases in a group that mean you were addressed ("jane doe,jane ji"),
# lower-case, comma-separated. "@<first name>" is always one.
ME_ALIASES = tuple(a.strip().lower() for a in os.environ.get("INBOX_ME_ALIASES", "").split(",") if a.strip())
ME_ID = os.environ.get("INBOX_ME_ID", "")                          # Entra user id, for Teams mentions
# Our own WhatsApp ids as they appear in @mentions (lid and/or phone digits).
# Without them, someone tagging a third person came through as "you were
# mentioned" because any mention matched.
ME_WA_IDS = {x.strip() for x in os.environ.get("INBOX_ME_WA_IDS", "").split(",") if x.strip()}
OWN_DOMAIN = os.environ.get("INBOX_OWN_DOMAIN", ME_EMAIL.split("@")[-1])
# Microsoft Graph Command Line Tools: a first-party public client that
# supports device-code sign-in with delegated Graph scopes, so no app
# registration is needed in the tenant.
CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
TENANT = os.environ.get("INBOX_TENANT", "common")
AUTHORITY = f"https://login.microsoftonline.com/{TENANT}"
# Mail.Read.Shared and the calendar scopes let the receiver read a shared
# mailbox (a projects@ style inbox) and the calendar; Mail.Send.Shared sends
# from that shared mailbox; ChatMessage.Send lets the agent post Teams messages
# with real @mentions through Graph (the desktop app cannot); the channel
# scopes cover Teams channels. Adding a scope means a new device-code sign-in.
SCOPES = ["Mail.Read", "Mail.Read.Shared", "Mail.Send.Shared",
          "Calendars.Read", "Calendars.Read.Shared",
          "Chat.Read", "ChatMessage.Read", "ChatMessage.Send",
          "ChannelMessage.Read.All", "Team.ReadBasic.All", "Channel.ReadBasic.All",
          "User.Read"]
GRAPH = "https://graph.microsoft.com/v1.0/"
MAIL_SUB_MIN = 4200          # Graph max for mail is 4230 minutes
CHAT_SUB_MIN = 55            # Graph max for chat messages is 60 minutes
RENEW_MARGIN_MIN = 12
INJECT_MIN_GAP_S = 45
IMPORTANT_WORDS = ("urgent", "asap", "deadline", "invoice", "payment", "quote",
                   "confirm", "reminder", "overdue", "please advise", "escalat",
                   "immediately", "today", "tomorrow")

_lock = threading.Lock()
_last_inject = 0.0
_inject_buf = []          # list of (line, auto_escalate)
_chat_topic_cache = {}
_seen_ids = []
_state = {"started": time.time(), "events": 0, "last_event": None,
          "token_ok": False, "subs": {}, "errors": 0}


def log(msg):
    line = f"[{datetime.now():%Y-%m-%dT%H:%M:%S}] {msg}\n"
    with open(LOG, "a") as f:
        f.write(line)
    sys.stdout.write(line); sys.stdout.flush()


def secret():
    if not SECRET_FILE.exists():
        SECRET_FILE.write_text(hashlib.sha256(os.urandom(32)).hexdigest())
        os.chmod(SECRET_FILE, 0o600)
    return SECRET_FILE.read_text().strip()


def public_url():
    try:
        u = PUBLIC_URL_FILE.read_text().strip().rstrip("/")
        return u if u.startswith("https://") else ""
    except OSError:
        return ""


# ---- MSAL ------------------------------------------------------------------
def _msal_app():
    import msal
    cache = msal.SerializableTokenCache()
    if TOKEN_CACHE.exists():
        cache.deserialize(TOKEN_CACHE.read_text())
    app = msal.PublicClientApplication(CLIENT_ID, authority=AUTHORITY, token_cache=cache)
    return app, cache


def _save_cache(cache):
    if cache.has_state_changed:
        TOKEN_CACHE.write_text(cache.serialize())
        os.chmod(TOKEN_CACHE, 0o600)


def token(interactive=False):
    app, cache = _msal_app()
    accounts = app.get_accounts()
    result = app.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None
    if not result and interactive:
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"device flow failed: {flow}")
        SIGNIN_FILE.write_text(flow["message"] + "\n")
        log(f"SIGN-IN NEEDED: {flow['message']}")
        print(flow["message"], flush=True)
        result = app.acquire_token_by_device_flow(flow)
        SIGNIN_FILE.unlink(missing_ok=True)
    _save_cache(cache)
    if result and "access_token" in result:
        _state["token_ok"] = True
        return result["access_token"]
    _state["token_ok"] = False
    return None


def graph(method, path, body=None, tok=None):
    tok = tok or token()
    if not tok:
        raise RuntimeError("no Graph token (run --login)")
    url = path if path.startswith("http") else GRAPH + path.lstrip("/")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {e.code} {method} {path.split('?')[0]}: {body}") from None


# ---- Subscriptions ---------------------------------------------------------
def _subs_load():
    try:
        return json.loads(SUBS_FILE.read_text())
    except Exception:
        return {}


def _subs_save(d):
    SUBS_FILE.write_text(json.dumps(d, indent=2))


def _expiry(minutes):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


# Microsoft returns 403 for a delegated subscription on me/chats/getAllMessages
# even with Chat.Read granted (verified 2026-09-06, scp decoded from the token).
# Per-chat subscriptions on chats/{id}/messages ARE allowed, so subscribe to
# the most recently active chats and refresh that list every hour.
# Coverage is not a fixed number: every chat active in the last ACTIVE_DAYS gets
# a live subscription (capped at CHAT_MAX, most recent first), the full chat
# list is re-read every CHAT_REFRESH_S, and a chat that received a message
# without a subscription yet (new chat, or dormant one waking up) has its new
# messages fetched at that refresh and is subscribed from then on. Measured
# 2026-09-06: 1479 chats total, 239 active in 90 days, 64 in 7 days.
CHAT_ACTIVE_DAYS = int(os.environ.get("INBOX_CHAT_ACTIVE_DAYS", 90))
# Graph refused (403) to create more than ~90-95 chat subscriptions for this
# user, so live push covers the most active chats up to CHAT_MAX and the
# catch-up below covers every other chat within CATCHUP_S seconds.
CHAT_MAX = int(os.environ.get("INBOX_CHAT_MAX", 90))
CATCHUP_S = int(os.environ.get("INBOX_CATCHUP_S", 120))
CHAT_REFRESH_S = int(os.environ.get("INBOX_CHAT_REFRESH_S", 600))
CATCHUP_FILE = CTX / "catchup_state.json"
# Mail folders to watch. Rules move client mail (a per-client folder) out of
# the Inbox, so a subscription on the Inbox alone misses it. Names are
# resolved to ids at the top level and one level under the Inbox.
MAIL_FOLDERS = [f.strip() for f in os.environ.get("INBOX_MAIL_FOLDERS", "Inbox,Clients").split(",") if f.strip()]
_mail_res_at = 0.0
_mail_res = []


def _find_folder(name):
    for path in ("me/mailFolders?$top=200&$select=id,displayName",
                 "me/mailFolders/inbox/childFolders?$top=200&$select=id,displayName"):
        for f in graph("GET", path).get("value", []):
            if (f.get("displayName") or "").lower() == name.lower():
                return f["id"]
    return None


def mail_resources():
    global _mail_res_at, _mail_res
    if time.time() - _mail_res_at < 3600 and _mail_res:
        return _mail_res
    out = []
    for name in MAIL_FOLDERS:
        if name.lower() == "inbox":
            out.append(("mail:Inbox", "me/mailFolders('Inbox')/messages")); continue
        fid = _find_folder(name)
        if fid:
            out.append((f"mail:{name}", f"me/mailFolders('{fid}')/messages"))
        else:
            log(f"subscriptions: mail folder {name!r} not found")
    _mail_res, _mail_res_at = out, time.time()
    return out
_chat_list_at = 0.0
_chat_list = []


def _all_chats():
    """Every chat, newest activity first: [(id, topic, last_message_iso)]."""
    out = []
    url = "me/chats?$top=50&$select=id,topic,chatType&$expand=lastMessagePreview"
    while url:
        page = graph("GET", url)
        for c in page.get("value", []):
            last = ((c.get("lastMessagePreview") or {}).get("createdDateTime")) or ""
            out.append((c["id"], c.get("topic") or c.get("chatType") or "chat", last))
        url = page.get("@odata.nextLink")
    out.sort(key=lambda t: t[2], reverse=True)
    return out


def chat_targets():
    """Chats to subscribe to: active within CHAT_ACTIVE_DAYS, capped at CHAT_MAX.
    Refreshes the full list every CHAT_REFRESH_S and runs the catch-up."""
    global _chat_list_at, _chat_list
    if time.time() - _chat_list_at < CHAT_REFRESH_S and _chat_list:
        return _chat_list
    allc = _all_chats()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=CHAT_ACTIVE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    active = [(i, t) for i, t, last in allc if last >= cutoff][:CHAT_MAX]
    try:
        catchup_new_chats(allc)
    except Exception as e:
        log(f"catch-up error: {e}")
    _chat_list, _chat_list_at = active, time.time()
    log(f"chats: {len(allc)} total, {len(active)} active within {CHAT_ACTIVE_DAYS}d -> subscribed set")
    return _chat_list


def catchup_new_chats(allc):
    """Record messages that arrived in chats we had no subscription for."""
    try:
        state = json.loads(CATCHUP_FILE.read_text())
    except Exception:
        state = {}
    since = state.get("since") or (datetime.now(timezone.utc) - timedelta(seconds=CHAT_REFRESH_S)).strftime("%Y-%m-%dT%H:%M:%SZ")
    subs = _subs_load()
    n_chats = n_msgs = 0
    for chat_id, topic, last in allc:
        if not last or last <= since or ("chat:" + chat_id) in subs:
            continue
        try:
            msgs = graph("GET", f"chats/{chat_id}/messages?$top=50").get("value", [])
        except Exception as e:
            log(f"catch-up: {topic!r} failed: {str(e)[:80]}"); continue
        n_chats += 1
        for m in reversed(msgs):
            if (m.get("createdDateTime") or "") <= since or m.get("messageType") != "message":
                continue
            frm = ((m.get("from") or {}).get("user") or {})
            if _is_me(frm.get("displayName", "")):
                continue
            info = _chat_topic(chat_id)
            record({"channel": "teams", "id": m.get("id"), "ts": m.get("createdDateTime"),
                    "from": frm.get("displayName", ""), "from_name": frm.get("displayName", ""),
                    "chat": info["topic"], "chat_type": info["type"], "chat_id": chat_id,
                    "text": _strip_html((m.get("body") or {}).get("content", ""))[:400],
                    "mentions": [x.get("mentionText", "") for x in m.get("mentions", [])],
                    "mention_ids": [((x.get("mentioned") or {}).get("user") or {}).get("id", "") for x in m.get("mentions", [])],
                    "catchup": True})
            n_msgs += 1
    CATCHUP_FILE.write_text(json.dumps({"since": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}))
    if n_chats:
        log(f"catch-up: {n_msgs} message(s) from {n_chats} unsubscribed chat(s)")


def renew_one(sub_id):
    subs = _subs_load()
    for key, v in subs.items():
        if v.get("id") == sub_id:
            minutes = MAIL_SUB_MIN if key.startswith("mail:") else CHAT_SUB_MIN
            try:
                graph("PATCH", f"subscriptions/{sub_id}", {"expirationDateTime": _expiry(minutes)})
                v["expiration"] = _expiry(minutes); _subs_save(subs)
                return True
            except Exception as e:
                log(f"renew_one {key}: {str(e)[:100]}")
                if "429" in str(e):
                    _throttled_until[0] = time.time() + 120
                return False
    return False


def ensure_subscriptions():
    if time.time() < _throttled_until[0]:
        return log("subscriptions: throttled, pass skipped")
    if not _pass_lock.acquire(blocking=False):
        return log("subscriptions: pass already running, skipped")
    try:
        _ensure_subscriptions_inner()
    finally:
        _pass_lock.release()


def _ensure_subscriptions_inner():
    url = public_url()
    if not url:
        log("subscriptions: waiting for public_url.txt")
        return
    subs = _subs_load()
    if "mail" in subs:                       # migrate the pre-folder record
        subs["mail:Inbox"] = subs.pop("mail")
    now = datetime.now(timezone.utc)
    try:
        mail_specs = [(k, (r, MAIL_SUB_MIN)) for k, r in mail_resources()]
    except Exception as e:
        mail_specs = [("mail:Inbox", ("me/mailFolders('Inbox')/messages", MAIL_SUB_MIN))]
        log(f"subscriptions: folder resolve failed ({e}); Inbox only this pass")
    for name, (resource, minutes) in mail_specs:
        cur = subs.get(name)
        try:
            if cur:
                exp = datetime.fromisoformat(cur["expiration"].replace("Z", "+00:00"))
                if exp - now > timedelta(minutes=RENEW_MARGIN_MIN):
                    continue
                try:
                    graph("PATCH", f"subscriptions/{cur['id']}", {"expirationDateTime": _expiry(minutes)})
                    cur["expiration"] = _expiry(minutes)
                    _subs_save(subs)
                    log(f"subscriptions: renewed {name}")
                    continue
                except Exception as e:
                    log(f"subscriptions: renew {name} failed ({e}); recreating")
            res = graph("POST", "subscriptions", {
                "changeType": "created", "notificationUrl": url + "/graph",
                "resource": resource, "expirationDateTime": _expiry(minutes),
                "clientState": secret()})
            subs[name] = {"id": res["id"], "expiration": res["expirationDateTime"], "resource": resource}
            _subs_save(subs)
            log(f"subscriptions: created {name} {res['id']} until {res['expirationDateTime']}")
        except Exception as e:
            _state["errors"] += 1
            log(f"subscriptions: {name} error: {e}")
    # Per-chat subscriptions.
    try:
        targets = chat_targets()
    except Exception as e:
        targets = []
        log(f"subscriptions: chat list failed: {e}")
    made = renewed = 0
    for chat_id, topic in targets:
        key = "chat:" + chat_id
        cur = subs.get(key)
        try:
            if cur:
                exp = datetime.fromisoformat(cur["expiration"].replace("Z", "+00:00"))
                if exp - now > timedelta(minutes=RENEW_MARGIN_MIN):
                    continue
                try:
                    graph("PATCH", f"subscriptions/{cur['id']}", {"expirationDateTime": _expiry(CHAT_SUB_MIN)})
                    cur["expiration"] = _expiry(CHAT_SUB_MIN); renewed += 1
                    _subs_save(subs)
                    continue
                except Exception:
                    pass
            res = graph("POST", "subscriptions", {
                "changeType": "created", "notificationUrl": url + "/graph",
                "resource": f"chats/{chat_id}/messages", "expirationDateTime": _expiry(CHAT_SUB_MIN),
                "lifecycleNotificationUrl": url + "/graph",
                "clientState": secret()})
            subs[key] = {"id": res["id"], "expiration": res["expirationDateTime"],
                         "resource": f"chats/{chat_id}/messages", "topic": topic}
            _subs_save(subs)
            made += 1
            time.sleep(0.3)
        except Exception as e:
            _state["errors"] += 1
            log(f"subscriptions: chat {topic!r} error: {str(e)[:120]}")
            if "429" in str(e):
                _throttled_until[0] = time.time() + 120
                log("subscriptions: throttled by Graph, pausing 2 minutes")
                break
            if "403" in str(e) and "Create" in str(e):
                # Per-user cap on chat subscriptions reached (~100). Older ones that
                # fell out of the active set are no longer renewed and expire within
                # the hour, freeing slots; do not hammer Graph meanwhile.
                waiting = [c for c, _ in targets if ("chat:" + c) not in subs]
                log(f"subscriptions: cap reached; {len(waiting)} active chat(s) wait for expiring slots")
                break
    # Forget chat subscriptions that have expired and fell out of the top list.
    for key in [k for k, v in subs.items() if k.startswith("chat:")]:
        try:
            if datetime.fromisoformat(subs[key]["expiration"].replace("Z", "+00:00")) < now:
                del subs[key]
        except Exception:
            pass
    if made or renewed:
        log(f"subscriptions: chats created={made} renewed={renewed} active={sum(1 for k in subs if k.startswith('chat:'))}")
    _subs_save(subs)
    _state["subs"] = {k: v.get("expiration") for k, v in subs.items() if k.startswith("mail:")}
    _state["subs"]["chats"] = sum(1 for k in subs if k.startswith("chat:"))


# Where the quoted thread starts in a bodyPreview: Outlook "From:/Sent:" (and
# the ________ rule above them), French "De :/Envoyé :", German "Von:/Gesendet:",
# "-----Original Message-----", "On ... wrote:", "Le ... a écrit :", "> " quotes.
# A corporate "Classification: Internal" line comes BEFORE the real text and is
# dropped separately, never cut on.
_QUOTED_RE = re.compile(r"^[ \t]*(?:(?:from|sent|de|envoy\u00e9|von|gesendet)[ \t]?:|-{2,}[ \t]*(?:original|forwarded|urspr\u00fcngliche)"
                        r"|_{5,}[ \t]*$|>|on\b[^\n]{0,160}(?:\n[^\n]{0,120})?\bwrote:|le\b[^\n]{0,160}\ba \u00e9crit[ \t]*:)",
                        re.I | re.M)
# A signature starts at a regards-family phrase ("Thanks & Regards", "Best
# regards,", "Regards, Jane", "Cordialement"), a phone-mail footer, or the
# sender's own name on a line (a "Thank you!" followed by a bare name had no
# regards line at all). "with regards to the ticket" is not a sign-off.
_SIGNOFF_RE = re.compile(r"(?:^|[\s,.!])(?:(?:many|best|kind|warm)\s+)?(?:thanks?\s*(?:&|and)?\s*|thank you\s*(?:&|and)?\s*)?"
                         r"(?:regards|rgds)\b(?!\s+(?:to|the)\b)"
                         r"|(?:^|\n)[ \t]*(?:(?:best|cheers|sincerely|yours (?:sincerely|faithfully)|cordialement|bien (?:\u00e0|a) vous"
                         r"|(?:mit )?freundlichen? gr(?:\u00fc|ue)(?:\u00df|ss)en?|mfg|viele gr(?:\u00fc|ue)(?:\u00df|ss)e)"
                         r"(?:[ \t]*(?:\n|$)|[,.!][^\n]{0,30}(?:\n|$))"
                         r"|(?:sent from my|get outlook for|please excuse any typo)\b)", re.I)
_GREETING_RE = re.compile(r"^(?:hi|hello|hey|dear|good (?:morning|afternoon|evening)|bonjour|hallo|salam)\b[^,\n]{0,40}[,:!.]?\s*", re.I)
_PLEASANTRY_RE = re.compile(r"(?:i\s+)?(?:hope|trust)\s+(?:you\s+are|you're|this\s+(?:e-?mail|message)\s+finds\s+you)\s+(?:all\s+)?(?:doing\s+)?(?:well|fine|good|great)[\s.!,]*", re.I)
_GENERIC_NAMES = {"team", "all", "sir", "madam", "everyone", "both", "colleagues", "there", "mr", "mrs", "ms", "miss", "dr"}
_COURTESY_RE = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in sorted(COURTESY_WORDS, key=len, reverse=True)) + r")\b[.!,]*")
_ASK_RE = re.compile(r"\b(?:please|pls|kindly|could you|can you|would you|let (?:me|us) know)\b")
_VENDOR_RE = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in VENDOR_WORDS) + r")\b")


def _name_tokens(addrs):
    """Lower-case word tokens of the display names and address local parts of
    a list of Graph emailAddress dicts ("Doe, Jane" -> {doe, jane})."""
    out = set()
    for a in addrs:
        out |= set(re.findall(r"[a-z]+", ((a or {}).get("name") or "").lower()))
        out |= set(re.findall(r"[a-z]+", ((a or {}).get("address") or "").lower().split("@")[0]))
    return out


def _is_name_line(line, names):
    toks = re.findall(r"[a-z]+", line.lower())
    return bool(toks) and len(toks) <= 5 and any(t in names for t in toks) and all(t in names or t in _GENERIC_NAMES for t in toks)


def _own_text(m):
    """The sender's own words in a message: bodyPreview minus the outside-mail
    banner, the "Classification:" line, everything from the first quoted-header
    marker, the greeting / recipient name line, and the signature (a sign-off
    phrase or the sender's name line, and all below). Lower-case, one line."""
    t = (m.get("bodyPreview") or "").replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"\[\s*(?:caution|external|warning)\b[^\]]*\]?", " ", t, flags=re.I)
    t = re.sub(r"^[ \t]*(?:caution|classification)\s*:[^\n]*\n?", "", t, flags=re.I | re.M)
    for rx in (_QUOTED_RE, _SIGNOFF_RE):
        hit = rx.search(t)
        if hit:
            t = t[:hit.start()]
    t = _PLEASANTRY_RE.sub(" ", _GREETING_RE.sub("", t.strip()))
    lines = [l.strip() for l in t.split("\n") if l.strip()]
    hello = _name_tokens([(r.get("emailAddress") or {}) for r in (m.get("toRecipients") or [])])
    if lines and _is_name_line(lines[0], hello):
        lines = lines[1:]
    sender = _name_tokens([(m.get("from") or {}).get("emailAddress") or {}])
    for i, l in enumerate(lines):
        if _is_name_line(l, sender):
            lines = lines[:i]; break
    return re.sub(r"\s+", " ", " ".join(lines)).strip().lower()


def _is_courtesy(own):
    """True when the sender's own words are a short closing: nothing left but
    COURTESY_WORDS and connectives (at most two other words), no question and
    no please/kindly ("Noted, please also send the CMR" is still a request)."""
    if not own or len(own) > COURTESY_MAX_CHARS or "?" in own or _ASK_RE.search(own):
        return False
    rest = _COURTESY_RE.sub(" ", own)
    if rest == own:
        return False
    return len([w for w in re.findall(r"[a-z0-9']+", rest) if w not in _COURTESY_FILLER]) <= 2


def _is_request(m, folder):
    frm = ((m.get("from") or {}).get("emailAddress") or {}).get("address", "").lower()
    if not frm or frm.endswith("@" + OWN_DOMAIN) or frm.startswith(SYSTEM_SENDERS):
        return False
    blob = ((m.get("subject") or "") + " " + (m.get("bodyPreview") or "")).lower()
    if "automatic reply" in blob or "out of office" in blob:
        return False
    # Meeting responses and cancellations are calendar traffic, not requests
    # (a "Canceled: weekly connect" once sat in the sweep as unanswered for 3 h).
    subj = (m.get("subject") or "").strip().lower()
    if subj.startswith(("canceled:", "cancelled:", "accepted:", "declined:", "tentative:",
                        "updated invitation", "invitation:")):
        return False
    # Portal notices addressed to a client's own approver (pre-billing uploads,
    # referrals) are system mail, not a request to us: INBOX_SYSTEM_PHRASES.
    if any(p in blob for p in SYSTEM_PHRASES):
        return False
    # Vendor / partner offers (profiles, rates) are ours to decide on, not
    # client requests: see VENDOR_DOMAINS / VENDOR_WORDS.
    if frm.rsplit("@", 1)[-1] in VENDOR_DOMAINS:
        return False
    own = _own_text(m)
    if not any(frm.endswith("@" + d) for d in CLIENT_DOMAINS) and _VENDOR_RE.search(own):
        return False
    # Courtesy closings ("Thank you!", "Noted, will update") are not requests,
    # whatever the quoted thread below them says.
    if _is_courtesy(own):
        return False
    return folder in CLIENT_FOLDERS or any(w in blob for w in QUOTE_WORDS)


def sweep_unanswered_requests():
    """Every 30 min: external requests received in the last 24h with no reply
    from our domain in the same conversation after QUOTE_SLA_H hours."""
    try:
        swept = json.loads(SWEPT_FILE.read_text())
    except Exception:
        swept = {}
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    found = 0
    for name, res in mail_resources():
        folder = name.split(":", 1)[1]
        base = res.split("/messages")[0]
        url = (f"{base}/messages?$filter=receivedDateTime%20ge%20{since}&$orderby=receivedDateTime%20desc&$top=100"
               "&$select=id,subject,from,toRecipients,receivedDateTime,bodyPreview,conversationId,webLink")
        try:
            msgs = graph("GET", url).get("value", [])
        except Exception as e:
            log(f"sweep: {folder} list failed: {str(e)[:100]}"); continue
        seen_conv = set()
        for m in msgs:
            conv = m.get("conversationId")
            if not conv or conv in seen_conv or not _is_request(m, folder):
                continue
            seen_conv.add(conv)
            rcv = datetime.fromisoformat(m["receivedDateTime"].replace("Z", "+00:00"))
            age_h = (now - rcv).total_seconds() / 3600
            if age_h < QUOTE_SLA_H or conv in swept:
                continue
            # Only messages in this conversation sent AFTER the request, and follow
            # paging: a long-running ticket thread has 70+ messages and an unordered
            # $top=50 returned the oldest ones, hiding a reply sent 8 minutes after
            # the request.
            try:
                flt = urllib.parse.quote(f"conversationId eq '{conv}' and sentDateTime ge {m['receivedDateTime']}")
                url_t = f"me/messages?$filter={flt}&$select=from,sentDateTime,receivedDateTime&$top=50"
                thread = []
                while url_t:
                    page = graph("GET", url_t)
                    thread += page.get("value", [])
                    url_t = page.get("@odata.nextLink")
            except Exception as e:
                log(f"sweep: thread lookup failed: {str(e)[:100]}"); continue
            answered = any((((t.get("from") or {}).get("emailAddress") or {}).get("address", "").lower().endswith("@" + OWN_DOMAIN))
                           and (t.get("sentDateTime") or t.get("receivedDateTime") or "") > m["receivedDateTime"] for t in thread)
            if not answered:
                # Answered on a sibling thread: a client's forward (its own
                # conversationId) was answered 2 minutes later on the original
                # thread and still flagged. Anything we sent to that sender
                # within QUOTE_SLA_H of the request counts as the answer.
                sender = (((m.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower()
                until = (rcv + timedelta(hours=QUOTE_SLA_H)).strftime("%Y-%m-%dT%H:%M:%SZ")
                try:
                    flt = urllib.parse.quote(f"sentDateTime ge {m['receivedDateTime']}")
                    url_s = f"me/mailFolders/sentitems/messages?$filter={flt}&$select=toRecipients,ccRecipients,sentDateTime,subject&$top=50"
                    sent = []
                    while url_s:
                        page = graph("GET", url_s)
                        sent += page.get("value", [])
                        url_s = page.get("@odata.nextLink")
                except Exception as e:
                    log(f"sweep: sent items lookup failed: {str(e)[:100]}"); continue
                for s in sent:
                    rcpts = [((r.get("emailAddress") or {}).get("address") or "").lower()
                             for r in (s.get("toRecipients") or []) + (s.get("ccRecipients") or [])]
                    if sender in rcpts and m["receivedDateTime"] < (s.get("sentDateTime") or "") <= until:
                        log(f"sweep: {folder} {(m.get('subject') or '')[:60]!r} from {sender} answered on another thread "
                            f"({(s.get('subject') or '')[:60]!r} at {s.get('sentDateTime')})")
                        answered = True; break
            if answered:
                continue
            frm = (m.get("from") or {}).get("emailAddress") or {}
            item = {"channel": "email", "id": "unanswered:" + m["id"], "ts": m["receivedDateTime"],
                    "from": frm.get("address", ""), "from_name": frm.get("name", ""),
                    "to": [r["emailAddress"]["address"] for r in m.get("toRecipients", [])], "cc": [],
                    "subject": f"UNANSWERED {age_h:.0f}h ({folder}): " + (m.get("subject") or ""),
                    "text": (m.get("bodyPreview") or "")[:300], "importance": "high",
                    "link": m.get("webLink"), "thread": conv}
            swept[conv] = now.isoformat()
            record(item)
            found += 1
    SWEPT_FILE.write_text(json.dumps(swept, indent=1))
    log(f"sweep: unanswered client requests flagged: {found}")


def catchup_tick():
    """Every CATCHUP_S: the 50 most recently active chats (one call). Any of them
    with a message newer than the last tick and no live subscription gets its
    new messages fetched and recorded. This is what makes coverage independent
    of the subscription cap: a message in ANY chat is seen within CATCHUP_S."""
    res = graph("GET", "me/chats?$top=50&$select=id,topic,chatType&$expand=lastMessagePreview"
                       "&$orderby=lastMessagePreview/createdDateTime%20desc")
    allc = []
    for c in res.get("value", []):
        last = ((c.get("lastMessagePreview") or {}).get("createdDateTime")) or ""
        allc.append((c["id"], c.get("topic") or c.get("chatType") or "chat", last))
    catchup_new_chats(allc)


def renewal_loop():
    last = 0.0
    last_catchup = 0.0
    while True:
        if time.time() - last_catchup >= CATCHUP_S:
            last_catchup = time.time()
            try:
                catchup_tick()
            except Exception as e:
                log(f"catch-up tick error: {str(e)[:120]}")
        if _pass_requested[0] or time.time() - last >= 300:
            _pass_requested[0] = False; last = time.time()
            try:
                ensure_subscriptions()
            except Exception as e:
                log(f"renewal loop error: {e}")
            if _sweep_count[0] % 6 == 0:             # every 30 minutes
                try:
                    sweep_unanswered_requests()
                except Exception as e:
                    log(f"sweep error: {e}")
            _sweep_count[0] += 1
        time.sleep(60)


# ---- Normalisation ---------------------------------------------------------
def _strip_html(s):
    s = re.sub(r"<at[^>]*>(.*?)</at>", r"@\1", s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_own_posts = {}          # chat jid -> time of our last message there
# A tag can sit in the line, come before the message, or come after it.
# Remember who tagged us where, and treat that sender's
# messages in the same chat for MENTION_AFTER_S as part of the request; on a
# tag, also pull that sender's messages from the previous MENTION_BEFORE_S.
_mentioned_by = {}       # (chat key, sender) -> time of their last tag of us
MENTION_AFTER_S = float(os.environ.get("INBOX_MENTION_AFTER_S", 10 * 60))
MENTION_BEFORE_S = float(os.environ.get("INBOX_MENTION_BEFORE_S", 3 * 60))


def _recent_from_sender(chat_key, sender, seconds):
    """Texts this sender posted in this chat within the last `seconds`, oldest
    first, from the digest (the message-then-tag case)."""
    out = []
    try:
        cutoff = time.time() - seconds
        for l in open(INBOX):
            try:
                r = json.loads(l)
            except Exception:
                continue
            if (r.get("from") or "") != sender:
                continue
            if (r.get("chat_jid") or r.get("chat_id") or r.get("chat") or "") != chat_key:
                continue
            try:
                ts = datetime.fromisoformat(str(r.get("ts", "")).replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if ts >= cutoff and (r.get("text") or "").strip() and not r.get("from_me"):
                out.append((r.get("text") or "").strip())
    except Exception:
        pass
    return out[-6:]


# Presence flag written by the menu-bar indicator: {"at_desk": bool, "since": iso}.
PRESENCE_PATH = Path.home() / ".voicemode" / "presence.json"
_presence_state = {"at_desk": None}


def _presence_watch():
    """Tell the agent when the user flips the menu-bar At desk / Away toggle.

    The menu bar writes presence.json silently, so the agent only learned of a
    change if it happened to read the file; the user came back to the desk and
    the agent kept reporting to their phone. Now a change is injected like any
    other event."""
    while True:
        try:
            cur = json.loads(PRESENCE_PATH.read_text()).get("at_desk")
        except Exception:
            cur = None
        if cur is not None and _presence_state["at_desk"] is not None and cur != _presence_state["at_desk"]:
            where = "AT DESK" if cur else "AWAY"
            how = ("reply in the session chat, the user is at the desk"
                   if cur else ("reply on WhatsApp +" + ESCALATE_TO if ESCALATE_TO else "hold non-urgent items")
                   + ", the user is away")
            _inject_buf.append((f"[presence] The user is now {where}: {how}.", False))
            _inject_later()
            log(f"presence: changed to {where}")
        if cur is not None:
            _presence_state["at_desk"] = cur
        time.sleep(20)


AGENT_SENT_IDS = Path.home() / ".voicemode" / "agent_sent_ids.jsonl"


def mark_agent_sent(message_id, chat="", text=""):
    """Record what the AGENT sent. The user and the agent post from the same
    account, so fromMe alone cannot tell them apart; without this the agent
    reads its own messages back as if the user had written them. The bridge
    returns no message id, so the exact text is the key."""
    if not (message_id or text):
        return
    try:
        with open(AGENT_SENT_IDS, "a") as f:
            f.write(json.dumps({"id": message_id, "chat": chat,
                                "text": (text or "")[:400],
                                "ts": datetime.now(timezone.utc).isoformat()}) + "\n")
    except OSError:
        pass


def _agent_sent_keys():
    """(ids, normalised texts) the agent sent in the last 24 h."""
    ids, texts = set(), set()
    cutoff = time.time() - 24 * 3600
    try:
        for l in open(AGENT_SENT_IDS):
            try:
                r = json.loads(l)
            except Exception:
                continue
            try:
                ts = datetime.fromisoformat(str(r.get("ts", "")).replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = cutoff
            if ts < cutoff:
                continue
            if r.get("id"):
                ids.add(r["id"])
            t = " ".join((r.get("text") or "").split())[:160]
            if t:
                texts.add(t)
    except OSError:
        pass
    return ids, texts


OWN_POST_WINDOW_S = float(os.environ.get("INBOX_OWN_POST_WINDOW_S", 24 * 3600))


def _seed_own_posts():
    """The thread-reply rule lived only in memory and a restart emptied it, so
    an engineer's reply 2 h after our post was filed as chatter.
    Seed from the digest: every message from our side in the last day."""
    try:
        cutoff = time.time() - OWN_POST_WINDOW_S
        for l in open(INBOX):
            try:
                r = json.loads(l)
            except Exception:
                continue
            if r.get("from_me") or _is_me(r.get("from", "")):
                try:
                    ts = datetime.fromisoformat(str(r.get("ts", "")).replace("Z", "+00:00")).timestamp()
                except Exception:
                    continue
                if ts >= cutoff:
                    key = r.get("chat_jid") or r.get("chat_id") or r.get("chat") or ""
                    _own_posts[key] = max(_own_posts.get(key, 0), ts)
        log(f"own-posts seeded: {len(_own_posts)} thread(s) from the last {OWN_POST_WINDOW_S/3600:.0f} h")
    except Exception as e:
        log(f"own-posts seed failed ({e})")


def _own_post_recent(jid):
    """Ask the bridge whether we posted in this chat within the window. The
    digest holds only inbound traffic, so bridge-sent posts (two questions the
    agent asked an engineer one evening) were invisible to the seed and his
    reply the next morning was filed as chatter."""
    if not jid:
        return 0
    try:
        import urllib.parse as _up
        with urllib.request.urlopen(f"{WA_DAEMON}/messages?phone={_up.quote(jid)}&limit=40", timeout=5) as r:
            msgs = json.load(r)
        cutoff = time.time() - OWN_POST_WINDOW_S
        best = 0
        for m in msgs or []:
            if m.get("from") != "me":
                continue
            try:
                ts = datetime.fromisoformat(str(m.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if ts >= cutoff:
                best = max(best, ts)
        if best:
            _own_posts[jid] = max(_own_posts.get(jid, 0), best)
        return best
    except Exception as e:
        log(f"own-post bridge check failed for {jid}: {e}")
        return 0


def _is_me(name_or_email):
    v = (name_or_email or "").lower()
    return bool(v) and ((bool(ME_EMAIL) and ME_EMAIL in v) or (bool(ME_NAMES) and all(n in v for n in ME_NAMES)))


def _mention_regexes():
    """Text patterns that mean you were tagged. Teams: "@first last" only (a
    bare "@first" matched someone else's split mention "@Other @First" and
    raised a false "you were mentioned"; the mentioned user id is used first).
    WhatsApp groups: "@first", plus every phrase in INBOX_ME_ALIASES."""
    teams = wa = None
    if ME_NAMES:
        teams = re.compile("@" + r"\s*@?\s*".join(re.escape(n) for n in ME_NAMES) + r"\b", re.I)
        pats = ["@" + re.escape(ME_NAMES[0]) + r"\b"]
        pats += [r"\b" + re.escape(a) + r"\b" for a in ME_ALIASES]
        wa = re.compile("|".join(pats), re.I)
    return teams, wa


_ME_TEAMS_RE, _ME_WA_RE = _mention_regexes()


def fetch_mail(resource):
    m = graph("GET", resource + "?$select=subject,from,toRecipients,ccRecipients,receivedDateTime,bodyPreview,importance,webLink,conversationId")
    frm = (m.get("from") or {}).get("emailAddress") or {}
    to = [r["emailAddress"]["address"] for r in m.get("toRecipients", [])]
    cc = [r["emailAddress"]["address"] for r in m.get("ccRecipients", [])]
    return {"channel": "email", "id": m.get("id"), "ts": m.get("receivedDateTime"),
            "from": frm.get("address", ""), "from_name": frm.get("name", ""),
            "to": to, "cc": cc, "subject": m.get("subject", ""),
            "text": (m.get("bodyPreview") or "")[:400],
            "importance": m.get("importance"), "link": m.get("webLink"),
            "thread": m.get("conversationId")}


def _chat_topic(chat_id):
    if chat_id in _chat_topic_cache:
        return _chat_topic_cache[chat_id]
    try:
        c = graph("GET", f"chats/{chat_id}?$select=topic,chatType,members&$expand=members")
        names = [m.get("displayName", "") for m in c.get("members", []) if not _is_me(m.get("displayName", ""))]
        topic = c.get("topic") or (", ".join(names[:3]) or "chat")
        info = {"topic": topic, "type": c.get("chatType", "")}
    except Exception:
        info = {"topic": "chat", "type": ""}
    _chat_topic_cache[chat_id] = info
    return info


def fetch_chat(resource):
    m = graph("GET", resource)
    frm = ((m.get("from") or {}).get("user") or {})
    chat_id = m.get("chatId") or resource.split("/")[1]
    info = _chat_topic(chat_id)
    mentions = [x.get("mentionText", "") for x in m.get("mentions", [])]
    mention_ids = [((x.get("mentioned") or {}).get("user") or {}).get("id", "") for x in m.get("mentions", [])]
    return {"channel": "teams", "id": m.get("id"), "ts": m.get("createdDateTime"),
            "from": frm.get("displayName", ""), "from_name": frm.get("displayName", ""),
            "chat": info["topic"], "chat_type": info["type"], "chat_id": chat_id,
            "text": _strip_html((m.get("body") or {}).get("content", ""))[:400],
            "mentions": mentions, "mention_ids": mention_ids}


def normalise_whatsapp(evt):
    """Daemon payload (daemon.js dispatch('message', ...)): chatJid, isGroup,
    fromMe, author ('me' or push name), text, messageId, timestamp, type."""
    d = evt.get("data") or {}
    jid = d.get("chatJid") or ""
    mtype = d.get("type") or "unknown"
    text = d.get("text") or ""
    if not text or text.startswith("[media"):
        text = {"imageMessage": "[image]", "audioMessage": "[voice note]", "videoMessage": "[video]",
                "documentMessage": "[document]", "stickerMessage": "[sticker]"}.get(mtype, f"[{mtype}]")
    author = d.get("author") or ""
    from_me = bool(d.get("fromMe")) or author == "me"
    other = ((bool(ESCALATE_TO) and jid.startswith(ESCALATE_TO)) or (not from_me and not d.get("isGroup")
             and author.strip().lower() in ME_WA_NAMES))
    return {"channel": "whatsapp", "id": d.get("messageId"), "from_other_phone": other,
            "ts": d.get("timestamp") or datetime.now(timezone.utc).isoformat(),
            "from": author if not from_me else "me", "from_me": from_me,
            "chat": jid.split("@")[0] if not d.get("isGroup") else (d.get("chatName") or jid.split("@")[0]),
            "chat_jid": jid, "group": bool(d.get("isGroup")), "type": mtype,
            "text": text[:400],
            "mentions": re.findall(r"@(\w+)", text)}


# ---- Triage ----------------------------------------------------------------
def is_important(item):
    ch = item["channel"]
    text = (item.get("text") or "").lower()
    if ch == "email":
        frm = item.get("from", "").lower()
        if frm.startswith("no-reply@") or frm.startswith("noreply@") or "automatic reply" in item.get("subject", "").lower():
            return False, "automated"
        if _is_me(frm):
            return False, "own (sent by you)"
        direct = any(ME_EMAIL in a.lower() for a in item.get("to", []) + item.get("cc", []))
        subj = (item.get("subject") or "").lower()
        hot = any(w in subj or w in text for w in IMPORTANT_WORDS)
        external = not frm.endswith("@" + OWN_DOMAIN)
        if item.get("importance") == "high":
            return True, "high importance"
        if direct and (external or hot or "?" in text):
            return True, "addressed to you" + (", external" if external else "") + (", keyword" if hot else "")
        return False, "not addressed / routine"
    if ch == "teams":
        # Sender-less, text-less events (call started, member added) are Teams
        # system messages; one from a group chat was injected once.
        if not (item.get("from") or "").strip() and not (text or "").strip():
            return False, "system event"
        if _is_me(item.get("from", "")):
            _own_posts[item.get("chat_id") or item.get("chat") or ""] = time.time()
            return False, "own"
        _lastt = _own_posts.get(item.get("chat_id") or item.get("chat") or "", 0)
        if _lastt and time.time() - _lastt < OWN_POST_WINDOW_S and item.get("chat_type") != "oneOnOne":
            return True, "reply in a thread you posted in"
        if item.get("chat_type") == "oneOnOne":
            return True, "direct message"
        # Use the mentioned user id when Graph gives it; otherwise require the
        # full name (see _mention_regexes for why a bare first name is not enough).
        _flat = re.sub(r"(&nbsp;|\s)+", " ", text)
        _ck = item.get("chat_id") or item.get("chat") or ""
        _snd = item.get("from") or ""
        if (ME_ID and ME_ID in item.get("mention_ids", [])) or any(_is_me(m) for m in item.get("mentions", [])) \
                or (_ME_TEAMS_RE is not None and _ME_TEAMS_RE.search(_flat)):
            _mentioned_by[(_ck, _snd)] = time.time()
            _prev = [t for t in _recent_from_sender(_ck, _snd, MENTION_BEFORE_S) if t != text.strip()]
            if _prev:
                item["text"] = "(sent just before the tag) " + " | ".join(_prev) + " || " + text
            return True, "you were mentioned"
        _lm = _mentioned_by.get((_ck, _snd), 0)
        if _lm and time.time() - _lm < MENTION_AFTER_S:
            return True, "follow-up to their tag of you"
        return False, "group chatter"
    if ch == "whatsapp":
        # Status (story) broadcasts are not messages to us; a contact's status
        # video was injected as a "direct message" once.
        _cj = str(item.get("chat_jid") or "")
        _c = str(item.get("chat") or "")
        # Status jids come in two shapes: "status@broadcast" and the newer
        # "<lid>@lid.status" (relayed as a bare "?" from a status reply).
        # Neither is a message addressed to us.
        if (_cj.startswith("status@") or _cj.endswith("@lid.status")
                or _cj.endswith(".status") or _c in ("status", "status@broadcast")):
            return False, "status broadcast"
        # Any bare bracketed camel-case token is a WhatsApp protocol placeholder
        # ([protocolMessage], [messageContextInfo], [senderKeyDistributionMessage]...),
        # never a message. Real placeholders carry spaces or a path.
        if re.match(r"^\[[a-z]+[A-Za-z]*\]$", text.strip()) and not re.match(r"^\[(voice note|image|video|document|sticker|media)", text.strip(), re.I):
            return False, "protocol noise"
        if item.get("from_other_phone") and not item.get("from_me"):
            item["from"] = "the user (other phone)"
            try:
                ACK_FILE.touch()
            except OSError:
                pass
            return True, "your instruction via WhatsApp"
        if item.get("from_me"):
            _own_posts[item.get("chat_jid") or item.get("chat") or ""] = time.time()
            _aids, _atexts = _agent_sent_keys()
            _norm = " ".join((item.get("text") or "").split())[:160]
            if item.get("id") in _aids or (_norm and _norm in _atexts):
                item["sent_by"] = "agent"
                return False, "own (sent by the agent)"
            item["sent_by"] = "user"
            return False, "own (sent by you)"
        if not item.get("group"):
            return True, "direct message"
        # A reply in a group where our side posted within the window is part
        # of a thread we opened (an engineer's answer sat unseen as "chatter"
        # after the agent asked him a question there).
        _jid = item.get("chat_jid") or item.get("chat") or ""
        _last = _own_posts.get(_jid, 0) or _own_post_recent(_jid)
        if _last and time.time() - _last < OWN_POST_WINDOW_S:
            return True, "reply in a thread you posted in"
        _ment = [m for m in item.get("mentions", []) if m]
        _low = text.lower()
        _snd = item.get("from") or ""
        if any(m in ME_WA_IDS for m in _ment) or (_ME_WA_RE is not None and _ME_WA_RE.search(_low)):
            _mentioned_by[(_jid, _snd)] = time.time()
            _prev = [t for t in _recent_from_sender(_jid, _snd, MENTION_BEFORE_S) if t != text.strip()]
            if _prev:
                item["text"] = "(sent just before the tag) " + " | ".join(_prev) + " || " + text
            return True, "you were mentioned"
        _lm = _mentioned_by.get((_jid, _snd), 0)
        if _lm and time.time() - _lm < MENTION_AFTER_S:
            return True, "follow-up to their tag of you"
        return False, "group chatter"
    return False, "unknown"


# ---- Persistence + delivery -------------------------------------------------
def _claude_running():
    return subprocess.run(["pgrep", "-x", "Claude"], capture_output=True).returncode == 0


def _wa_send(phone, message):
    data = json.dumps({"phone": phone, "message": message}).encode()
    req = urllib.request.Request(WA_DAEMON + "/send", data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        body = r.read().decode()
    try:
        mark_agent_sent((json.loads(body) or {}).get("id"), phone, message)
    except Exception:
        pass
    return body[:120]


def _escalate_if_unacked(t0, text, force=False):
    try:
        acked = (not force) and ACK_FILE.exists() and ACK_FILE.stat().st_mtime >= t0
    except OSError:
        acked = False
    if acked:
        return log("escalation: acknowledged in the session, not sending")
    if time.time() - _last_escalation[0] < ESCALATE_MIN_GAP_S:
        return log("escalation: rate-limited, skipped")
    try:
        body = "Claude: " + text.replace("[inbox] ", "", 1) + "\n\nReply here to tell me what to do."
        res = _wa_send(ESCALATE_TO, body)
        _last_escalation[0] = time.time()
        log(f"escalation: sent to +{ESCALATE_TO} ({res})")
    except Exception as e:
        _state["errors"] += 1
        log(f"escalation: send failed: {e}")


def _check_delivered(t0, text):
    """Ninety seconds after an instruction from the second phone was handed to
    the daemon, confirm the daemon actually pasted it. If not, tell the user
    on that phone; the daemon keeps retrying. Three instructions were lost
    silently on 2026-09-06 before this existed."""
    try:
        ok = DELIVERED.exists() and DELIVERED.stat().st_mtime >= t0
    except OSError:
        ok = False
    if ok:
        return
    try:
        _wa_send(ESCALATE_TO, "Got your message. It reached the Mac, but the Claude session has not picked it up yet. "
                              "It is being retried; I will reply as soon as it lands.")
        log("delivery watch: session did not confirm the paste; user notified on WhatsApp")
    except Exception as e:
        log(f"delivery watch: notify failed: {e}")


# Quiet mode ("do this digest in the background"): when
# ~/.voicemode/quiet.json exists, nothing is injected into the Claude session
# and nothing is escalated. Everything still lands in the digest and the log,
# so the agent reads it on demand. Delete the file to turn injection back on.
QUIET_FLAG = Path.home() / ".voicemode" / "quiet.json"


def _quiet() -> bool:
    return QUIET_FLAG.exists()


def _flush_inject():
    global _last_inject, _inject_buf
    if not _inject_buf:
        return
    if _quiet():
        n = len(_inject_buf); _inject_buf = []
        log(f"inject: quiet mode, {n} item(s) logged only")
        return
    items = _inject_buf; _inject_buf = []
    lines = [l for l, _ in items]
    auto = any(e for _, e in items)
    text = "[inbox] " + (" | ".join(lines) if len(lines) > 1 else lines[0])
    if not _claude_running():
        with open(PENDING, "a") as f:
            for l in lines:
                f.write(json.dumps({"ts": time.time(), "line": l}) + "\n")
        log(f"inject: Claude not running; queued {len(lines)} important item(s); escalating")
        threading.Thread(target=_escalate_if_unacked, args=(time.time(), text, True), daemon=True).start()
        return
    INJECT_FILE.write_text(text[:1500])
    _last_inject = time.time()
    log(f"inject: {text[:120]!r}")
    if auto:
        threading.Timer(ESCALATE_AFTER_S, _escalate_if_unacked, [time.time(), text]).start()
    else:
        log("inject: conversation-first item; escalation left to the agent")
        if "(your instruction via WhatsApp)" in text and ESCALATE_TO:
            threading.Timer(DELIVERY_WATCH_S, _check_delivered, [time.time(), text]).start()


def _inject_later():
    delay = max(0.0, INJECT_MIN_GAP_S - (time.time() - _last_inject))
    threading.Timer(delay, lambda: _with_lock(_flush_inject)).start()


def _with_lock(fn):
    with _lock:
        fn()


def _summary_line(item, why):
    ch = item["channel"]
    if ch == "email":
        where = f"email from {item.get('from_name') or item.get('from')}, subject {item.get('subject','')!r}"
    elif ch == "teams":
        where = f"Teams {item.get('chat')} from {item.get('from')}"
    else:
        where = f"WhatsApp {item.get('chat')} from {item.get('from')}"
    return f"{where} ({why}): {item.get('text','')[:160]}"


def _whisper(path, mime, language=None):
    """Transcribe. Whisper labels the user's Urdu as Hindi (Devanagari) or
    Punjabi (Gurmukhi); the user speaks English, Urdu and Punjabi and wants
    Urdu script, never Hindi. So detect first, and re-run forced to Urdu on
    hi/pa."""
    data = path.read_bytes()
    b = "----inboxwhisper"
    lang = f"--{b}\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\n{language}\r\n" if language else ""
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper-1\r\n"
            f"--{b}\r\nContent-Disposition: form-data; name=\"response_format\"\r\n\r\nverbose_json\r\n" + lang +
            f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n").encode() + data + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(WHISPER_URL, data=body, headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode("utf-8", "replace")
    try:
        j = json.loads(raw)
    except Exception:
        return raw.strip()
    text = (j.get("text") or "").strip()
    detected = (j.get("language") or "").lower()
    if language is None and (detected in ("hi", "hindi", "pa", "panjabi", "punjabi")
                             or any("\u0900" <= ch <= "\u097f" or "\u0a00" <= ch <= "\u0a7f" for ch in text)):
        log(f"whisper: detected {detected or 'devanagari'}; re-running as Urdu")
        return _whisper(path, mime, language="ur")
    return text


def _enrich_whatsapp_media(item):
    """Fetch a photo / voice note / document through the daemon's /media route,
    save it under context/media, transcribe voice notes with whisper, and put
    something readable in item["text"] so triage and the agent can use it."""
    kind = MEDIA_TYPES.get(item.get("type"))
    if not kind or not item.get("id") or not item.get("chat_jid"):
        return
    try:
        q = urllib.parse.urlencode({"phone": item["chat_jid"], "id": item["id"]})
        with urllib.request.urlopen(f"{WA_DAEMON}/media?{q}", timeout=60) as r:
            mime = (r.headers.get("Content-Type") or "").split(";")[0].strip()
            data = r.read()
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        ext = _EXT.get(mime, mime.split("/")[-1] or "bin")
        path = MEDIA_DIR / f"{item['id']}.{ext}"
        path.write_bytes(data)
        item["media_path"] = str(path)
        caption = item.get("text", "")
        caption = "" if caption.startswith("[") else caption
        if kind == "voice note":
            try:
                text = _whisper(path, mime or "audio/ogg")
                item["text"] = f"[voice note] {text}" if text else f"[voice note saved: {path}]"
            except Exception as e:
                item["text"] = f"[voice note saved: {path}] (transcription failed: {e})"
        else:
            item["text"] = f"[{kind} saved: {path}]" + (f" {caption}" if caption else "")
        log(f"whatsapp media: {kind} {len(data)} bytes -> {path.name}")
    except Exception as e:
        _state["errors"] += 1
        log(f"whatsapp media: fetch failed for {item.get('id')}: {e}")


READ_GRACE_S = float(os.environ.get("INBOX_READ_GRACE_S", 75))


def _user_has_read(item):
    """Has the user already read this message themselves? Teams: the chat
    viewpoint's last-read time is at or after the message. WhatsApp: the bridge
    reports the chat's unread count as zero. The rule: a message the user has
    read is not acted on or relayed."""
    try:
        if item.get("channel") == "teams" and item.get("chat_id"):
            c = graph("GET", f"chats/{item['chat_id']}?$select=id,viewpoint")
            last = ((c or {}).get("viewpoint") or {}).get("lastMessageReadDateTime") or ""
            return bool(last) and last >= (item.get("ts") or "")
        if item.get("channel") == "whatsapp":
            jid = item.get("chat_jid") or ""
            with urllib.request.urlopen(WA_DAEMON + "/chats?limit=400", timeout=10) as r:
                d = json.loads(r.read().decode())
            chats = d.get("chats", d) if isinstance(d, dict) else d
            for c in chats:
                if c.get("id") == jid:
                    return int(c.get("unreadCount") or 0) == 0
    except Exception as e:
        log(f"read-check failed ({e}); treating as unread")
    return False


def _queue_after_read_check(item, why, auto):
    global _inject_buf
    time.sleep(READ_GRACE_S)
    if _user_has_read(item):
        log(f"{item['channel']}: read by the user within {READ_GRACE_S:.0f}s; not injected ({why})")
        return
    with _lock:
        _inject_buf.append((_summary_line(item, why), auto))
        _inject_later()


def record(item):
    if item.get("channel") == "whatsapp" and not item.get("from_me"):
        _enrich_whatsapp_media(item)
    key = (item.get("channel"), item.get("id"))
    with _lock:
        if item.get("id") and key in _seen_ids:
            return
        _seen_ids.append(key)
        del _seen_ids[:-500]
    important, why = is_important(item)
    item["important"] = important
    item["why"] = why
    item["received"] = datetime.now(timezone.utc).isoformat()
    with _lock:
        with open(INBOX, "a") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
        _state["events"] += 1
        _state["last_event"] = item["received"]
        if important:
            # Mentions and direct messages on Teams/WhatsApp: the agent replies as
            # the user and asks what is needed first. Mail alerts and the
            # unanswered-request sweep still escalate automatically.
            conversational = item["channel"] in ("teams", "whatsapp") and why in (
                "direct message", "you were mentioned", "your instruction via WhatsApp",
                "reply in a thread you posted in", "follow-up to their tag of you")
            if item["channel"] in ("teams", "whatsapp") and why != "your instruction via WhatsApp":
                # Give the user a moment: if they read it themselves first, it is theirs.
                threading.Thread(target=_queue_after_read_check, args=(item, why, not conversational), daemon=True).start()
            else:
                _inject_buf.append((_summary_line(item, why), not conversational))
                _inject_later()
    render_today()
    log(f"{item['channel']}: {'IMPORTANT ' if important else ''}{item.get('from')} -> {item.get('text','')[:80]!r} [{why}]")


def render_today():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=36)
    items = []
    try:
        for line in INBOX.read_text().splitlines()[-2000:]:
            try:
                it = json.loads(line)
            except Exception:
                continue
            try:
                if datetime.fromisoformat(it["received"]) >= cutoff:
                    items.append(it)
            except Exception:
                pass
    except OSError:
        pass
    out = [f"# Inbox digest (last 36h), generated {datetime.now():%Y-%m-%d %H:%M}", ""]
    imp = [i for i in items if i.get("important")]
    out.append(f"## Important ({len(imp)})")
    for i in reversed(imp):
        out.append(f"- {i['received'][11:16]}Z {_summary_line(i, i.get('why',''))}")
    for ch in ("email", "teams", "whatsapp"):
        rows = [i for i in items if i["channel"] == ch and not i.get("important")]
        out.append(""); out.append(f"## {ch} (other, {len(rows)})")
        for i in reversed(rows[-60:]):
            who = i.get("from_name") or i.get("from")
            where = i.get("subject") or i.get("chat") or ""
            out.append(f"- {i['received'][11:16]}Z {who} / {where}: {i.get('text','')[:120]}")
    with _lock:
        TODAY.write_text("\n".join(out) + "\n")


# ---- HTTP ------------------------------------------------------------------
class H(BaseHTTPRequestHandler):
    def _send(self, code, body=b"", ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/graph" and "validationToken" in q:
            return self._send(200, q["validationToken"][0].encode())
        if u.path == "/health":
            body = dict(_state, uptime=int(time.time() - _state["started"]),
                        public_url=public_url(), claude_running=_claude_running())
            return self._send(200, json.dumps(body).encode(), "application/json")
        return self._send(404, b"not found")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b""
        if u.path == "/graph":
            if "validationToken" in q:
                return self._send(200, q["validationToken"][0].encode())
            self._send(202)
            threading.Thread(target=self._graph, args=(raw,), daemon=True).start()
            return
        if u.path == "/whatsapp":
            sig = self.headers.get("X-WA-Signature", "")
            want = "sha256=" + hmac.new(secret().encode(), raw, hashlib.sha256).hexdigest()
            # /whatsapp is reachable through the tunnel; an unsigned post could
            # inject a fake "[inbox] WhatsApp ..." line into the live session.
            if not sig or not hmac.compare_digest(sig, want):
                log("whatsapp: missing/bad signature, dropped")
                return self._send(401)
            self._send(200)
            threading.Thread(target=self._whatsapp, args=(raw,), daemon=True).start()
            return
        return self._send(404)

    def _graph(self, raw):
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            return log("graph: bad JSON")
        for n in payload.get("value", []):
            if n.get("clientState") != secret():
                log("graph: clientState mismatch, dropped"); continue
            if n.get("lifecycleEvent"):
                ev = n["lifecycleEvent"]
                if ev == "reauthorizationRequired":
                    renew_one(n.get("subscriptionId"))
                else:
                    log(f"graph: lifecycle {ev} for {n.get('subscriptionId')}; pass requested")
                    if ev == "subscriptionRemoved":
                        subs = _subs_load()
                        for k in [k for k, v in subs.items() if v.get("id") == n.get("subscriptionId")]:
                            del subs[k]
                        _subs_save(subs)
                    _pass_requested[0] = True
                continue
            res = n.get("resource", "")
            try:
                # Chat notifications arrive as chats('<id>')/messages('<id>'), mail as
                # Users/<id>/Messages/<id>. The old test looked for "chats/" only, so
                # every Teams event was fetched as mail and rejected (toRecipients is
                # not a chatMessage property): chat push was silently dropped.
                low = res.lower()
                if low.startswith("chats(") or low.startswith("chats/") or "/chats/" in low:
                    record(fetch_chat(res))
                elif "/messages" in low:
                    record(fetch_mail(res))
                else:
                    log(f"graph: unhandled resource {res}")
            except Exception as e:
                _state["errors"] += 1
                log(f"graph: fetch {res} failed: {e}")

    def _whatsapp(self, raw):
        try:
            evt = json.loads(raw or b"{}")
        except Exception:
            return log("whatsapp: bad JSON")
        if evt.get("event") == "connection":
            return log(f"whatsapp: connection {evt.get('data')}")
        if evt.get("event") != "message":
            return
        try:
            item = normalise_whatsapp(evt)
            record(item)
        except Exception as e:
            _state["errors"] += 1
            log(f"whatsapp: normalise failed: {e}")


def main():
    _seed_own_posts()
    if "--login" in sys.argv:
        tok = token(interactive=True)
        print("signed in" if tok else "sign-in failed"); return 0 if tok else 1
    if "--status" in sys.argv:
        try:
            print(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=3).read().decode())
        except Exception as e:
            print(json.dumps({"error": str(e)}))
        return 0
    secret()
    log(f"starting on 0.0.0.0:{PORT}; public_url={public_url() or '(unset)'}")
    try:
        token()
    except Exception as e:
        log(f"token check: {e}")
    threading.Thread(target=renewal_loop, daemon=True).start()
    threading.Thread(target=_presence_watch, daemon=True).start()
    render_today()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
