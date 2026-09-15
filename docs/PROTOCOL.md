# Agent-side protocol (for the Claude session)

- Keep the voice loop alive: call `converse` again after every turn; never end it on "bye" or a silence timeout.
- `[voice] ...` lines are spoken instructions injected by the daemon. Acknowledge by voice, spawn long work as background agents, confirm anything destructive (STT mishears).
- `[inbox] ...` lines are live alerts from the receiver. Say them out loud at once; touch `~/.voicemode/context/ack` when the user responds so no WhatsApp escalation is sent.
- Before any reply into a chat or thread, re-read it in the same turn.
- To drive a named chat session: write `@@session=<Tab>><Row>;back=<Tab>><Row>;file=/abs/path;msg=<text>` to `inject.txt`; ask the chat to save its output into a watched folder and read that back.
- Log every assigned task with `tasks.py`; read `today.md` at session start and on "brief me".
- A Teams or WhatsApp mention or direct message is conversation first: reply as the user right away in their register, ask what is needed, answer each reply promptly, and escalate only the gathered requirement, only if urgent and the user is not responding. Mail alerts and the unanswered-request sweep still escalate on their own.
- Speak with `wait_for_response=true` whenever an answer is possible. On such a turn an interrupt hands the mic to voice-mode. On a turn spoken without listening, the daemon records the interruption itself and it arrives as a `[voice]` line; answer it by voice at once, the daemon will not cut that reply.
- `~/.voicemode/indicator/vm_recording.flag` means voice-mode holds the mic; `daemon_capture.flag` means the daemon does. Anything else that wants the mic (the enrolment recorder, for example) should wait for both to be absent, or raise `vm_recording.flag` so the daemon steps off the device.
- One language per spoken reply. English on the default voice; when the user wants Urdu, send the whole reply as one plain `converse` call with `voice="hf_alpha"` and the text written in Devanagari (not inside `turns`). Never mix languages inside one spoken reply; mixed input is fine.
- An instruction that arrives from the user's second WhatsApp number is the user's own. Do the work, then confirm on that same number (text, or a file via the bridge's `/send` with `image` or `document`), and log it in the ledger like any other task.
- An instruction from the second phone that reaches the session is already confirmed delivered by the daemon; the user only hears from the receiver when it did not arrive. Answer it promptly on that phone anyway, that reply is the real acknowledgement.

## Calls (`[call]` lines from call_watch.py)

- `[call] started: <Teams|WhatsApp> at HH:MM`: a call is live and `~/.voicemode/indicator/call_active.json` exists. Stop speaking at once and do not start a `converse` turn until the call ends; the daemon has already stopped listening for the wake word and will not open the mic. Text instructions (`[inbox]`, the second phone) still arrive and can be worked on silently.
- `[call] ended: <app> after <N> min, recording at <dir>, transcript pending`: the call is over, the voice loop may resume. Do not read the WAV files; wait for the transcript.
- `[call] transcript ready: <path> (<N> words)`: read `<path>` (one line per segment, `[HH:MM:SS] MIC:` is the user, `REMOTE:` the other side), summarise it in a few lines, log any commitments made on the call in the task ledger, and only then speak. `[call] transcript failed for <dir>: ...` means run `call_watch.py --transcribe <dir>` later or tell the user the recording is there untranscribed.
- `[call]` and `[inbox]` lines never cut speech in flight; they surface at the next tool boundary. A `[voice]` line still does.

## Presence and quiet mode

- `[presence] The user is now AT DESK: ...` or `... AWAY: ...`: switch channel. At desk means answer by voice in the session; away means write to the second WhatsApp number (through `tools/wa_send.sh`) and keep the room quiet. `tools/presence.py` prints the current state (exit 0 at desk, 1 away) for a check before speaking.
- While `~/.voicemode/quiet.json` exists the receiver injects nothing and escalates nothing; read the digest on demand with `tools/digest.sh [minutes]` or `tools/digest_brief.py [minutes]` and delete the file to resume live alerts.

## Outgoings (the 12-minute contract)

- `tools/outgoings.py --min 15` runs every 12 minutes: the session loops it itself (`/loop 12m`), or an external cron runs it with `agent.env` sourced and delivers the output as one `[inbox] outgoings: ...` block. Every line is something the user sent in person: `HH:MM | WhatsApp | YOU -> <chat> | <text>`, `HH:MM | Teams | YOU in <chat> | <text>`, `HH:MM | Email | YOU -> <to> | <subject> | <preview>`.
- Before answering anyone on a thread, or reporting a thread's status, read the latest outgoings block (or run the script). A thread the user has answered themselves is closed for the agent unless they say otherwise; never re-raise it, never send a second reply to it.
- Every send the agent makes on WhatsApp goes through `tools/wa_send.sh` (or `inbox_hooks._wa_send`), which logs the text to `~/.voicemode/agent_sent_ids.jsonl`; that log is how `outgoings.py` and the digest tell the agent's messages from the user's. A send made any other way will come back looking like the user's own words.
- Messages the agent posts as the user carry the agent tag (`[AI]` by default, `OUTGOINGS_AGENT_PREFIXES`) so people know, and so the tag can be filtered.

## Mail bodies

- Any mail body the agent builds goes through `send_mail.build_body(text)`: paragraphs separated by blank lines, the stored signature template appended. Never type a signature by hand. If `build_body` refuses (template missing or failing the marker check), stop and tell the user; do not send a mail without the proper signature.
