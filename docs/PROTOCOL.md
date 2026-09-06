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
