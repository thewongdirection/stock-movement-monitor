"""The interactive control surface.

`commands.py` holds every command's logic once. The console REPL and the
Telegram bot are both thin shells over it, so what you test locally is
byte-for-byte the behaviour you get on your phone — the only difference is where
the text comes from and where the reply goes.
"""

from .commands import CommandRouter, Reply  # noqa: F401
