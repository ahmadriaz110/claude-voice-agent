#!/usr/bin/env python3
"""
VoiceMode menu bar indicator + launcher.

Menu bar glyph reflects what VoiceMode is doing right now:

    ⚪  idle          - not listening, not speaking
    🔴  listening     - mic is open, recording you
    🟡  transcribing  - audio sent to Whisper, waiting on text
    🔵  speaking      - playing TTS back at you

State comes from tailing VoiceMode's own JSONL event log, so it reflects
what VoiceMode actually did rather than inferring from mic state. It also
won't false-positive when Zoom or anything else uses the microphone.

The menu can also START a voice conversation, so you don't have to open a
session and type. Three ways:

    New voice session      - fresh context, launched in your home dir
    Continue last session  - `claude -c`, the most recent conversation
    Resume session >        - pick a specific past session by title

Each opens a real Terminal window, because voice needs an interactive TTY
to record and play audio.

Deliberately NOT here: a start/stop listening toggle. VoiceMode has no
always-on listener - the agent opens the mic for the duration of a
question and closes it after - so a toggle would imply control that does
not exist. That belongs with the streaming/barge-in work.
"""

import json
import os
import subprocess
import urllib.parse
from datetime import datetime, date
from itertools import islice
from pathlib import Path

import rumps

EVENTS_DIR = Path.home() / ".voicemode" / "logs" / "events"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
PREFS_PATH = Path.home() / ".voicemode" / "indicator" / "prefs.json"
# Presence of this file tells the agent to stop looping at the end of the
# current turn. The agent checks it between turns, so pause takes effect
# after the turn in flight - it cannot interrupt one mid-sentence.
PAUSE_FLAG = Path.home() / ".voicemode" / "indicator" / "paused.flag"
POLL_SECONDS = 0.25
MAX_SESSIONS = 8

# `claude --model` aliases, and `--effort` levels. There is no "off" for
# effort - low is the floor - so the menu does not pretend otherwise.
MODELS = ["opus", "sonnet", "fable"]
# settings.json's effortLevel enum stops at xhigh (the CLI --effort flag also
# accepts "max", but writing that to settings would be schema-invalid).
EFFORTS = ["low", "medium", "high", "xhigh"]

# Claude Code reads this at session start, so writing here is what makes the
# choice apply to DESKTOP APP sessions too - a claude:// deep link cannot
# carry --model/--effort.
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"

# Defaults tuned for voice: lowest effort, because latency between turns
# is dominated by thinking time, not by STT or TTS.
DEFAULT_PREFS = {"model": "opus", "effort": "low", "thinking": True}

# Kept ASCII-only: it is interpolated into an AppleScript string, and
# smart quotes / em-dashes there are a reliable source of pain.
VOICE_PROMPT = "Start voice mode and talk to me."

# Idle uses a TEXT glyph, not an emoji. Emoji render as fixed-colour
# images, so a white emoji circle vanishes against a light menu bar. A
# text glyph is drawn in the menu bar's own text colour, so it is dark on
# light and light on dark - visible either way. The active states stay as
# emoji because their colour is the whole point and reads on both themes.
IDLE_GLYPH = "○"

# Animation frames per state. Idle stays STATIC - a permanently moving menu bar
# item is a distraction when nothing is happening. The active states animate so
# they are noticeable in peripheral vision without being read.
#
# Listening  : a pulsing level meter - suggests audio coming IN
# Transcribing: a rotating spinner - work in progress, indeterminate
# Speaking   : an expanding wave - suggests audio going OUT
# Text glyph animations render in the menu bar's own colour, so they stay
# visible on light and dark. Emoji cannot do that, which is why idle is text.
LEVELS = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "▆", "▅", "▄", "▃", "▂"]
SPINNER = ["◐", "◓", "◑", "◒"]
WAVES = ["(", "((", "(((", "((((", "(((", "(("]

