# claude-voice-agent

**"Hey Claude"**: a hands-free voice agent for Claude Code that watches your mail, Teams and WhatsApp and speaks up.

A hands-free, always-listening voice agent for **Claude Code on the Claude desktop app (macOS)**, built from small local daemons rather than a new app:

- **Wake word + speaker verification.** "Hey Claude, do X" is heard only from the enrolled voice (resemblyzer voiceprint), transcribed locally (whisper), and typed into the *running* Claude session, so context is never lost.
- **Barge-in.** Interrupt Claude mid-sentence; if Claude was not listening, the daemon records what you said and injects it anyway.
- **Commands while Claude works.** Injected messages land mid-turn; the daemon shortens Claude's current speech or listen window so the message is read within seconds.
- **Awareness.** A push receiver for Microsoft Graph change notifications (Outlook folders, Teams chats) and a WhatsApp webhook (Baileys bridge). Everything is logged to a digest; important items are pushed into the live session; unanswered ones are escalated to a second WhatsApp number, whose text or voice-note replies come back as instructions.
- **Driving trained chats.** Open a named session in the app's *Chat and Cowork* tab by its sidebar title, attach a file, type an instruction, return. Chat replies cannot be read by tools, so instructions ask the chat to save results into a watched folder.
- **Task ledger**, backfill scripts for 60 days of mail/Teams/WhatsApp history, and a self-healing check for the voicemode patches.

