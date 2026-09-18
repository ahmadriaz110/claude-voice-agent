# claude-voice-agent

**"Hey Claude"**: a hands-free voice agent for Claude Code that watches your mail, Teams and WhatsApp and speaks up.

A hands-free, always-listening voice agent for **Claude Code on the Claude desktop app (macOS)**, built from small local daemons rather than a new app:

- **Wake word + speaker verification.** "Hey Claude, do X" is heard only from the enrolled voice (resemblyzer voiceprint), transcribed locally (whisper), and typed into the *running* Claude session, so context is never lost.
- **Barge-in.** Interrupt Claude mid-sentence; if Claude was not listening, the daemon records what you said and injects it anyway.
- **Commands while Claude works.** Injected messages land mid-turn; the daemon shortens Claude's current speech or listen window so the message is read within seconds.
- **Awareness.** A push receiver for Microsoft Graph change notifications (Outlook folders, Teams chats) and a WhatsApp webhook (Baileys bridge). Everything is logged to a digest; important items are pushed into the live session; unanswered ones are escalated to a second WhatsApp number, whose text or voice-note replies come back as instructions.
- **Driving trained chats.** Open a named session in the app's *Chat and Cowork* tab by its sidebar title, attach a file, type an instruction, return. Chat replies cannot be read by tools, so instructions ask the chat to save results into a watched folder.
- **Calls.** When a Teams or WhatsApp call starts, the voice agent goes quiet and stays off the mic; a small CoreAudio helper records your side and the remote side, and a transcript lands in the session when the call ends.
- **Knowing what you already did.** A 12-minute check lists your own recent sends (WhatsApp, Teams, mail) so the agent stops re-raising things you handled yourself; a menu-bar "At desk / Away" toggle tells it whether to speak or message your phone.
- **Task ledger**, backfill scripts for 60 days of mail/Teams/WhatsApp history, and a self-healing check for the voicemode patches (deaf mic, mute TTS).