ANIM_FRAMES = {
    "Listening":    ["🔴 " + LEVELS[i % len(LEVELS)] for i in range(len(LEVELS))],
    "Transcribing": ["🟡 " + f for f in SPINNER],
    "Speaking":     ["🔵 " + w for w in WAVES],
}
ANIM_INTERVAL = 0.12          # seconds per frame

STATES = {
    "RECORDING_START":    ("🔴", "Listening"),
    "RECORDING_END":      (IDLE_GLYPH, "Idle"),
    "STT_START":          ("🟡", "Transcribing"),
    "STT_COMPLETE":       (IDLE_GLYPH, "Idle"),
    "TTS_PLAYBACK_START": ("🔵", "Speaking"),
    "TTS_PLAYBACK_END":   (IDLE_GLYPH, "Idle"),
}


def run_in_terminal(command: str) -> None:
    """Open Terminal.app and run `command` in a new window, then focus it."""
    escaped = command.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        f'tell application "Terminal"\n'
        f'  do script "{escaped}"\n'
        f'  activate\n'
        f'end tell'
    )
    subprocess.Popen(["osascript", "-e", script])


def probe(url: str, timeout: float = 1.5) -> bool:
    """Cheap liveness check for a local/LAN HTTP service."""
    import urllib.request
    import urllib.error
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True          # responded at all == alive
    except Exception:
        return False


def open_url(url: str) -> None:
    """Hand a URL to LaunchServices (i.e. open it in Claude.app)."""
    subprocess.Popen(["open", url])


def app_new_session(prompt: str) -> None:
    """Open a NEW Claude Code tab in the desktop app, prompt pre-filled.

    Preferred over the Terminal route: the desktop app carries all the
    connectors (Zoho, Deel, Outlook, computer-use, browser control) that a
    bare `claude` CLI session does not have.

    The prompt is pre-filled but NOT auto-sent - press Enter. There is no
    deep-link parameter to send it, so this is as close as the scheme gets.
    """
    open_url("claude://code/new?q=" + urllib.parse.quote(prompt, safe=""))


def type_into_claude(prompt: str) -> None:
    """Bring Claude.app forward and type `prompt`, then press Return.

    This is how we resume voice in the session you are ALREADY in. There
    is no supported way for an outside process to inject a message into a
    running Claude Code session - `claude://` only opens NEW tabs, and the
    session-messaging MCP tool is callable by agents, not by us. So we
    drive the UI instead.

    Requires Accessibility permission for this process (System Settings ->
    Privacy & Security -> Accessibility). Without it, System Events raises
    and nothing is typed.
    """
    escaped = prompt.replace("\\", "\\\\").replace('"', '\\"')

    # Verify Claude is actually frontmost BEFORE typing. Typing blind is the
    # dangerous failure mode: if Claude is slow to focus, or the user clicks
    # away mid-delay, the keystrokes land in whatever app is in front -
    # a chat window, a terminal, an editor - and then press Return.
    # We poll for focus, and abort rather than type into the wrong place.
    script = (
        'tell application "Claude" to activate\n'
        'set claudeReady to false\n'
        'repeat 20 times\n'
        '  delay 0.15\n'
        '  tell application "System Events"\n'
        '    if (name of first application process whose frontmost is true) '
        'is "Claude" then\n'
        '      set claudeReady to true\n'
        '      exit repeat\n'
        '    end if\n'
        '  end tell\n'
        'end repeat\n'
        'if not claudeReady then return "ABORT: Claude did not come to front"\n'
        'delay 0.25\n'
        'tell application "System Events"\n'
        f'  keystroke "{escaped}"\n'
        '  delay 0.2\n'
        '  key code 36\n'          # Return
        'end tell\n'
        'return "OK"'
    )

    try:
        res = subprocess.run(["osascript", "-e", script],
                             capture_output=True, text=True, timeout=20)
        out = (res.stdout or "").strip()
        err = (res.stderr or "").strip()
    except subprocess.TimeoutExpired:
        out, err = "", "osascript timed out"

    if out != "OK":
        # -1719 / -25211 are the usual "not authorised for Accessibility"
        # codes; anything else we surface verbatim so it is diagnosable.
        if "-1719" in err or "-25211" in err or "not allowed" in err.lower():
            detail = ("Grant Accessibility to VoiceMode Indicator in "
                      "System Settings > Privacy & Security.")
        else:
            detail = (err or out or "Unknown error")[:180]
        rumps.notification("VoiceMode", "Could not resume voice", detail)


