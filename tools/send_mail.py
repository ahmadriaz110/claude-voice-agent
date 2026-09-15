#!/usr/bin/env python3
"""Build an HTML mail body with your FULL stored signature, every time.

Why: a mail once went out with a hand-written compact signature because the
agent typed the signature into the body itself. Never hand-write a signature
again: always build the body from the stored template, and refuse to build
one at all if the template is missing or looks wrong (a wrong signature on a
client mail is worse than no mail).

The template is a private file, not part of this repo:
    ~/.voicemode/templates/signature.html
Save it from a mail you sent from Outlook (the signature block's HTML, images
as data: URIs or hosted). MAIL_SIGNATURE_MARKER is a string that must appear
in the template for it to be accepted (your surname in capitals, a company
name, the signature tool's name), so a truncated or replaced file is caught.

Usage:
    from send_mail import build_body
    html = build_body("Hi Jane,\\n\\nFirst para.\\n\\nSecond para.\\n\\nThank you.")
then pass html to your Graph / Outlook send with body_type html. Paragraphs
are split on blank lines; single newlines become <br>. Run as a script it
reads the text on stdin and writes /tmp/mail_body.html.
"""
import os
from pathlib import Path
import html as _html

SIG = Path(os.environ.get("MAIL_SIGNATURE_FILE", Path.home() / ".voicemode" / "templates" / "signature.html"))
MARKER = os.environ.get("MAIL_SIGNATURE_MARKER", "")
FONT = "font-family:Aptos,Calibri,Arial,sans-serif; font-size:11pt; color:rgb(0,0,0);"


def build_body(text: str) -> str:
    sig = SIG.read_text()            # deliberately NOT wrapped in try/except:
    if MARKER and MARKER.lower() not in sig.lower():
        raise SystemExit("signature template looks wrong, refusing to build the mail")
    paras = []
    for p in text.replace("\r\n", "\n").split("\n\n"):
        if p.strip():
            esc = _html.escape(p.strip()).replace("\n", "<br>")
            paras.append(f'<p style="margin:0 0 12pt 0; {FONT}">{esc}</p>')
    return (f'<div style="{FONT}">' + "".join(paras)
            + '<div style="margin-top:6pt">' + sig + "</div></div>")


if __name__ == "__main__":
    import sys
    body = build_body(sys.stdin.read())
    out = Path("/tmp/mail_body.html"); out.write_text(body)
    print(out, len(body), "chars")
