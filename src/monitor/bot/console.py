"""A local console standing in for the Telegram bot.

Same router, same handlers, same replies — only the transport differs. So you
can exercise the whole control surface, including config edits that really
persist, before creating a bot token. HTML markup is rendered down to terminal
text; buttons become numbered choices you can pick by typing the number.

    monitor console
    monitor console --script "list; add PLTR; levels"
"""

from __future__ import annotations

import html
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .commands import BotContext, Button, CommandRouter, Reply

BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RESET = "\033[0m"

TAG = re.compile(r"<[^>]+>")


@dataclass
class Console:
    router: CommandRouter
    colour: bool = True

    def _c(self, code: str, text: str) -> str:
        return f"{code}{text}{RESET}" if self.colour else text

    def render(self, reply: Reply) -> str:
        out = [self._to_terminal(reply.text)]
        if reply.files:
            out.append("")
            for path in reply.files:
                exists = Path(path).exists()
                size = f" ({Path(path).stat().st_size:,} bytes)" if exists else " (missing)"
                out.append(self._c(CYAN, f"  📎 attachment: {path}{size}"))
                out.append(
                    self._c(DIM, "     on Telegram this arrives as a file you can open")
                )
        if reply.buttons:
            out.append("")
            out.append(self._c(DIM, "  buttons (type the number, or the command):"))
            for i, button in enumerate(reply.buttons, 1):
                out.append(
                    f"   {self._c(YELLOW, f'[{i}]')} {button.label}"
                    f"  {self._c(DIM, f'→ /{button.command}')}"
                )
        return "\n".join(out)

    def _to_terminal(self, text: str) -> str:
        """Turn the Telegram-flavoured HTML into something readable in a shell."""
        text = re.sub(r"<b>(.*?)</b>", lambda m: self._c(BOLD, m.group(1)), text, flags=re.S)
        text = re.sub(r"<i>(.*?)</i>", lambda m: self._c(DIM, m.group(1)), text, flags=re.S)
        text = re.sub(r"<code>(.*?)</code>", lambda m: self._c(CYAN, m.group(1)), text, flags=re.S)
        text = re.sub(r'<a href="([^"]+)">(.*?)</a>', r"\2 (\1)", text, flags=re.S)
        text = TAG.sub("", text)
        return html.unescape(text)

    def run_line(self, line: str, last: Reply | None) -> Reply:
        """Resolve a numeric button choice against the previous reply."""
        stripped = line.strip()
        if last and stripped.isdigit():
            index = int(stripped) - 1
            if 0 <= index < len(last.buttons):
                chosen = last.buttons[index]
                print(self._c(DIM, f"  → /{chosen.command}"))
                return self.router.handle(chosen.command)
        return self.router.handle(line)


def run_console(ctx: BotContext, script: str | None = None, colour: bool = True) -> int:
    console = Console(router=CommandRouter(ctx), colour=colour)

    banner = [
        console._c(BOLD, "stock-movement-monitor console"),
        console._c(
            DIM,
            "The Telegram bot's command surface, locally. Config changes are real "
            "and persist to the runtime overlay.",
        ),
        console._c(DIM, "Type a command, a button number, or 'quit'. /help for the list."),
        "",
    ]
    print("\n".join(banner))

    last: Reply | None = None

    if script:
        for line in [p.strip() for p in script.split(";") if p.strip()]:
            print(f"{console._c(YELLOW, '>')} {line}")
            last = console.run_line(line, last)
            print(console.render(last))
            print()
        return 0

    while True:
        try:
            line = input(console._c(YELLOW, "> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if line.lower() in {"quit", "exit", "q"}:
            return 0
        if not line:
            continue
        last = console.run_line(line, last)
        print(console.render(last))
        print()


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin wrapper
    from ..cli import main as cli_main

    return cli_main(["console", *(argv or sys.argv[1:])])
