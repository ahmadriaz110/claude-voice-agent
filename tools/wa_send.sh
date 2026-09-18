#!/bin/bash
# Send on WhatsApp through the local bridge AND record what was sent as
# agent-sent, so the digest and outgoings.py can tell the agent's own
# messages apart from yours (both go out from the same account; the bridge
# returns no message id, so the exact text is the key).
#
# usage: wa_send.sh '<json payload for POST /send>'
#
#   text:        {"phone":"4915551234567","message":"On my way"}
#   document:    {"phone":"<digits or jid>","document":"/abs/path/report.pdf","mimetype":"application/pdf","message":"caption"}
#   image:       {"phone":"...","image":"/abs/path/photo.jpg","message":"caption"}
#   voice note:  {"phone":"...","audio":"/abs/path/note.ogg","mimetype":"audio/ogg; codecs=opus","ptt":true}
#
# phone accepts <digits>, <lid>@lid or <id>@g.us. Paths are absolute paths on
# this machine (the bridge reads the file itself). A voice note is an
# Ogg/Opus file with ptt true; make one from any audio with
#   ffmpeg -y -i in.mp3 -c:a libopus -b:a 32k -ar 48000 -ac 1 out.ogg
# The bridge answers {"ok":true,...,"media":"audio","voiceNote":true}.
payload="$1"
WA_URL="${WA_DAEMON_URL:-http://127.0.0.1:47823}"
# Guard: a throwaway "TEST" line once went to a client from a compound command.
# Test-looking messages go only to your own second number (INBOX_ESCALATE_TO);
# to anyone else they are refused here.
if printf '%s' "$payload" | python3 -c 'import sys, json, os
try:
    j = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)
m = (j.get("message") or "").strip(); ph = (j.get("phone") or "").replace(" ", "").lstrip("+")
own = os.environ.get("INBOX_ESCALATE_TO", "").replace(" ", "").lstrip("+")
sys.exit(1 if (m.upper().startswith("TEST") and (not own or ph != own)) else 0)'; then :; else
  echo '{"ok": false, "error": "refused: test-looking message to a number that is not your own"}'; exit 1
fi
resp=$(curl -s -X POST "$WA_URL/send" -H 'Content-Type: application/json' -d "$payload")
echo "$resp"
python3 - "$payload" "$resp" <<'PY'
import sys, json, datetime, pathlib
try:
    sent = json.loads(sys.argv[1]); resp = json.loads(sys.argv[2])
except Exception:
    raise SystemExit
if not resp.get("ok"):
    raise SystemExit
text = sent.get("message") or ""
p = pathlib.Path.home()/".voicemode"/"agent_sent_ids.jsonl"
with open(p, "a") as f:
    f.write(json.dumps({"id": resp.get("id"), "chat": resp.get("to",""),
                        "text": text[:400],
                        "media": bool(sent.get("document") or sent.get("audio") or sent.get("image")),
                        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat()})+"\n")
PY