Everything runs on the Mac. Speech-to-text and text-to-speech are OpenAI-compatible local endpoints (whisper.cpp / Kokoro via [voice-mode](https://github.com/mbailey/voicemode)); a LAN GPU box can serve whisper `large-v3`.

## Architecture

```
 mic ──► bargein_daemon.py ──► (wake / barge-in / capture) ──► pastes into Claude composer (AX API)
           │  speaker verify (resemblyzer, :8899)                ▲
           │  cut TTS / end listen (voicemode control socket)    │ inject.txt
 Claude ◄──┘                                                      │
   ▲  converse (voice-mode MCP, patched)                          │
   │                                                              │
 inbox_hooks.py (:8898, behind a Cloudflare/Tailscale hostname) ──┘
   ├── Graph change notifications: mail folders + per-chat Teams subscriptions (auto-renew)
   ├── WhatsApp webhook (Baileys daemon on 127.0.0.1:47823, HMAC-signed), media + whisper for voice notes
   ├── inbox.jsonl / today.md digest, escalation to a second WhatsApp number, ack file
   └── sweep: client requests unanswered by your domain for N hours
```

## Components

| Path | What it is |
|---|---|
| `daemon/bargein_daemon.py` | Wake word, barge-in, post-interrupt capture, speaker-verify server, native AX paste into the Claude composer, `@@session=` named-session commands, `@@axtitles` label dump |
| `daemon/launcher.c` | Tiny fork-not-exec launcher so the daemon keeps its own macOS TCC (Accessibility / Microphone) identity |
| `daemon/voicemode_indicator.py` | rumps menu-bar indicator (listening / thinking / speaking) |
| `inbox/inbox_hooks.py` | Push receiver: Graph subscriptions, WhatsApp webhook, digest, triage, escalation, media, unanswered-request sweep |
| `inbox/backfill.py`, `inbox/backfill_whatsapp.py` | 60-day history pulls |
| `patches/*.patch` | Changes to voice-mode 8.12.0 (`simple_failover.py`, `tools/converse.py`): connect timeout, prompt-echo and hallucination guards, energy-gated VAD, speaker-gated end-of-turn, speaker filter on the listen window, Urdu-not-Hindi re-transcription |
| `tools/heal_voicemode.py` | Re-applies / verifies the patches after `uv tool upgrade` |
| `tools/tasks.py` | Task ledger (`add / start / done / block / list`) |
| `tools/qr_render.py` | Renders a terminal QR (WhatsApp linking) to an image |
| `launchd/*.plist` | launchd templates (`__HOME__` is substituted by `install.sh`) |

## Requirements

- macOS, the Claude desktop app (Code tab), Python 3.11 (a venv per `install.sh`)
- [voice-mode](https://github.com/mbailey/voicemode) 8.12.0 installed with `uv tool`, with local whisper + Kokoro services
- Python packages: `resemblyzer`, `webrtcvad`, `sounddevice`, `numpy`, `scipy`, `pyobjc-framework-ApplicationServices`, `pyobjc-framework-Quartz`, `pyobjc-framework-Cocoa`, `msal`, `pypdf`, `rumps`
- Accessibility + Microphone grants for the daemon (macOS prompts on first run)
- A public HTTPS hostname that forwards to this Mac (Cloudflare Tunnel public hostname → `http://<mac-lan-ip>:8898`, or Tailscale Funnel) for Graph push
- A Microsoft 365 account; `inbox_hooks.py --login` runs a device-code sign-in with the Microsoft Graph Command Line Tools public client (delegated `Mail.Read Chat.Read ChatMessage.Read`). No app registration needed, but your tenant must allow that client.
- A Baileys WhatsApp bridge exposing `/status /qr /send /chats /messages /media /webhooks` on `127.0.0.1:47823` (the `daemon.js` in `whatsapp-bridge/` notes describe the routes this receiver expects; the bridge itself is not included)

## Setup (short form)

1. `./install.sh`, creates the venv, installs packages, fills `launchd/*.plist` with your home path, copies them to `~/Library/LaunchAgents`.
2. Copy `config.example.env` to `~/.voicemode/indicator/agent.env` and fill in your values (mailbox, own domain, escalation number, hostname, home session title, mic name, STT URLs).
3. Enrol your voice: record ~70 s of natural speech and save the 256-dim resemblyzer embedding as `~/.voicemode/indicator/voiceprint.npy` (see `daemon/bargein_daemon.py::load_speaker_model`).
4. Apply `patches/` to your voice-mode install (`patch -p1 -d <site-packages>`), then run `tools/heal_voicemode.py` to verify.
5. `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.voicemode.bargein.plist` and the same for `com.voicemode.inbox.plist`.
6. `inbox_hooks.py --login`, write your public URL to `~/.voicemode/context/public_url.txt`; subscriptions are created and renewed automatically (`curl 127.0.0.1:8898/health`).
7. Register the WhatsApp webhook on your bridge: `POST /webhooks {url: http://127.0.0.1:8898/whatsapp, secret: <~/.voicemode/context/secret>}`.

In Claude, keep the voice loop alive with the `converse` tool; treat `[voice] ...` and `[inbox] ...` lines as spoken input and live alerts. A short protocol for that is in `docs/PROTOCOL.md`.

## Features, with the commands that drive them

| Say / do | What happens |
|---|---|
| **"Hey Claude, open Amazon and YouTube in two tabs"** while Claude is busy | Wake word verified against your voiceprint, the whole sentence transcribed, pasted into the running session as `[voice] ...`; Claude's current speech or listen window is cut so it reads it within seconds |
| Talk over Claude mid-sentence | Barge-in (speaker-verified). If Claude was not going to listen, the daemon records you and injects the words anyway |
| "Hey Claude" alone | Pastes the resume prompt into the existing session (context kept). A 4 s window accepts a follow-up command as the sentence |
| Give three tasks in a row | Each becomes a background agent; results are spoken as they finish, whichever first |
| New mail in a watched folder, a Teams message in one of the subscribed chats, a WhatsApp message | Arrives via push in seconds, appended to `context/inbox.jsonl`, `today.md` regenerated. Important ones (addressed to you, mentions, direct messages, high importance, client requests) are pushed into the session and spoken |
| You don't answer an important alert for 20 s | The same line is sent to your second WhatsApp number. Reply by text or **voice note**; it comes back as an instruction (voice notes transcribed, Devanagari re-run as Urdu) |
| A client request sits unanswered by your domain for 2 h | Flagged as `UNANSWERED Nh` and escalated like any important item |
| **"Give All CVs this file and tell it to translate it"** | Daemon opens the *Chat and Cowork* tab, clicks the "All CVs" row, pastes the file as an attachment, types the instruction, returns to your Code session; the chat saves its output to a folder you watch |
| "Brief me" / "what came in" | The agent reads `today.md` and speaks the important items first |
| "What's pending?" | `tasks.py list open`, read aloud |

## How we run it (reference setup)

- **Mac** (Apple silicon): Claude desktop app, voice-mode 8.12.0 (Kokoro TTS + whisper `medium` locally as fallback), both daemons under launchd, the WhatsApp bridge, the menu-bar indicator. Audio: a USB headset mic; output through the Mac speakers (the mic hears the TTS, so speaker verification, not loudness, decides what is an interruption).
- **Second PC on the LAN** (Windows, 2× RTX 3090): an OpenAI-compatible whisper server (`large-v3`) on port 9000. voice-mode's `VOICEMODE_STT_BASE_URLS` lists it first and the local server second, so a dead LAN box degrades to `medium`, never to silence. LAN overhead measured ~11 ms against ~1 s of inference. The wake-word and voice-note transcriptions use the same server.
- **No API keys.** Nothing goes to a paid API. Microsoft Graph and WhatsApp are your own accounts; the Graph sign-in is a delegated device-code login cached with refresh tokens on disk (0600).
- **Public hostname**: an existing Cloudflare Tunnel on the other PC with one public hostname pointed at `http://<mac-lan-ip>:8898`. Tailscale Funnel works the same way.
- **Smaller machines**: everything except whisper `large-v3` runs comfortably on the Mac alone (Kokoro is ~80 M parameters, resemblyzer ~10 ms per check on CPU). Use whisper `medium` or `small` locally and drop the LAN box; accuracy on names suffers a little, nothing else changes.
- **What it costs to run**: two Python processes (~300 MB with the speaker model), one Node process for the bridge, and the GPU box only when speech is being transcribed.

## WhatsApp bridge

The receiver expects a small local Baileys bridge on `127.0.0.1:47823` with these routes: `GET /status`, `GET /qr` (also writes a PNG), `POST /send {phone, message}`, `GET /chats`, `GET /messages?phone=&limit=` (each item with `id`, `type`, `from`, `text`, `timestamp`), `GET /media?phone=&id=` (raw bytes + content type), and `POST /webhooks {url, events, secret}` delivering `{event, timestamp, data:{chatJid,isGroup,fromMe,author,text,messageId,timestamp,type}}` signed with `X-WA-Signature: sha256=<hmac>`. `whatsapp-bridge/daemon.js.patch` adds the `/media` route and the `id`/`type` fields to a bridge built on [Baileys](https://github.com/WhiskeySockets/Baileys) with `syncFullHistory: true`. Link the device by scanning the QR from `tools/qr_render.py`'s image or the bridge's PNG; a phone-side "couldn't link" usually means a second copy of the bridge is fighting for the port, or a stale session directory.

## Privacy and safety

- Everything stays local except Graph/WhatsApp traffic to their own services and STT/TTS to endpoints you choose.
- The receiver validates Graph `clientState` and WhatsApp HMAC signatures; unsigned WhatsApp posts are rejected because the endpoint is reachable through your tunnel.
- Do **not** commit: `voiceprint.npy`, `msal_cache.json`, `context/secret`, `context/*.jsonl`, `context/media/`, `history/`, or any memory files. `.gitignore` covers them.
- The daemon types into the Claude composer with your Accessibility grant. A mis-transcription becomes an instruction; keep the `[voice]` prefix so the agent confirms anything destructive.

## Known limits

- Injected messages surface at Claude's next tool boundary; the daemon cuts speech/listen windows to keep that under a few seconds, not instant.
- Speaker verification (resemblyzer GE2E) is decent, not biometric: similar voices, or a TV, can score close to the enrolled voice. A stronger model (ECAPA) is a planned upgrade.
- Sidebar navigation depends on the app's accessibility labels ("Chat and Cowork", row titles); an app update may rename them. `@@axtitles` dumps the current labels.
- WhatsApp calls cannot be placed by any unofficial library. Old WhatsApp media may fail to download (expired on WhatsApp's servers).
- Patches target voice-mode 8.12.0 exactly.

## Credits

[voice-mode](https://github.com/mbailey/voicemode) (MCP voice loop), [resemblyzer](https://github.com/resemble-ai/Resemblyzer), [Baileys](https://github.com/WhiskeySockets/Baileys), whisper.cpp, Kokoro.

## License

MIT.
