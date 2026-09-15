#!/usr/bin/env python3
"""Build a properly formatted HTML email body, with a compact fallback signature.

The lenient sibling of send_mail.py: sending through Graph out of hours is
fine, but the mail must look right, real paragraph breaks and the signature
in place; a plain-text body with no spacing looks bad in Outlook. This one
never refuses: if the stored template (~/.voicemode/templates/signature.html,
private, not in the repo) is missing it falls back to a plain block built
from MAIL_SIG_NAME / MAIL_SIG_TITLE / MAIL_SIG_PHONE / MAIL_SIG_EMAIL /
MAIL_SIG_WEB. Prefer send_mail.build_body for anything client-facing; it
refuses instead of degrading.

Usage:
    from mail_html import build
    html = build("Hi Jane,\\n\\nFirst paragraph.\\n\\nSecond paragraph.\\n\\nBest regards,")
Paragraphs are split on blank lines; single newlines become <br>.
"""
import os
from pathlib import Path
import html as _html

SIG = Path(os.environ.get("MAIL_SIGNATURE_FILE", Path.home() / ".voicemode" / "templates" / "signature.html"))
FONT = ("font-family:Aptos,Calibri,Arial,sans-serif; font-size:11pt; "
        "color:rgb(0,0,0);")


def _fallback_signature() -> str:
    lines = [f"<b>{_html.escape(os.environ.get('MAIL_SIG_NAME', ''))}</b>"]
    for key in ("MAIL_SIG_TITLE", "MAIL_SIG_PHONE", "MAIL_SIG_EMAIL", "MAIL_SIG_WEB"):
        v = os.environ.get(key, "")
        if v:
            lines.append(_html.escape(v))
    return f'<p style="margin:0; {FONT}">' + "<br>".join(lines) + "</p>"


def build(text: str, signature: bool = True) -> str:
    paras = [p for p in text.replace("\r\n", "\n").split("\n\n")]
    body = []
    for p in paras:
        if not p.strip():
            continue
        esc = _html.escape(p.strip()).replace("\n", "<br>")
        body.append(f'<p style="margin:0 0 12pt 0; {FONT}">{esc}</p>')
    out = f'<div style="{FONT}">' + "".join(body)
    if signature:
        try:
            out += '<div style="margin-top:6pt">' + SIG.read_text() + "</div>"
        except OSError:
            out += _fallback_signature()
    return out + "</div>"


if __name__ == "__main__":
    import sys
    print(build(sys.stdin.read()))