def shq(s: str) -> str:
    """Single-quote for the shell."""
    return "'" + str(s).replace("'", "'\\''") + "'"


def patch_claude_settings(**keys) -> bool:
    """Merge `keys` into ~/.claude/settings.json, preserving everything else.

    Read-modify-write rather than overwrite: that file also holds
    permissions, theme, and notification prefs, and clobbering it would
    silently disable them. A malformed existing file aborts rather than
    being replaced.
    """
    try:
        data = json.loads(CLAUDE_SETTINGS.read_text()) if CLAUDE_SETTINGS.exists() else {}
        if not isinstance(data, dict):
            return False
    except (OSError, json.JSONDecodeError):
        return False

    data.update(keys)
    try:
        CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        tmp = CLAUDE_SETTINGS.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.replace(CLAUDE_SETTINGS)   # atomic-ish; avoids a torn file
        return True
    except OSError:
        return False


def load_prefs() -> dict:
    prefs = dict(DEFAULT_PREFS)
    try:
        prefs.update(json.loads(PREFS_PATH.read_text()))
    except (OSError, json.JSONDecodeError):
        pass
    # Guard against a hand-edited prefs file naming something invalid.
    if prefs.get("model") not in MODELS:
        prefs["model"] = DEFAULT_PREFS["model"]
    if prefs.get("effort") not in EFFORTS:
        prefs["effort"] = DEFAULT_PREFS["effort"]
    return prefs


def save_prefs(prefs: dict) -> None:
    try:
        PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        PREFS_PATH.write_text(json.dumps(prefs, indent=2))
    except OSError:
        pass


def discover_sessions(limit: int = MAX_SESSIONS):
    """Recent Claude Code sessions, newest first.

    Returns [{id, title, cwd, mtime}]. Reads only the head of each file -
    aiTitle is the first line and cwd appears within the first few - so
    this stays fast even against multi-megabyte transcripts.
    """
    if not PROJECTS_DIR.is_dir():
        return []

    files = []
    for project in PROJECTS_DIR.iterdir():
        if not project.is_dir():
            continue
        for f in project.glob("*.jsonl"):
            try:
                files.append((f.stat().st_mtime, f))
            except OSError:
                continue
    files.sort(key=lambda t: t[0], reverse=True)

    sessions = []
    for mtime, path in files[:limit]:
        title, cwd = None, None
        try:
            with path.open("r", errors="replace") as fh:
                for line in islice(fh, 40):
                    if title and cwd:
                        break
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    title = title or d.get("aiTitle")
                    cwd = cwd or d.get("cwd")
        except OSError:
            continue

        sessions.append({
            "id": path.stem,
            "title": (title or path.stem[:8]),
            "cwd": cwd or str(Path.home()),
            "mtime": mtime,
        })
    return sessions