Everything runs on the Mac. Speech-to-text and text-to-speech are OpenAI-compatible local endpoints (whisper.cpp / Kokoro via [voice-mode](https://github.com/mbailey/voicemode)); a LAN GPU box can serve whisper `large-v3`.

## What's new (September 2026)

The first release (6 September) was the voice loop plus the mail, Teams and WhatsApp awareness. Since then the stack has been running all day, every day, and most of what changed came from things that went wrong in use.

- **Outgoings check** (`tools/outgoings.py`). The agent kept re-raising threads I had already answered myself from my phone or from Outlook. The receiver only ever saw inbound traffic, and the bridge cannot tell my sends from the agent's (same account). Now every send the agent makes goes through `tools/wa_send.sh` or `inbox_hooks._wa_send`, which log the text to `agent_sent_ids.jsonl`; every 12 minutes the session runs `outgoings.py`, which reads the WhatsApp desktop app's ChatStorage, the Teams chats and Sent Items over Graph, subtracts the agent's own sends, redacts anything that looks like a credential, and hands the list to the session. The rule in `docs/PROTOCOL.md` is simple: read it before answering anyone.
- **DNS-over-HTTPS fallback** (`daemon/dns_fallback.py`). Twice in two days the Mac's resolvers went silent on UDP 53 while HTTPS itself was fine, so every Graph and MSAL call died on name resolution and the relay went blind without any error that looked like a network fault. The module monkeypatches `socket.getaddrinfo`: normal resolution first, and only on failure a DoH query to a resolver reached by IP, cached five minutes, DoH-first for two minutes after a failure because the system resolver takes about 30 s to give up each time. One import line at the top of the receiver, the call watch and `outgoings.py`.
- **Call watcher** (`callwatch/`). When a Teams or WhatsApp call comes in the voice agent has to go quiet at once, stay off the microphone, and still know what was said. `call_watch.py` polls CoreAudio every 2 s for the process objects of the two apps and reads whether they are running input and output; both together for 20 s is a call (a voice note being recorded also opens both for a few seconds, which is why it is 20 and not 3). It writes `call_active.json`, which the barge-in daemon checks once a second and treats as "do not touch the mic"; it injects `[call] started`; and it starts `audiotap`, a small Swift binary that records the mic through an IOProc and the remote side through a CoreAudio process tap wrapped in a private aggregate device (macOS 14.2+, no BlackHole, no loopback device), 16 kHz mono WAV in 10-minute chunks, supervised and restarted at the next chunk number if it dies. When the app releases the mic for 8 s the call ends, `[call] ended` is injected, and the chunks go to whisper in 90 s pieces with retries (one 10-minute upload used to hold the STT server for minutes and took it down when the client gave up). `transcript.txt` is one line per segment tagged MIC or REMOTE; `[call] transcript ready` tells the agent where it is. The daemon no longer cuts speech for `[inbox]` or `[call]` lines, because those are background reads, not you talking.
- **Group watcher** (`tools/watch_group.py`). For a live job with its own WhatsApp group, a monitor runs this script and each new message arrives in the session as one event line, without subscribing the whole inbox to the group.
- **HTML mail helper** (`tools/send_mail.py`, `tools/mail_html.py`). A mail once went out with a hand-typed compact signature. `build_body` now builds every body from the stored signature template and refuses to build one when the template is missing or fails a marker check; `mail_html.build` is the lenient variant with an env-driven fallback block. The template itself is private and lives outside the repo.
- **Receiver triage.** Courtesy closings ("Thank you!", "Noted, will update the customer") are no longer flagged as unanswered requests: the receiver strips the quoted thread, greeting and signature and looks at the sender's own words. Supplier offers (a profile, a rate card) carry no reply SLA. A request answered on a sibling thread counts as answered (Sent Items are checked for anything sent to that sender within the SLA window). Teams mentions match the Graph user id, not a bare first name. A chat item is held 75 s and dropped if you read it yourself. Replies in a group you posted in, and follow-ups after someone tagged you, are pushed like mentions. Status broadcasts and protocol placeholders are ignored.
- **Presence and quiet mode.** The menu bar has an At desk / Away toggle; the receiver injects a `[presence]` line when it flips. `~/.voicemode/quiet.json` makes the receiver log everything and inject nothing, for a digest read on demand.
- **Daemon hardening.** Injected text is persisted to `inject_pending.jsonl` before anything is done with it and replayed after a restart (an instruction was lost once when the daemon restarted three seconds after reading it). A collapsed sidebar is opened before a session row is looked for. `@@axcopy` reads a chat's last reply back through its Copy button. The two-minute health check also restarts a Kokoro that answers 200 with an empty body.
- **Bridge patch.** `whatsapp-bridge/daemon.js.send-media.patch` now also sends voice notes (`audio` path, Ogg/Opus with `ptt`) and text with `mentions`.
- **Outgoings watch instead of a cron** (`tools/outgoings_watch.py`, 18 September). The 12-minute cron was too slow: the agent would answer a thread I had replied to eight minutes earlier. The watch runs as a long-lived monitor from the session, calls `outgoings.py` over a four-minute window every 60 s while the presence toggle says At desk, and prints each new send once, so it lands in the session as an event within a minute. Media sends are matched by timestamp now (`agent_sent_ids.jsonl` carries a `media` flag): a voice note or a PDF the agent sent logged an empty text, which put an empty string in the "agent sent" set and hid every voice note I sent myself.
- **Graph scopes for shared mailboxes and real mentions.** The receiver's token now covers a shared mailbox (a projects@ style inbox), the calendar, Teams channels and `ChatMessage.Send`, because a Teams post with a real @mention only works through Graph with a `mentions` array (the desktop connector sends "@Name" as plain text). Each new scope is one device-code sign-in by me.
- **Call transcripts without hallucinations.** Whisper turned a 21-minute call, on the side that mostly listened, into one sentence repeated 280 times. Pieces with under `CALLWATCH_MIN_SPEECH_S` seconds of detected speech (ffmpeg `silencedetect`) are no longer uploaded, and a segment that repeats one of the previous three, or a phrase that dominates a whole piece, is dropped before the transcript is written.
- **Ledger with priority tiers.** `tools/tasks.py prio ID high|medium|low`, and every save also renders `Pending.md`, the open items grouped by tier, newest first, for me to read live.
- **Send guard.** `tools/wa_send.sh` refuses a message that starts with "TEST" to any number other than your own second number; a placeholder line once reached a client from a compound command.
- **Sweep.** Meeting responses and cancellations ("Canceled:", "Accepted:", "Declined:") are calendar traffic, not unanswered requests.

### What this setup does day to day

The Mac sits on the desk with the headset on. Mail, Teams and WhatsApp arrive through the receiver; what matters is spoken, what does not goes into the digest. I answer some things myself from the phone or from Outlook, and the outgoings check keeps the agent from chasing those. When a call comes in, the agent stops talking, the watcher records both sides, and ten minutes after the call there is a transcript in the session to summarise or act on. Away from the desk, the menu-bar toggle sends replies to my other phone instead, and the same phone can send instructions back, as text or as a voice note. A cheap triage subagent reads the digest every so often with `digest_brief.py` and reports only what needs a decision. The rest is the voice loop from the first release: wake word, barge-in, background agents, a task ledger.

## What it feels like, honestly

Close to ChatGPT's voice mode, with Claude Code doing the work: you talk, it talks back, you can cut it off mid-sentence, and you can hand it things while it works. The difference is latency. This is turn-based, not a streaming duplex model: after you stop speaking it takes one to three seconds before Claude starts answering, and a command injected while it is busy surfaces a few seconds later, at its next step. Long thinking is the biggest wait, not speech. In exchange you get the full Claude Code toolset behind the voice, everything running locally, and no per-minute bill.

## Architecture

```
 mic ──► bargein_daemon.py ──► (wake / barge-in / capture) ──► pastes into Claude composer (AX API)
           │  speaker verify (resemblyzer, :8899)                ▲
           │  cut TTS / end listen (voicemode control socket)    │ inject.txt
 Claude ◄──┘                                                      │
   ▲  converse (voice-mode MCP, patched)                          │
   │                                                              │
 inbox_hooks.py (:8898, behind a Cloudflare/Tailscale hostname) ──┤
   ├── Graph change notifications: mail folders + per-chat Teams subscriptions (auto-renew)
   ├── WhatsApp webhook (Baileys daemon on 127.0.0.1:47823, HMAC-signed), media + whisper for voice notes
   ├── inbox.jsonl / today.md digest, escalation to a second WhatsApp number, ack file
   ├── sweep: client requests unanswered by your domain for N hours
   └── presence.json watch ("[presence]" lines), quiet.json (log only, inject nothing)
                                                                    │
 call_watch.py (launchd) ── audiotap (CoreAudio process tap + mic) ─┘  "[call] started / ended / transcript ready"
   └── call_active.json while a call is on: the daemon stays off the mic

 outgoings.py (every 12 min): your own sends across WhatsApp / Teams / Sent Items, handed to the session
 dns_fallback.py: DNS-over-HTTPS when the system resolver dies (imported by the receiver, the call watch, outgoings)
```

## Components

| Path | What it is |
|---|---|
| `daemon/bargein_daemon.py` | Wake word, barge-in, post-interrupt capture, ECAPA speaker verification (with a resemblyzer fallback) and the verify server voice-mode calls, native AX paste into the Claude composer, `@@session=` named-session commands, `@@axtitles` label dump |
| `daemon/launcher.c` | Tiny fork-not-exec launcher so the daemon keeps its own macOS TCC (Accessibility / Microphone) identity |
| `daemon/voicemode_indicator.py` | rumps menu-bar indicator (listening / thinking / speaking), the At desk / Away presence toggle (`~/.voicemode/presence.json`) |
| `daemon/dns_fallback.py` | `import dns_fallback` at the top of a daemon wraps `socket.getaddrinfo`: normal resolution first, DNS-over-HTTPS by IP when the system resolver fails, DoH-first for two minutes after a failure, five-minute cache |
| `inbox/inbox_hooks.py` | Push receiver: Graph subscriptions, WhatsApp webhook, digest, triage, escalation, media, unanswered-request sweep (courtesy-reply and vendor-offer filters, sibling-thread answers via Sent Items), read-check before injecting, presence watch, quiet mode |
| `inbox/backfill.py`, `inbox/backfill_whatsapp.py` | 60-day history pulls |
| `callwatch/call_watch.py` | Detects a live Teams / WhatsApp call over CoreAudio, records both sides through `audiotap`, writes `call_active.json` so the daemon stays off the mic, transcribes afterwards (90 s pieces, retries), injects `[call] started / ended / transcript ready` |
| `callwatch/audiotap/` | Swift helper (`main.swift`, `build.sh`, `embedded-Info.plist`): CoreAudio process tap on the app's process objects plus a mic IOProc, 16 kHz mono WAV chunks, JSON progress on stdout; re-spawns as its own TCC identity so the Microphone and System Audio Recording prompts are attributed to it |
| `tools/outgoings.py` | Your own sends in the last N minutes across WhatsApp (the desktop app's ChatStorage), Teams and Sent Items (Graph), minus what the agent sent; credential-looking short messages are redacted. Run on a 12-minute cron |
| `tools/watch_group.py` | Follows one WhatsApp group from ChatStorage and prints each new message as an event line, for a monitor that feeds a live job's chat into the session |
| `tools/send_mail.py`, `tools/mail_html.py` | HTML mail bodies with your stored signature template (`~/.voicemode/templates/signature.html`, private): `send_mail.build_body` refuses when the template is missing or fails the marker check, `mail_html.build` falls back to a plain block from env |
| `tools/wa_send.sh` | Bridge send helper: text, document, image, voice note (`audio` + `ptt`); logs what the agent sent to `agent_sent_ids.jsonl` so the digest and `outgoings.py` can tell the agent's messages from yours |
| `tools/presence.py`, `tools/digest_brief.py`, `tools/digest.sh` | Shell-readable presence flag; compact digest views for a triage subagent (your sends marked `YOU (sent)`, the agent's `AGENT (sent)`) |
| `patches/*.patch` | Changes to voice-mode 8.12.0 (`simple_failover.py`, `tools/converse.py`): connect timeout, prompt-echo, URL and hallucination guards, energy-gated VAD with a noise floor taken from non-speech frames only, speaker-gated end-of-turn, optional speaker filter on the listen window, Urdu-not-Hindi re-transcription |
| `tools/heal_voicemode.py` | Re-applies / verifies the patches after `uv tool upgrade`, strips the 25-request limit from the Kokoro launchd plist (see Known limits), restarts a deaf barge-in daemon (`--mic`) and a mute Kokoro (`--tts`) |
| `launchd/com.voicemode.micwatch.plist` | Runs `heal_voicemode.py --mic` every two minutes: if the barge-in daemon has been failing to open its microphone (PortAudio -9986 after a USB or CoreAudio blip, the wake word goes quiet while injection still works) it restarts the daemon and writes one line to `heal.log`. The same run posts a one-line TTS request to Kokoro; a 200 with an empty body means Kokoro is wedged mute (launchd sees it as healthy) and it is kickstarted, at most once per five minutes |
| `launchd/com.voicemode.callwatch.plist` | Keeps `call_watch.py` running (KeepAlive, RunAtLoad); ffmpeg's Homebrew path is added because launchd agents get a bare PATH |
| `tools/enrol/` | `enrol_record.py` (three minutes of you reading `enrolment_text.txt`), `enrol_embed.py` (builds the ECAPA and resemblyzer prints), `calibrate.py` (scores your recording, your own TTS and rejected room clips, suggests thresholds) |
| `tools/tasks.py` | Task ledger (`add / start / done / block / list`) |
| `tools/qr_render.py` | Renders a terminal QR (WhatsApp linking) to an image |
| `launchd/*.plist` | launchd templates (`__HOME__` is substituted by `install.sh`) |

## Requirements

- macOS, the Claude desktop app (Code tab), Python 3.11 (a venv per `install.sh`)
- [voice-mode](https://github.com/mbailey/voicemode) 8.12.0 installed with `uv tool`, with local whisper + Kokoro services
- Python packages: `speechbrain`, `torch`, `torchaudio` (ECAPA-TDNN, downloads ~80 MB of weights from Hugging Face on first run into `~/.voicemode/indicator/ecapa/`), `resemblyzer` (fallback), `webrtcvad`, `sounddevice`, `numpy`, `scipy`, `pyobjc-framework-ApplicationServices`, `pyobjc-framework-Quartz`, `pyobjc-framework-Cocoa`, `msal`, `pypdf`, `rumps`, `requests`
- Accessibility + Microphone grants for the daemon (macOS prompts on first run)
- For the call watch: macOS 14.2 or later (CoreAudio process taps), the Xcode command line tools (`swiftc`) to build `audiotap`, `ffmpeg` from Homebrew, and the WhatsApp desktop app if you want `outgoings.py` and `watch_group.py` to read its ChatStorage
- A public HTTPS hostname that forwards to this Mac (Cloudflare Tunnel public hostname → `http://<mac-lan-ip>:8898`, or Tailscale Funnel) for Graph push
- A Microsoft 365 account; `inbox_hooks.py --login` runs a device-code sign-in with the Microsoft Graph Command Line Tools public client (delegated `Mail.Read Chat.Read ChatMessage.Read`). No app registration needed, but your tenant must allow that client.
- A Baileys WhatsApp bridge exposing `/status /qr /send /chats /messages /media /webhooks` on `127.0.0.1:47823` (the `daemon.js` in `whatsapp-bridge/` notes describe the routes this receiver expects; the bridge itself is not included)

## Setup (short form)

1. `./install.sh`, creates the venv, installs packages, fills `launchd/*.plist` with your home path, copies them to `~/Library/LaunchAgents`, and builds `audiotap` when `swiftc` is present.
2. Copy `config.example.env` to `~/.voicemode/indicator/agent.env` and fill in your values (mailbox, your name parts, own domain, client and vendor domains, escalation number, hostname, home session title, mic name, STT URLs, time zone).
3. Enrol your voice: `tools/enrol/enrol_record.py 180` while you read `tools/enrol/enrolment_text.txt` aloud (the daemon stays off the mic during it), then `tools/enrol/enrol_embed.py` writes `voiceprint_ecapa.npy` (and a resemblyzer print as fallback). Run `tools/enrol/calibrate.py` once to see how your voice, your TTS voice and room noise score, and set the thresholds in `agent.env` from that.
4. Apply `patches/` to your voice-mode install (`patch -p1 -d <site-packages>`), then run `tools/heal_voicemode.py` to verify.
5. `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.voicemode.bargein.plist` and the same for `com.voicemode.inbox.plist` and `com.voicemode.micwatch.plist`.
6. `inbox_hooks.py --login`, write your public URL to `~/.voicemode/context/public_url.txt`; subscriptions are created and renewed automatically (`curl 127.0.0.1:8898/health`).
7. Register the WhatsApp webhook on your bridge: `POST /webhooks {url: http://127.0.0.1:8898/whatsapp, secret: <~/.voicemode/context/secret>}`.
8. Call watch: `python callwatch/call_watch.py --test-record 10` once from a terminal to trigger the two macOS prompts for `audiotap` (Microphone, and System Audio Recording Only under Privacy & Security > Screen & System Audio Recording), check the WAVs it reports, then `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.voicemode.callwatch.plist`. `--dry-run` prints what CoreAudio sees.
9. Mail signature: save your signature block's HTML to `~/.voicemode/templates/signature.html` and set `MAIL_SIGNATURE_MARKER` to a string that must appear in it (your surname in capitals). `send_mail.build_body` refuses to build a mail when the file is missing or the marker is absent.
10. Outgoings: have the session run `tools/outgoings.py --min 15` every 12 minutes (a `/loop 12m` in Claude Code is how the reference setup does it) and before it answers anyone; an external cron can do the same and write the output into `inject.txt` prefixed `[inbox] outgoings:`.

In Claude, keep the voice loop alive with the `converse` tool; treat `[voice] ...` and `[inbox] ...` lines as spoken input and live alerts, `[call] ...` lines as call notices and `[presence] ...` lines as a channel switch. A short protocol for that is in `docs/PROTOCOL.md`.

## Features, with the commands that drive them

| Say / do | What happens |
|---|---|
| Any message delivered to the session (`[voice]`, `[inbox]`) | First the daemon checks which session is showing (the header button is described "<title>, rename session") and, if it is not the one named by `BARGEIN_HOME_SESSION`, selects that one in the sidebar over accessibility, so a message never lands in whatever unrelated session you left in front. Then it delivers |
| Any message delivered to the session (`[voice]`, `[inbox]`) | Lands in the background: the composer takes the text over accessibility and a Return is posted to Claude's process, so whatever you are working in stays in front and your clipboard is untouched. A covered window is normally throttled by Chromium and ignores the write, so start Claude with `--disable-backgrounding-occluded-windows --disable-renderer-backgrounding` (`tools/claude_with_flags.sh`, `launchd/com.voicemode.claude-flags.plist` does it at login; `tools/relaunch_claude.sh` relaunches a running app with them; for a Dock launcher, `osacompile` a one-line applet that runs `open -a Claude --args <flags>` and give it Claude's icon with `NSWorkspace.setIcon_forFile_options_`, the bundle icns alone does not show). With the flags, a covered window takes the message in the background too. Without them the daemon brings Claude forward for the paste and hands focus back to the app you were in within about a second (`BARGEIN_PASTE_FOREGROUND=1` forces that always). A closed window is reopened behind your work. Nothing is ever sent twice |
| **"Hey Claude, open Amazon and YouTube in two tabs"** while Claude is busy | Wake word verified against your voiceprint, the whole sentence transcribed, pasted into the running session as `[voice] ...`; Claude's current speech or listen window is cut so it reads it within seconds |
| Talk over Claude mid-sentence | Barge-in, verified against your ECAPA voiceprint and relative to the TTS voice, stop sent straight to voice-mode's control socket (well under a second). If that turn was going to listen, the mic is handed to voice-mode (red icon). If it was not, the daemon records you itself (icon red as well) and injects the words |
| "Hey Claude" alone | Pastes the resume prompt into the existing session (context kept). A 4 s window accepts a follow-up command as the sentence |
| Give three tasks in a row | Each becomes a background agent; results are spoken as they finish, whichever first |
| New mail in a watched folder, a Teams message in one of the subscribed chats, a WhatsApp message | Arrives via push in seconds, appended to `context/inbox.jsonl`, `today.md` regenerated. Important ones (addressed to you, mentions, direct messages, high importance, client requests) are pushed into the session and spoken |
| You don't answer an important alert for 20 s | The same line is sent to your second WhatsApp number. Reply by text or **voice note**; it comes back as an instruction (voice notes transcribed, Devanagari re-run as Urdu) |
| A client request sits unanswered by your domain for 2 h | Flagged as `UNANSWERED Nh` and escalated like any important item. Not flagged: a "Thank you!" or "Noted, will update" whose own words (quoted thread, greeting and signature stripped) are nothing but courtesy phrases; a supplier's rate or candidate profile (`INBOX_VENDOR_DOMAINS`, vendor phrases); a request you answered on a sibling thread (anything you sent to that sender within the SLA window counts, checked in Sent Items) |
| A Teams or WhatsApp message arrives and you read it yourself within 75 s | Not injected. The receiver waits `INBOX_READ_GRACE_S`, then checks the Teams chat viewpoint or the bridge's unread count; a message you have read is yours to handle |
| A reply lands in a group where you or the agent posted in the last 24 h, or a sender keeps writing after tagging you | Treated as part of a thread you opened (`reply in a thread you posted in`, `follow-up to their tag of you`) and pushed like a mention; on a tag, that sender's messages from the three minutes before are attached |
| A Teams or WhatsApp call starts | Within 20 s the call watch writes `call_active.json`; the daemon disarms, stops listening for the wake word and never opens the mic while the file exists. `[call] started: WhatsApp at 14:02` reaches the session and the agent goes quiet. `audiotap` records `mic-NNN.wav` and `remote-NNN.wav` in 10-minute chunks under `~/.voicemode/calls/<stamp>-<app>/`. When the app releases the mic for 8 s: `[call] ended: ... transcript pending`, then each chunk goes to whisper in 90 s pieces and `[call] transcript ready: <path> (N words)` follows; the agent summarises the transcript, the watcher never does |
| You click **At desk** or **Away** in the menu bar | `presence.json` flips and the receiver injects `[presence] The user is now AWAY: reply on WhatsApp +<second number>, the user is away`; the agent switches channel instead of talking to an empty room |
| Every 12 minutes | `outgoings.py` lists what you sent yourself in the last 15 minutes on WhatsApp, Teams and mail (the agent's own tagged sends excluded), so a thread you already closed is not raised again |
| `~/.voicemode/quiet.json` exists | Nothing is injected or escalated; everything still lands in the digest, read on demand with `digest.sh` or `digest_brief.py` |
| **"Give My translator chat this file and tell it to translate it"** | Daemon opens the *Chat and Cowork* tab, clicks the "My translator chat" row, pastes the file as an attachment, types the instruction, returns to your Code session; the chat saves its output to a folder you watch. `@@axcopy=<Tab>><Row>;back=...` presses the last "Copy" action in that chat and saves the clipboard to `ax_copy.txt` when a reply must be read back |
| "Brief me" / "what came in" | The agent reads `today.md` and speaks the important items first |
| "What's pending?" | `tasks.py list open`, read aloud |
| "Answer me in Urdu" | The whole reply is spoken by Kokoro's Hindi voice (`hf_alpha`), written by the agent in Devanagari, so no transliteration step. One language per reply; mixed-language input is whisper's job. The Perso-Arabic sounds flatten to their Hindi neighbours |
| A WhatsApp message from your second number | Treated as an instruction (text or voice note). Results, including files and images, go back to that number through the bridge's `/send` |
| A status update as a voice note | Write the script in plain spoken English (numbers written out, initialisms spaced so they are read as letters), generate it with local Kokoro (`POST /v1/audio/speech`, `response_format: mp3`), convert with `ffmpeg -i in.mp3 -c:a libopus -b:a 32k -ar 48000 -ac 1 out.ogg`, and send it with `tools/wa_send.sh '{"phone":"...","audio":"/abs/out.ogg","mimetype":"audio/ogg; codecs=opus","ptt":true}'`. It shows as a real voice message. English only: Kokoro's English voice reading Roman Urdu is not intelligible to a native speaker |

## Speaker verification and interrupts

The mic hears the TTS through the speakers, so loudness cannot decide what an interruption is. The daemon embeds the last 1.7 s with ECAPA-TDNN (`speechbrain/spkrec-ecapa-voxceleb`, 192 dims, about 20 ms per check on an Apple CPU) and compares it with your enrolment print and with a print of the TTS voice. Measured on our setup: the enrolled voice scores 0.5 to 0.8 clean and 0.3 to 0.4 while talking over the TTS; the TTS itself scores about 0.07; other people and a video playing in the room score 0.0 to 0.1. An interrupt is accepted when you clear the absolute bar (0.33), or when you clearly beat the TTS print (0.50 with a 0.04 margin, or 0.22 with a 0.15 margin, the second tier exists so acceptance does not wait for the score to climb). The wake word uses the same print at 0.30.

Three rules that cost us a night to find, all in `bargein_daemon.py`:

- A `[voice]` line reaches the agent at its next tool boundary, so the agent's next `converse` call is already the reply. The daemon only cuts speech whose tool call started before the injection landed; otherwise it was cutting every answer at 1.5 s and it looked like barge-in misfiring.
- After a verified interrupt the daemon reads `wait_for_response` off voice-mode's `TOOL_REQUEST_START` event. True: hand the mic to voice-mode, which opens it a fixed 1.2 s after playback stops. False: record at once. A timed check raced voice-mode and stole interrupts as text.
- Send the stop as one JSON line to `control.sock` (`{"command": "skip_forward"}`), not through the `voicemode control` CLI, which is a full Python start-up and cost about 3 s per stop.

On the listening side the patched `converse.py` takes its noise floor from non-speech frames only (the upstream floor was sampled from your own first words and heard your next pause as the end of the take) and the silence threshold is 1.8 s, so people who pause to think are not cut off.

## How we run it (reference setup)

- **Mac** (Apple silicon): Claude desktop app, voice-mode 8.12.0 (Kokoro TTS + whisper `medium` locally as fallback), both daemons under launchd, the WhatsApp bridge, the menu-bar indicator. Audio: a USB headset mic; output through the Mac speakers (the mic hears the TTS, so speaker verification, not loudness, decides what is an interruption).
- **Second PC on the LAN** (Windows, 2× RTX 3090): an OpenAI-compatible whisper server (`large-v3`) on port 9000. voice-mode's `VOICEMODE_STT_BASE_URLS` lists it first and the local server second, so a dead LAN box degrades to `medium`, never to silence. LAN overhead measured ~11 ms against ~1 s of inference. The wake-word and voice-note transcriptions use the same server.
- **No API keys.** Nothing goes to a paid API. Microsoft Graph and WhatsApp are your own accounts; the Graph sign-in is a delegated device-code login cached with refresh tokens on disk (0600).
- **Public hostname**: an existing Cloudflare Tunnel on the other PC with one public hostname pointed at `http://<mac-lan-ip>:8898`. Tailscale Funnel works the same way.
- **Smaller machines**: everything except whisper `large-v3` runs comfortably on the Mac alone (Kokoro is ~80 M parameters, resemblyzer ~10 ms per check on CPU). Use whisper `medium` or `small` locally and drop the LAN box; accuracy on names suffers a little, nothing else changes.
- **What it costs to run**: two Python processes (~300 MB with the speaker model), one Node process for the bridge, and the GPU box only when speech is being transcribed.

## WhatsApp bridge

The receiver expects a small local Baileys bridge on `127.0.0.1:47823` with these routes: `GET /status`, `GET /qr` (also writes a PNG), `POST /send {phone, message, image?, document?}` (`image` / `document` is a path on this machine, `message` becomes the caption), `GET /chats`, `GET /messages?phone=&limit=` (each item with `id`, `type`, `from`, `text`, `timestamp`), `GET /media?phone=&id=` (raw bytes + content type), and `POST /webhooks {url, events, secret}` delivering `{event, timestamp, data:{chatJid,isGroup,fromMe,author,text,messageId,timestamp,type}}` signed with `X-WA-Signature: sha256=<hmac>`. `whatsapp-bridge/daemon.js.patch` adds the `/media` route and the `id`/`type` fields, and `daemon.js.send-media.patch` adds image, document and audio sending (`audio` is a path; an `.ogg`/`.opus` file goes out as a voice note with `ptt: true`, anything else as a playable audio file) plus `mentions` (a list of jids whose `@number` tags appear in the text), to a bridge built on [Baileys](https://github.com/WhiskeySockets/Baileys) with `syncFullHistory: true`. Link the device by scanning the QR from `tools/qr_render.py`'s image or the bridge's PNG; a phone-side "couldn't link" usually means a second copy of the bridge is fighting for the port, or a stale session directory.

## Windows and Linux

| Part | Status |
|---|---|
| `inbox/inbox_hooks.py`, backfills, `tools/` | Plain Python. Run on Windows (native or WSL) and Linux. Install as a service with Task Scheduler / NSSM on Windows, systemd on Linux (the launchd templates show the two commands). |
| WhatsApp bridge | Node, runs anywhere. |
| whisper / Kokoro | Already run on Windows in the reference setup (GPU box). |
| `daemon/bargein_daemon.py`, ears | sounddevice, webrtcvad, resemblyzer and the whisper calls all work on Windows. Must run natively, not in WSL, to reach the microphone and the app. |
| `daemon/bargein_daemon.py`, hands | macOS only today: the Accessibility API and CGEvent code that finds the Claude composer, clicks a sidebar row and pastes. See the porting guide below. |
| `daemon/voicemode_indicator.py` | macOS menu bar (rumps). A tray-icon equivalent with `pystray` is a natural port. |
| `daemon/launcher.c` | macOS TCC only; not needed elsewhere. |
| voice-mode converse loop on Windows | Not verified yet. |

### Porting the daemon's hands to Windows (guide, not done yet)

The macOS-specific code is confined to a handful of functions in `daemon/bargein_daemon.py`. Everything else in the daemon is portable Python.

| Function | What it does on macOS | Windows equivalent |
|---|---|---|
| `_native_paste(text)` | Activates the Claude app, finds the composer (`_ax_find_composer`: an `AXTextArea` whose description or class mentions prompt / ProseMirror), sets focus, puts text on the pasteboard, sends Cmd+V and Return with `CGEvent` | `uiautomation` (or `pywinauto` with the `uia` backend): find the Edit control inside the Claude window, `SetFocus()`, set the clipboard with `pywin32` (`win32clipboard`), send Ctrl+V and Enter with `SendInput` (`pyautogui` or `keyboard`) |
| `_ax_find_titled(pid, title)` + `_ax_press(el)` | Breadth-first search of the window's accessibility tree for a sidebar entry by title, then `AXPress` or a click at its centre | Walk the UIA tree for a `ListItem` / `Button` whose `Name` contains the title, then `Invoke()` or click its bounding rectangle |
| `switch_session(path)` | Clicks a tab, then a row ("Chat and Cowork>My chat") | Same two-step, with the UIA names your app build shows; dump them once with the tool in the next row |
| `@@axtitles` handler | Dumps every labelled element to `ax_dump.txt` so you can learn the current labels | `uiautomation.EnumAndLogControl` or `pywinauto`'s `print_control_identifiers()` |
| `_native_attach(path)` | Puts a file URL on the pasteboard and pastes it, which the composer takes as an attachment | Set the clipboard to `CF_HDROP` with the file path (`win32clipboard`, `DROPFILES` struct) and send Ctrl+V; or drive the paperclip button through UIA and the file dialog |
| `launch_session()` fallback | `open claude://code/new?q=...` when the app is not running | `start claude://code/new?q=...` |
| `_start_verify_server`, `is_user_speaking`, wake and barge-in loops | Portable | Unchanged; `sounddevice` picks the mic by name via `BARGEIN_MIC_NAME` |
| `fire()` | Calls `voicemode control skip-forward` (voice-mode's control socket) | Same command if voice-mode runs on Windows; verify that first |

How to test a port, piece by piece:

1. `python bargein_daemon.py` with `BARGEIN_WAKE=0` and speak over TTS: confirms audio, VAD and the voiceprint on your machine.
2. Write `@@axtitles` into `inject.txt`: confirms you can read the app's control tree; use the dump to set `BARGEIN_HOME_SESSION`.
3. Write a plain line into `inject.txt`: confirms focus + paste into the composer.
4. Write `@@session=<Tab>><Row>;back=<Tab>><Row>;file=<path>;msg=hello`: confirms sidebar navigation and the attachment.
5. Only then enable the wake word and barge-in.

Run the daemon natively on Windows (not WSL): the microphone and the app's UI tree are only reachable from a Windows process. The receiver can live in WSL or natively.

If you port it, please open an issue or a pull request with your UIA control names and what you changed; that is the fastest way for the next person.

## Privacy and safety

- Everything stays local except Graph/WhatsApp traffic to their own services and STT/TTS to endpoints you choose.
- The receiver validates Graph `clientState` and WhatsApp HMAC signatures; unsigned WhatsApp posts are rejected because the endpoint is reachable through your tunnel.
- Do **not** commit: `voiceprint.npy`, `msal_cache.json`, `context/secret`, `context/*.jsonl`, `context/media/`, `history/`, `calls/`, `templates/signature.html`, `agent_sent_ids.jsonl`, or any memory files. `.gitignore` covers them.
- The daemon types into the Claude composer with your Accessibility grant. A mis-transcription becomes an instruction; keep the `[voice]` prefix so the agent confirms anything destructive.
- The call watch records both sides of your calls to disk and sends the audio to whatever whisper endpoint you configure. Recording calls has legal conditions that differ by country; tell the people on the call, and keep the whisper endpoint on your own machines.
- `outgoings.py` reads the WhatsApp desktop app's local database (a copy, WAL included) and redacts short messages that look like a credential before printing; it prints the rest of your sends verbatim into the session.

## Known limits

- Injected messages surface at Claude's next tool boundary; the daemon cuts speech/listen windows to keep that under a few seconds, not instant.
- Speaker verification is not biometric. ECAPA separates you from the TTS and from other voices with a wide margin in our measurements, but audio playing in the room still delays acceptance by a second or two while your score climbs, and a very similar voice has not been tested.
- voice-mode's Kokoro launchd template sets `UVICORN_LIMIT_MAX_REQUESTS=25`, so uvicorn exits cleanly after 25 requests and launchd restarts it; with a menu bar probing health that is a restart every ten minutes, seen as a blue icon with no sound, a line that dies mid-sentence, or an OpenAI failover error. Set `VOICEMODE_KOKORO_MAX_REQUESTS` high in `voicemode.env` (zero means exit at once) and let `heal_voicemode.py` strip the key from the plist.
- Graph allows about 100 chat subscriptions per user. The receiver subscribes the 90 most recently active chats and polls the rest for new messages every two minutes, so a message in a dormant chat can take up to two minutes to arrive.
- Sidebar navigation depends on the app's accessibility labels ("Chat and Cowork", row titles); an app update may rename them. `@@axtitles` dumps the current labels.
- WhatsApp calls cannot be placed by any unofficial library. Old WhatsApp media may fail to download (expired on WhatsApp's servers).
- Patches target voice-mode 8.12.0 exactly.
- A Claude window closed with the red cross, or minimised, leaves the app running with no window the daemon can paste into (three messages were lost that way once: the daemon saw no window at all, fell back to blind keystrokes, and logged them as sent). The daemon now un-minimises the window over accessibility and, for a closed window, sends the app the reopen a Dock click sends, waits for the window, and then pastes; both cases tested at 3 to 4 s. Injected text is now never sent blind. A failed paste goes to a retry queue (every 30 s for 15 min), a confirmed paste writes `~/.voicemode/context/delivered`, the receiver tells the second phone after 90 s without that marker, and the daemon messages it if the retries run out. An `ax probe` line every 10 minutes in `bargein.log` shows how many nodes and inputs the app exposes.
- Kokoro's Hindi voices work through the plain `converse` message path (`voice="hf_alpha"`). Inside a pipelined `turns` survey the same voice comes back as `tts_failed` before any request reaches Kokoro; speak Urdu replies as single calls.
- Call watch: the process tap needs macOS 14.2 or later and a "System Audio Recording Only" grant for `audiotap`; without the binary, `call_watch.py` still detects calls over ctypes and silences the agent, but records nothing. The tap's aggregate device only ticks while the tapped app produces output, so the remote file starts when the other side first makes a sound. The start rule (mic and output together for 20 s) means the first 20 s of a call are not recorded and a long voice note can look like a call for a moment. Teams helper processes come and go during a call; the tap list is refreshed every 5 s and on macOS 26 the description also carries the bundle ids, so a helper that respawns is picked up. `--test-record` and `--dry-run` are the two checks worth running after an OS update.
- `dns_fallback.py` only resolves A records (IPv4) and only for the hosts a daemon asks for after the system resolver has failed; it is a stopgap for a dead resolver, not a resolver.
- `outgoings.py` and `watch_group.py` read the WhatsApp desktop app's ChatStorage schema (`ZWAMESSAGE`, `ZWACHATSESSION`, `ZWAGROUPMEMBER`); a WhatsApp update that changes it breaks them until the queries are adjusted.

## Credits

Built on [voice-mode](https://github.com/mbailey/voicemode) (the MCP voice loop and its control channel), [resemblyzer](https://github.com/resemble-ai/Resemblyzer), [webrtcvad](https://github.com/wiseman/py-webrtcvad), [Baileys](https://github.com/WhiskeySockets/Baileys), whisper.cpp and [Kokoro](https://github.com/hexgrad/kokoro).

Inspiration, read but not used: [aayushdebugging/claude-voice](https://github.com/aayushdebugging/claude-voice), which showed that barge-in with Claude on a local, zero-cost stack was practical, and the interruption handling in [Pipecat](https://github.com/pipecat-ai/pipecat) and [LiveKit Agents](https://github.com/livekit/agents).

## License

MIT.
