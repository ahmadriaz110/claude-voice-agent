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
 inbox_hooks.py (:8898, behind a Cloudflare/Tailscale hostname) ──┘
   ├── Graph change notifications: mail folders + per-chat Teams subscriptions (auto-renew)
   ├── WhatsApp webhook (Baileys daemon on 127.0.0.1:47823, HMAC-signed), media + whisper for voice notes
   ├── inbox.jsonl / today.md digest, escalation to a second WhatsApp number, ack file
   └── sweep: client requests unanswered by your domain for N hours
```

## Components

| Path | What it is |
|---|---|
| `daemon/bargein_daemon.py` | Wake word, barge-in, post-interrupt capture, ECAPA speaker verification (with a resemblyzer fallback) and the verify server voice-mode calls, native AX paste into the Claude composer, `@@session=` named-session commands, `@@axtitles` label dump |
| `daemon/launcher.c` | Tiny fork-not-exec launcher so the daemon keeps its own macOS TCC (Accessibility / Microphone) identity |
| `daemon/voicemode_indicator.py` | rumps menu-bar indicator (listening / thinking / speaking) |
| `inbox/inbox_hooks.py` | Push receiver: Graph subscriptions, WhatsApp webhook, digest, triage, escalation, media, unanswered-request sweep |
| `inbox/backfill.py`, `inbox/backfill_whatsapp.py` | 60-day history pulls |
| `patches/*.patch` | Changes to voice-mode 8.12.0 (`simple_failover.py`, `tools/converse.py`): connect timeout, prompt-echo, URL and hallucination guards, energy-gated VAD with a noise floor taken from non-speech frames only, speaker-gated end-of-turn, optional speaker filter on the listen window, Urdu-not-Hindi re-transcription |
| `tools/heal_voicemode.py` | Re-applies / verifies the patches after `uv tool upgrade`, and strips the 25-request limit from the Kokoro launchd plist (see Known limits) |
| `tools/enrol/` | `enrol_record.py` (three minutes of you reading `enrolment_text.txt`), `enrol_embed.py` (builds the ECAPA and resemblyzer prints), `calibrate.py` (scores your recording, your own TTS and rejected room clips, suggests thresholds) |
| `tools/tasks.py` | Task ledger (`add / start / done / block / list`) |
| `tools/qr_render.py` | Renders a terminal QR (WhatsApp linking) to an image |
| `launchd/*.plist` | launchd templates (`__HOME__` is substituted by `install.sh`) |

## Requirements

- macOS, the Claude desktop app (Code tab), Python 3.11 (a venv per `install.sh`)
- [voice-mode](https://github.com/mbailey/voicemode) 8.12.0 installed with `uv tool`, with local whisper + Kokoro services
- Python packages: `speechbrain`, `torch`, `torchaudio` (ECAPA-TDNN, downloads ~80 MB of weights from Hugging Face on first run into `~/.voicemode/indicator/ecapa/`), `resemblyzer` (fallback), `webrtcvad`, `sounddevice`, `numpy`, `scipy`, `pyobjc-framework-ApplicationServices`, `pyobjc-framework-Quartz`, `pyobjc-framework-Cocoa`, `msal`, `pypdf`, `rumps`
- Accessibility + Microphone grants for the daemon (macOS prompts on first run)
- A public HTTPS hostname that forwards to this Mac (Cloudflare Tunnel public hostname → `http://<mac-lan-ip>:8898`, or Tailscale Funnel) for Graph push
- A Microsoft 365 account; `inbox_hooks.py --login` runs a device-code sign-in with the Microsoft Graph Command Line Tools public client (delegated `Mail.Read Chat.Read ChatMessage.Read`). No app registration needed, but your tenant must allow that client.
- A Baileys WhatsApp bridge exposing `/status /qr /send /chats /messages /media /webhooks` on `127.0.0.1:47823` (the `daemon.js` in `whatsapp-bridge/` notes describe the routes this receiver expects; the bridge itself is not included)

## Setup (short form)

1. `./install.sh`, creates the venv, installs packages, fills `launchd/*.plist` with your home path, copies them to `~/Library/LaunchAgents`.
2. Copy `config.example.env` to `~/.voicemode/indicator/agent.env` and fill in your values (mailbox, own domain, escalation number, hostname, home session title, mic name, STT URLs).
3. Enrol your voice: `tools/enrol/enrol_record.py 180` while you read `tools/enrol/enrolment_text.txt` aloud (the daemon stays off the mic during it), then `tools/enrol/enrol_embed.py` writes `voiceprint_ecapa.npy` (and a resemblyzer print as fallback). Run `tools/enrol/calibrate.py` once to see how your voice, your TTS voice and room noise score, and set the thresholds in `agent.env` from that.
4. Apply `patches/` to your voice-mode install (`patch -p1 -d <site-packages>`), then run `tools/heal_voicemode.py` to verify.
5. `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.voicemode.bargein.plist` and the same for `com.voicemode.inbox.plist`.
6. `inbox_hooks.py --login`, write your public URL to `~/.voicemode/context/public_url.txt`; subscriptions are created and renewed automatically (`curl 127.0.0.1:8898/health`).
7. Register the WhatsApp webhook on your bridge: `POST /webhooks {url: http://127.0.0.1:8898/whatsapp, secret: <~/.voicemode/context/secret>}`.

In Claude, keep the voice loop alive with the `converse` tool; treat `[voice] ...` and `[inbox] ...` lines as spoken input and live alerts. A short protocol for that is in `docs/PROTOCOL.md`.

## Features, with the commands that drive them

| Say / do | What happens |
|---|---|
| **"Hey Claude, open Amazon and YouTube in two tabs"** while Claude is busy | Wake word verified against your voiceprint, the whole sentence transcribed, pasted into the running session as `[voice] ...`; Claude's current speech or listen window is cut so it reads it within seconds |
| Talk over Claude mid-sentence | Barge-in, verified against your ECAPA voiceprint and relative to the TTS voice, stop sent straight to voice-mode's control socket (well under a second). If that turn was going to listen, the mic is handed to voice-mode (red icon). If it was not, the daemon records you itself (icon red as well) and injects the words |
| "Hey Claude" alone | Pastes the resume prompt into the existing session (context kept). A 4 s window accepts a follow-up command as the sentence |
| Give three tasks in a row | Each becomes a background agent; results are spoken as they finish, whichever first |
| New mail in a watched folder, a Teams message in one of the subscribed chats, a WhatsApp message | Arrives via push in seconds, appended to `context/inbox.jsonl`, `today.md` regenerated. Important ones (addressed to you, mentions, direct messages, high importance, client requests) are pushed into the session and spoken |
| You don't answer an important alert for 20 s | The same line is sent to your second WhatsApp number. Reply by text or **voice note**; it comes back as an instruction (voice notes transcribed, Devanagari re-run as Urdu) |
| A client request sits unanswered by your domain for 2 h | Flagged as `UNANSWERED Nh` and escalated like any important item |
| **"Give All CVs this file and tell it to translate it"** | Daemon opens the *Chat and Cowork* tab, clicks the "All CVs" row, pastes the file as an attachment, types the instruction, returns to your Code session; the chat saves its output to a folder you watch |
| "Brief me" / "what came in" | The agent reads `today.md` and speaks the important items first |
| "What's pending?" | `tasks.py list open`, read aloud |
| "Answer me in Urdu" | The whole reply is spoken by Kokoro's Hindi voice (`hf_alpha`), written by the agent in Devanagari, so no transliteration step. One language per reply; mixed-language input is whisper's job. The Perso-Arabic sounds flatten to their Hindi neighbours |
| A WhatsApp message from your second number | Treated as an instruction (text or voice note). Results, including files and images, go back to that number through the bridge's `/send` |

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

The receiver expects a small local Baileys bridge on `127.0.0.1:47823` with these routes: `GET /status`, `GET /qr` (also writes a PNG), `POST /send {phone, message, image?, document?}` (`image` / `document` is a path on this machine, `message` becomes the caption), `GET /chats`, `GET /messages?phone=&limit=` (each item with `id`, `type`, `from`, `text`, `timestamp`), `GET /media?phone=&id=` (raw bytes + content type), and `POST /webhooks {url, events, secret}` delivering `{event, timestamp, data:{chatJid,isGroup,fromMe,author,text,messageId,timestamp,type}}` signed with `X-WA-Signature: sha256=<hmac>`. `whatsapp-bridge/daemon.js.patch` adds the `/media` route and the `id`/`type` fields, and `daemon.js.send-media.patch` adds image and document sending, to a bridge built on [Baileys](https://github.com/WhiskeySockets/Baileys) with `syncFullHistory: true`. Link the device by scanning the QR from `tools/qr_render.py`'s image or the bridge's PNG; a phone-side "couldn't link" usually means a second copy of the bridge is fighting for the port, or a stale session directory.

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
- Do **not** commit: `voiceprint.npy`, `msal_cache.json`, `context/secret`, `context/*.jsonl`, `context/media/`, `history/`, or any memory files. `.gitignore` covers them.
- The daemon types into the Claude composer with your Accessibility grant. A mis-transcription becomes an instruction; keep the `[voice]` prefix so the agent confirms anything destructive.

## Known limits

- Injected messages surface at Claude's next tool boundary; the daemon cuts speech/listen windows to keep that under a few seconds, not instant.
- Speaker verification is not biometric. ECAPA separates you from the TTS and from other voices with a wide margin in our measurements, but audio playing in the room still delays acceptance by a second or two while your score climbs, and a very similar voice has not been tested.
- voice-mode's Kokoro launchd template sets `UVICORN_LIMIT_MAX_REQUESTS=25`, so uvicorn exits cleanly after 25 requests and launchd restarts it; with a menu bar probing health that is a restart every ten minutes, seen as a blue icon with no sound, a line that dies mid-sentence, or an OpenAI failover error. Set `VOICEMODE_KOKORO_MAX_REQUESTS` high in `voicemode.env` (zero means exit at once) and let `heal_voicemode.py` strip the key from the plist.
- Graph allows about 100 chat subscriptions per user. The receiver subscribes the 90 most recently active chats and polls the rest for new messages every two minutes, so a message in a dormant chat can take up to two minutes to arrive.
- Sidebar navigation depends on the app's accessibility labels ("Chat and Cowork", row titles); an app update may rename them. `@@axtitles` dumps the current labels.
- WhatsApp calls cannot be placed by any unofficial library. Old WhatsApp media may fail to download (expired on WhatsApp's servers).
- Patches target voice-mode 8.12.0 exactly.
- Kokoro's Hindi voices work through the plain `converse` message path (`voice="hf_alpha"`). Inside a pipelined `turns` survey the same voice comes back as `tts_failed` before any request reaches Kokoro; speak Urdu replies as single calls.

## Credits

Built on [voice-mode](https://github.com/mbailey/voicemode) (the MCP voice loop and its control channel), [resemblyzer](https://github.com/resemble-ai/Resemblyzer), [webrtcvad](https://github.com/wiseman/py-webrtcvad), [Baileys](https://github.com/WhiskeySockets/Baileys), whisper.cpp and [Kokoro](https://github.com/hexgrad/kokoro).

Inspiration, read but not used: [aayushdebugging/claude-voice](https://github.com/aayushdebugging/claude-voice), which showed that barge-in with Claude on a local, zero-cost stack was practical, and the interruption handling in [Pipecat](https://github.com/pipecat-ai/pipecat) and [LiveKit Agents](https://github.com/livekit/agents).

## License

MIT.
