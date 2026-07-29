"""A terminal REPL over the same command set Telegram uses.

Exists so the whole interactive surface can be exercised on a machine with no
bot token and no chat — which is how it gets tested, and how you check a change
before it reaches your phone.
"""

from __future__ import annotations

import sys

from .commands import Commands

BANNER = """stock-movement-monitor console
Same commands as the Telegram bot. /help for the list, /quit to leave.
"""


def repl(commands: Commands, stream=None, out=None) -> int:
    stream = stream or sys.stdin
    out = out or sys.stdout
    print(BANNER, file=out)

    while True:
        try:
            print("> ", end="", flush=True, file=out)
            line = stream.readline()
        except KeyboardInterrupt:
            print(file=out)
            return 0
        if not line:                     # EOF
            print(file=out)
            return 0
        line = line.strip()
        if not line:
            continue
        if line.lower() in ("/quit", "/exit", "quit", "exit"):
            return 0
        reply = commands.dispatch(line)
        if reply.text:
            print(reply.text, file=out)
        print(file=out)
