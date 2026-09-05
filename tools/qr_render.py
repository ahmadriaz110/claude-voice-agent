#!/usr/bin/env python3
"""Turn a terminal half-block QR (as printed by the WhatsApp MCP) into a crisp
SVG a phone can scan from the screen. Reads the ANSI text on stdin, writes
the SVG path given as argv[1]."""
import re, sys
ANSI = re.compile(r"\x1b?\[[0-9;]*m")
CELL = {"█": (1, 1), "▀": (1, 0), "▄": (0, 1), " ": (0, 0)}
rows = []
for line in sys.stdin.read().splitlines():
    t = ANSI.sub("", line)
    if not t.strip() or not any(c in "█▀▄" for c in t):
        continue
    top, bot = [], []
    for ch in t:
        u, d = CELL.get(ch, (0, 0))
        top.append(u); bot.append(d)
    rows.append(top); rows.append(bot)
w = max(len(r) for r in rows); h = len(rows)
M, Q = 10, 4
W, Hh = (w + 2 * Q) * M, (h + 2 * Q) * M
out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{Hh}" viewBox="0 0 {W} {Hh}" shape-rendering="crispEdges">',
       f'<rect width="{W}" height="{Hh}" fill="#fff"/>']
for y, r in enumerate(rows):
    for x, v in enumerate(r):
        if v:
            out.append(f'<rect x="{(x+Q)*M}" y="{(y+Q)*M}" width="{M}" height="{M}" fill="#000"/>')
out.append("</svg>")
open(sys.argv[1], "w").write("\n".join(out))
print(f"svg {w}x{h} modules -> {sys.argv[1]}")
