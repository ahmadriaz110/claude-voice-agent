# Agent-side protocol (for the Claude session)

- Keep the voice loop alive: call `converse` again after every turn; never end it on "bye" or a silence timeout.
- `[voice] ...` lines are spoken instructions injected by the daemon. Acknowledge by voice, spawn long work as background agents, confirm anything destructive (STT mishears).
- `[inbox] ...` lines are live alerts from the receiver. Say them out loud at once; touch `~/.voicemode/context/ack` when the user responds so no WhatsApp escalation is sent.
- Before any reply into a chat or thread, re-read it in the same turn.
- To drive a named chat session: write `@@session=<Tab>><Row>;back=<Tab>><Row>;file=/abs/path;msg=<text>` to `inject.txt`; ask the chat to save its output into a watched folder and read that back.
- Log every assigned task with `tasks.py`; read `today.md` at session start and on "brief me".
- A Teams or WhatsApp mention or direct message is conversation first: reply as the user right away in their register, ask what is needed, answer each reply promptly, and escalate only the gathered requirement, only if urgent and the user is not responding. Mail alerts and the unanswered-request sweep still escalate on their own.