class VoiceModeIndicator(rumps.App):
    def __init__(self):
        super().__init__(IDLE_GLYPH, quit_button=None)

        self.prefs = load_prefs()

        self.state_item = rumps.MenuItem("Idle")
        self.last_item = rumps.MenuItem("Last activity: -")
        self.resume_menu = rumps.MenuItem("Resume session")
        self.model_menu = rumps.MenuItem("Model")
        self.effort_menu = rumps.MenuItem("Thinking effort")

        self._model_items = {}
        for name in MODELS:
            item = rumps.MenuItem(name.capitalize(), callback=self._set_model(name))
            self._model_items[name] = item
            self.model_menu.add(item)

        self._effort_items = {}
        for level in EFFORTS:
            item = rumps.MenuItem(level.capitalize(), callback=self._set_effort(level))
            self._effort_items[level] = item
            self.effort_menu.add(item)

        self.thinking_menu = rumps.MenuItem("Thinking")
        self._thinking_items = {
            True: rumps.MenuItem("On", callback=self._set_thinking(True)),
            False: rumps.MenuItem("Off (fastest)", callback=self._set_thinking(False)),
        }
        self.thinking_menu.add(self._thinking_items[True])
        self.thinking_menu.add(self._thinking_items[False])

        # Terminal launches are kept, but demoted: they lack the desktop
        # app's connectors.
        self.terminal_menu = rumps.MenuItem("Terminal session (no connectors)")
        self.terminal_menu.add(
            rumps.MenuItem("New session", callback=self.new_session))
        self.terminal_menu.add(
            rumps.MenuItem("Continue last session", callback=self.continue_session))

        # Service health. Without this a dead Whisper or Kokoro is invisible
        # until a voice turn fails, which reads as "voice is broken" rather
        # than "one service needs restarting".
        self.health_item = rumps.MenuItem("Services: checking…",
                                          callback=self.restart_services)
        self._health_checked_at = 0.0

        # Barge-in: cuts the utterance being spoken RIGHT NOW and advances to
        # the listen turn. voicemode already implements this end to end
        # (control_channel.py + streaming.py aborts PortAudio in ~85ms); it was
        # inert only because VOICEMODE_CONTROL_CHANNEL_ENABLED defaulted False.
        self.barge_item = rumps.MenuItem("Stop speaking (barge-in)",
                                         callback=self.barge_in)

        self.pause_item = rumps.MenuItem("Pause voice", callback=self.pause_voice)
        self.resume_here_item = rumps.MenuItem(
            "Resume voice here", callback=self.resume_here)

        # Model / effort live UNDER the Terminal submenu, because they only
        # work there.
        #
        # The desktop app launches Claude Code with explicit `--model` and
        # `--effort` flags taken from its own composer pickers, and a CLI flag
        # overrides settings.json. It stores those picker values in Electron
        # Local Storage / IndexedDB, which is not safely writable from here.
        # So there is NO way to set the model for an app session externally -
        # use the pickers at the bottom-right of the composer instead.
        self.terminal_menu.add(rumps.separator)
        self.terminal_menu.add(self.model_menu)
        self.terminal_menu.add(self.effort_menu)

        self.menu = [
            self.state_item,
            self.last_item,
            self.health_item,
            None,
            self.barge_item,
            self.pause_item,
            self.resume_here_item,
            None,
            rumps.MenuItem("New voice session", callback=self.app_session),
            self.resume_menu,
            self.terminal_menu,
            None,
            self.thinking_menu,
            None,
            rumps.MenuItem("Open event log", callback=self.open_log),
            rumps.MenuItem("Quit", callback=self.quit_app),
        ]
        self._sync_pref_checkmarks()

        self._anim_i = 0
        self._anim_state = None
        self._anim_last = 0.0
        self._fh = None
        self._path = None
        self._last_event_at = None
        self._sessions_built_at = 0.0

        self._open_today()
        if self._fh:
            self._fh.seek(0, os.SEEK_END)  # start from "now"

        # Reconcile on startup: push whatever the menu claims is selected into
        # settings.json. Without this the two can silently disagree - a choice
        # made on an older build (or lost to a concurrent write by the app)
        # keeps its checkmark while never actually applying to new sessions.
        self._apply_prefs_to_settings()

        self._sync_pause_items()
        self._rebuild_sessions()
        rumps.Timer(self.tick, POLL_SECONDS).start()

    def _apply_prefs_to_settings(self) -> bool:
        """Write the thinking toggle to settings.json.

        Only `alwaysThinkingEnabled` goes here. `model` and `effortLevel` are
        deliberately NOT written: the desktop app passes --model/--effort on
        the command line from its own pickers, and CLI flags beat the settings
        file - so writing them would have no effect on app sessions while
        looking like it did. Terminal launches get them as explicit flags
        instead (see _flags).
        """
        return patch_claude_settings(
            alwaysThinkingEnabled=self.prefs.get("thinking", True))

    # --- launching ----------------------------------------------------

    def _flags(self) -> str:
        """Model / effort flags for a launch, from the current preferences."""
        return f"--model {shq(self.prefs['model'])} --effort {shq(self.prefs['effort'])}"

    def _check_health(self):
        """Refresh the services line. STT may be the remote GPU box."""
        # Read from voicemode.env, not os.environ: launchd starts us with a
        # bare environment, so the STT chain would otherwise always look like
        # plain localhost and we would health-check the wrong host.
        raw = ""
        try:
            for line in (Path.home() / ".voicemode" / "voicemode.env").read_text().splitlines():
                line = line.strip()
                if line.startswith("VOICEMODE_STT_BASE_URLS="):
                    raw = line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            pass
        raw = raw or os.environ.get("VOICEMODE_STT_BASE_URLS", "http://127.0.0.1:2022/v1")
        stt_urls = [u.strip() for u in raw.split(",") if u.strip()]
        stt_url = stt_urls[0] if stt_urls else "http://127.0.0.1:2022/v1"

        # Whisper answers /health; Kokoro answers /v1/models.
        stt_ok = probe(stt_url.rstrip("/").removesuffix("/v1") + "/health")
        if not stt_ok:
            stt_ok = probe(stt_url.rstrip("/") + "/models")
        tts_ok = probe("http://127.0.0.1:8880/v1/models")

        # Do not hardcode a subnet - it silently rots when the LAN is
        # renumbered. "Remote" simply means "not loopback".
        remote = not any(h in stt_url for h in ("127.0.0.1", "localhost", "::1"))
        stt_label = "STT" + (" (remote)" if remote else "")

        broken = []
        if not stt_ok:
            broken.append(stt_label)
        if not tts_ok:
            broken.append("TTS")

        if not broken:
            self.health_item.title = "Services: OK"
        else:
            self.health_item.title = f"Services: {', '.join(broken)} DOWN, click to restart"

        # ALERT on the transition, not on every poll. Displaying a state is
        # not the same as telling you about it: whisper was dead for two days
        # (17-19 Aug) and nothing said so. Notify when it breaks, and again
        # when it comes back, so a silent degradation is impossible.
        prev = getattr(self, "_prev_broken", None)
        now_key = ",".join(sorted(broken))
        if prev is not None and now_key != prev:
            if broken:
                detail = f"{', '.join(broken)} not responding."
                if any("remote" in b for b in broken):
                    detail += " Dictation has fallen back to local medium."
                rumps.notification("VoiceMode", "Voice service DOWN", detail)
            else:
                rumps.notification("VoiceMode", "Voice services recovered",
                                   "All endpoints responding again.")
        self._prev_broken = now_key

    def restart_services(self, _):
        """Restart the local services. The remote GPU box is not ours to
        restart from here - that needs the whisper-admin MCP tools."""
        for label in ("whisper", "kokoro"):
            subprocess.Popen(
                [str(Path.home() / ".local/bin/voicemode"), "service", "restart", label],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        rumps.notification(
            "VoiceMode", "Restarting local services",
            "Whisper and Kokoro restarting. A remote STT host must be "
            "restarted on that machine.")

    def barge_in(self, _):
        """Cut the current utterance immediately."""
        try:
            res = subprocess.run(
                [str(Path.home() / ".local/bin/voicemode"), "control", "skip-forward"],
                capture_output=True, text=True, timeout=8)
        except Exception as e:
            rumps.notification("VoiceMode", "Barge-in failed", str(e)[:160])
            return
        if res.returncode != 0:
            err = (res.stderr or res.stdout or "").strip()[:170]
            # The usual cause is the control socket not being bound, i.e. the
            # MCP server started before VOICEMODE_CONTROL_CHANNEL_ENABLED=true
            # was set. The env is only read at server start.
            if "socket" in err.lower() or "connect" in err.lower() or not err:
                err = ("Control socket not listening. Restart Claude Code so "
                       "the voice server picks up the control-channel setting.")
            rumps.notification("VoiceMode", "Barge-in failed", err)

    def pause_voice(self, _):
        """Ask the agent to stop looping after the current turn."""
        try:
            PAUSE_FLAG.parent.mkdir(parents=True, exist_ok=True)
            PAUSE_FLAG.write_text(datetime.now().isoformat())
        except OSError:
            return
        self._sync_pause_items()
        rumps.notification(
            "VoiceMode", "Paused",
            "Stops after the current turn. Session stays open - "
            "use Resume voice here to pick it back up.")

    def resume_here(self, _):
        """Resume voice in the session that is already open."""
        PAUSE_FLAG.unlink(missing_ok=True)
        self._sync_pause_items()
        type_into_claude(VOICE_PROMPT)

    def _sync_pause_items(self):
        paused = PAUSE_FLAG.exists()
        self.pause_item.title = "Paused (voice will stop)" if paused else "Pause voice"
        self.pause_item.state = 1 if paused else 0

    def app_session(self, _):
        """Primary path: new Claude Code tab in the desktop app."""
        PAUSE_FLAG.unlink(missing_ok=True)
        self._sync_pause_items()
        app_new_session(VOICE_PROMPT)

    def new_session(self, _):
        run_in_terminal(
            f"cd {shq(Path.home())} && claude {self._flags()} {shq(VOICE_PROMPT)}"
        )

    def continue_session(self, _):
        run_in_terminal(
            f"cd {shq(Path.home())} && claude -c {self._flags()} {shq(VOICE_PROMPT)}"
        )

    def _resume(self, session):
        def callback(_):
            run_in_terminal(
                f"cd {shq(session['cwd'])} && "
                f"claude --resume {shq(session['id'])} {self._flags()} "
                f"{shq(VOICE_PROMPT)}"
            )
        return callback

    # --- preferences --------------------------------------------------

    # Every setter writes the WHOLE selection, not just the key that changed.
    # A partial write leaves the other keys at whatever settings.json happens
    # to hold, which is how model and thinking drifted apart before.

    def _set_model(self, name):
        def callback(_):
            self.prefs["model"] = name
            save_prefs(self.prefs)
            self._commit()
        return callback

    def _set_effort(self, level):
        def callback(_):
            self.prefs["effort"] = level
            # Selecting an effort implies thinking is wanted, so re-enable it.
            self.prefs["thinking"] = True
            save_prefs(self.prefs)
            self._commit()
        return callback

    def _set_thinking(self, enabled):
        def callback(_):
            self.prefs["thinking"] = enabled
            save_prefs(self.prefs)
            self._commit()
        return callback

    def _commit(self):
        """Persist the selection and confirm it actually landed on disk."""
        ok = self._apply_prefs_to_settings()
        self._sync_pref_checkmarks()
        if not ok:
            rumps.notification(
                "VoiceMode", "Could not write settings",
                "~/.claude/settings.json was unreadable or malformed. "
                "Selection saved locally but will NOT apply to new sessions.")

    def _sync_pref_checkmarks(self):
        for name, item in self._model_items.items():
            item.state = 1 if name == self.prefs["model"] else 0
        for level, item in self._effort_items.items():
            item.state = 1 if level == self.prefs["effort"] else 0
        thinking = self.prefs.get("thinking", True)
        for enabled, item in self._thinking_items.items():
            item.state = 1 if enabled == thinking else 0

        self.model_menu.title = f"Model: {self.prefs['model']}"
        self.effort_menu.title = (
            f"Effort: {self.prefs['effort']}" if thinking else "Effort: (thinking off)"
        )
        self.thinking_menu.title = f"Thinking: {'on' if thinking else 'off'}"

    def _rebuild_sessions(self):
        # rumps only creates the submenu's NSMenu once it has an item, so
        # clear() raises AttributeError on the very first build.
        try:
            self.resume_menu.clear()
        except AttributeError:
            pass
        sessions = discover_sessions()
        if not sessions:
            self.resume_menu.add(rumps.MenuItem("No sessions found"))
            return
        for s in sessions:
            when = datetime.fromtimestamp(s["mtime"]).strftime("%d %b %H:%M")
            title = s["title"]
            if len(title) > 44:
                title = title[:43] + "…"
            self.resume_menu.add(
                rumps.MenuItem(f"{title}  ({when})", callback=self._resume(s))
            )

    # --- log tailing --------------------------------------------------

    def _today_path(self) -> Path:
        return EVENTS_DIR / f"voicemode_events_{date.today().isoformat()}.jsonl"

    def _open_today(self):
        """(Re)open today's log. Handles midnight rollover, and the log not
        existing yet - VoiceMode creates it on first use."""
        path = self._today_path()
        if path == self._path and self._fh:
            return
        if self._fh:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
        if path.exists():
            try:
                self._fh = path.open("r", errors="replace")
                self._path = path
            except OSError:
                self._fh = None

    def tick(self, _):
        if self._path != self._today_path() or self._fh is None:
            self._open_today()

        if self._fh is not None:
            glyph = label = None
            for line in self._fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                hit = STATES.get(event.get("event_type"))
                if hit:
                    glyph, label = hit
                    self._last_event_at = event.get("timestamp", "")

            if glyph:
                # Remember the logical state; the title itself is now driven by
                # the animator below so it can cycle between events.
                self._anim_state = label if label in ANIM_FRAMES else None
                self._anim_i = 0
                if self._anim_state is None:
                    self.title = glyph
                self.state_item.title = label
                self.last_item.title = f"Last activity: {self._fmt(self._last_event_at)}"

        now = datetime.now().timestamp()

        # Advance the animation. Driven off the same 0.25s tick, stepping only
        # when ANIM_INTERVAL has elapsed so the rate is independent of poll rate.
        if self._anim_state and now - self._anim_last >= ANIM_INTERVAL:
            self._anim_last = now
            frames = ANIM_FRAMES[self._anim_state]
            self.title = frames[self._anim_i % len(frames)]
            self._anim_i += 1

        # Refresh the session list occasionally so new conversations show up.
        if now - self._sessions_built_at > 60:
            self._sessions_built_at = now
            try:
                self._rebuild_sessions()
            except Exception:
                pass

        # Health check on a slower cadence - it makes network calls.
        if now - self._health_checked_at > 30:
            self._health_checked_at = now
            try:
                self._check_health()
            except Exception:
                self.health_item.title = "Services: check failed"

    @staticmethod
    def _fmt(ts: str) -> str:
        if not ts:
            return "-"
        try:
            return datetime.fromisoformat(ts.replace("Z", "")).strftime("%H:%M:%S")
        except ValueError:
            return ts[:19] or "-"

    # --- misc ---------------------------------------------------------

    def open_log(self, _):
        path = self._today_path()
        if path.exists():
            subprocess.Popen(["open", "-R", str(path)])
        else:
            rumps.notification("VoiceMode", "No event log yet",
                               "It appears the first time you use voice mode.")

    def quit_app(self, _):
        rumps.quit_application()


if __name__ == "__main__":
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    VoiceModeIndicator().run()
