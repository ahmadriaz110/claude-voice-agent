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
INJECT_FILE = HOME / ".voicemode" / "indicator" / "inject.txt"
# Escalation: if an important item is pushed into the session and he has not
# responded within ESCALATE_AFTER_S, the same line is sent to his other
# WhatsApp number through the local Baileys daemon (POST /send). "Responded"
# = ACK_FILE touched after the push: the barge-in daemon touches it whenever
# it verifies his voice, and any reply from that number touches it too.
ACK_FILE = CTX / "ack"
ESCALATE_TO = os.environ.get("INBOX_ESCALATE_TO", "")            # digits only, e.g. 4915551234567
ESCALATE_AFTER_S = float(os.environ.get("INBOX_ESCALATE_AFTER", 20))
ESCALATE_MIN_GAP_S = 120
WA_DAEMON = os.environ.get("WA_DAEMON_URL", "http://127.0.0.1:47823")
MEDIA_DIR = CTX / "media"
WHISPER_URL = os.environ.get("INBOX_WHISPER_URL", "http://127.0.0.1:2022/v1/audio/transcriptions")
MEDIA_TYPES = {"imageMessage": "image", "audioMessage": "voice note", "videoMessage": "video",
               "documentMessage": "document", "stickerMessage": "sticker"}
_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "audio/ogg": "ogg",
        "audio/mpeg": "mp3", "audio/mp4": "m4a", "video/mp4": "mp4", "application/pdf": "pdf"}
_last_escalation = [0.0]
# Unanswered client requests. New quote/clarification requests from HCL or
# any client must not sit in the Inbox: if no reply from your own domain within
# QUOTE_SLA_H hours, flag it as important (so it is spoken and escalated).
QUOTE_SLA_H = float(os.environ.get("INBOX_QUOTE_SLA_H", 2))
QUOTE_WORDS = ("quote", "quotation", "rfq", "request", "pricing", "price", "availability",
               "engineer", "support", "site visit", "schedule", "confirm", "urgent", "ticket",
               "resource", "onsite", "on-site", "estimate", "proposal")
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
ME_EMAIL = os.environ.get("INBOX_ME_EMAIL", "")          # your mailbox address
ME_NAMES = ("ahmad", "riaz")
OWN_DOMAIN = os.environ.get("INBOX_OWN_DOMAIN", ME_EMAIL.split("@")[-1])
# Microsoft Graph Command Line Tools: a first-party public client that
# supports device-code sign-in with delegated Graph scopes, so no app
# registration is needed in the tenant.
CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
TENANT = os.environ.get("INBOX_TENANT", "common")
AUTHORITY = f"https://login.microsoftonline.com/{TENANT}"
SCOPES = ["Mail.Read", "Chat.Read", "ChatMessage.Read", "User.Read"]
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
# Mail folders to watch. Rules move client mail (e.g. HCL) out of the Inbox,
# so a subscription on the Inbox alone misses it. Names are resolved to ids
# at the top level and one level under the Inbox.
MAIL_FOLDERS = [f.strip() for f in os.environ.get("INBOX_MAIL_FOLDERS", "Inbox,HCL").split(",") if f.strip()]
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


def _is_request(m, folder):
    frm = ((m.get("from") or {}).get("emailAddress") or {}).get("address", "").lower()
    if not frm or frm.endswith("@" + OWN_DOMAIN) or frm.startswith(("no-reply@", "noreply@", "mailer-daemon")):
        return False
    blob = ((m.get("subject") or "") + " " + (m.get("bodyPreview") or "")).lower()
    if "automatic reply" in blob or "out of office" in blob:
        return False
    return folder == "HCL" or any(w in blob for w in QUOTE_WORDS)


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
            # the request (Mahesh / TRM2026TN099, 2026-09-06).
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


def _is_me(name_or_email):
    v = (name_or_email or "").lower()
    return ME_EMAIL in v or all(n in v for n in ME_NAMES)


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
    return {"channel": "teams", "id": m.get("id"), "ts": m.get("createdDateTime"),
            "from": frm.get("displayName", ""), "from_name": frm.get("displayName", ""),
            "chat": info["topic"], "chat_type": info["type"], "chat_id": chat_id,
            "text": _strip_html((m.get("body") or {}).get("content", ""))[:400],
            "mentions": mentions}


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
    other = (jid.startswith(ESCALATE_TO) or (not from_me and not d.get("isGroup")
             and author.strip().lower() in ("ahmad riaz", "ahmad", "ahmad riaz (other)")))
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
            return False, "own"
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
        if _is_me(item.get("from", "")):
            return False, "own"
        if item.get("chat_type") == "oneOnOne":
            return True, "direct message"
        if any(_is_me(m) for m in item.get("mentions", [])) or "@ahmad" in text:
            return True, "you were mentioned"
        return False, "group chatter"
    if ch == "whatsapp":
        if item.get("from_other_phone") and not item.get("from_me"):
            item["from"] = "the user (other phone)"
            try:
                ACK_FILE.touch()
            except OSError:
                pass
            return True, "your instruction via WhatsApp"
        if item.get("from_me"):
            return False, "own"
        if not item.get("group"):
            return True, "direct message"
        if any(m and m in text for m in item.get("mentions", [])) or "ahmad" in text:
            return True, "you were mentioned"
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
        return r.read().decode()[:120]


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


def _flush_inject():
    global _last_inject, _inject_buf
    if not _inject_buf:
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
    """Transcribe. Whisper labels his Urdu as Hindi (Devanagari) or Punjabi
    (Gurmukhi); he speaks English, Urdu and Punjabi and wants Urdu script,
    never Hindi. So detect first, and re-run forced to Urdu on hi/pa."""
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
            # the user and asks what is needed first (his rule, 2026-09-06). Mail
            # alerts and the unanswered-request sweep still escalate automatically.
            conversational = item["channel"] in ("teams", "whatsapp") and why in (
                "direct message", "you were mentioned", "your instruction via WhatsApp")
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
            if item.get("from_me"):
                return
            record(item)
        except Exception as e:
            _state["errors"] += 1
            log(f"whatsapp: normalise failed: {e}")


def main():
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
    render_today()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
