#!/usr/bin/env python3
"""Turn a screenplay into an asciicast, for `agg` to render as a GIF.

    python assets/make_cast.py <screenplay> <cast> [cols] [rows]

One directory per demo under assets/ holds its screenplay, cast and gif; this renderer is shared by
all of them. See assets/README.md for the full pipeline.

Why a written session rather than a recording. The demo has to show two things side by side: what
Backlot returns, which is real, and what the same call against real Slack returns, which cannot be
real here — an authenticated call would need a workspace token in a public asset, and the values
would differ anyway, because real Slack answers about a real workspace and Backlot answers about
your corpus. What matches is the schema.

A session recorder cannot do that. vhs was tried: it can print anything, but it cannot hide the
command that produces it, because `Hide` stops frame capture while the typed line stays in the
terminal and returns on `Show`. So the file-swap needed to stage the second half showed up on
camera. Writing the session instead makes the seam explicit rather than hidden: the screenplay is
committed, and anyone can read which lines were executed and which were authored — something a
recording cannot show you.

Directives, one per line:

    !  <cmd>   run it, print the command as typed, splice its real stdout
    !! <cmd>   run it, print nothing (setup: start a server, clean up)
    E  K=V     set an environment variable for every command after it, off camera — so a
               command can be shown as a reader would type it, without a setup prefix
    C          clear the screen, as `clear` would — so the last beat can hold a screen of its own
    $  <cmd>   print the command as typed, do not run it
    #  <cmd>   same, dimmed, rendered as a shell comment — a call you would make, not one made here
    >  <text>  narration, cyan
    |  <text>  a line of authored output
    J  <file>  splice a JSON file the way `jq` prints it, coloured
    J  <<TAG   the same, for JSON written inline: every line until a line reading TAG
    ~  <secs>  pause
    //         a comment in the screenplay itself; never rendered
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PROMPT = "\033[38;5;114m$\033[0m "
CYAN = "\033[1;36m"
DIM = "\033[38;5;245m"
KEY = "\033[34;1m"
STR = "\033[32m"
OFF = "\033[0m"

CPS = 0.05  # seconds per typed character
LINE_PAUSE = 0.07  # between output lines
AFTER_ENTER = 0.32


def _scalar(v) -> str:
    if isinstance(v, str):
        return f'{STR}"{v}"{OFF}'
    return {True: "true", False: "false", None: "null"}.get(v, json.dumps(v))


def colour_json(value, indent: int = 0) -> list[str]:
    """Render `value` the way `jq` does on a terminal: keys bold blue, strings green, rest plain."""
    pad = "  " * indent
    if isinstance(value, dict):
        if not value:
            return [pad + "{}"]
        out = [pad + "{"]
        for i, (k, v) in enumerate(items := list(value.items())):
            comma = "," if i < len(items) - 1 else ""
            head = f'{pad}  {KEY}"{k}"{OFF}: '
            if isinstance(v, (dict, list)):
                nested = colour_json(v, indent + 1)
                out += [head + nested[0].lstrip(), *nested[1:-1], nested[-1] + comma]
            else:
                out.append(head + _scalar(v) + comma)
        return out + [pad + "}"]
    if isinstance(value, list):
        if not value:
            return [pad + "[]"]
        out = [pad + "["]
        for i, v in enumerate(value):
            comma = "," if i < len(value) - 1 else ""
            if isinstance(v, (dict, list)):
                nested = colour_json(v, indent + 1)
                out += [*nested[:-1], nested[-1] + comma]
            else:
                out.append("  " * (indent + 1) + _scalar(v) + comma)
        return out + [pad + "]"]
    return [pad + _scalar(value)]


def build(screenplay: Path, width: int, height: int) -> str:
    events: list[list] = []
    t = 0.0

    def emit(text: str) -> None:
        events.append([round(t, 3), "o", text])

    def out_line(line: str) -> None:
        nonlocal t
        emit(line + "\r\n")
        t += LINE_PAUSE

    def typed(cmd: str, dim: bool = False) -> None:
        nonlocal t
        emit(PROMPT + (DIM if dim else ""))
        for ch in cmd:
            emit(ch)
            t += CPS
        emit((OFF if dim else "") + "\r\n")
        t += AFTER_ENTER

    def run(cmd: str) -> str:
        done = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if done.returncode != 0 and not done.stdout:
            raise SystemExit(f"screenplay command failed: {cmd}\n{done.stderr}")
        return done.stdout

    lines = screenplay.read_text().splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        i += 1
        if raw.startswith("//"):
            continue
        if not raw.strip():
            emit("\r\n")
            t += LINE_PAUSE
            continue
        kind, _, rest = raw.partition(" ")
        rest = rest.strip()
        if kind == "E":
            key, _, value = rest.partition("=")
            os.environ[key.strip()] = value.strip()
        elif kind == "!!":
            run(rest)
        elif kind == "!":
            typed(rest)
            for line in run(rest).splitlines():
                out_line(line)
        elif kind == "$":
            typed(rest)
        elif kind == "#":
            # The marker is the directive AND part of what is rendered: these lines have to read as
            # shell comments, or a dim command is just a command.
            typed("# " + rest, dim=True)
        elif kind == ">":
            emit(f"\r\n{CYAN}> {rest}{OFF}\r\n")
            t += LINE_PAUSE
        elif kind == "|":
            out_line(rest)
        elif kind == "J":
            if rest.startswith("<<"):
                tag, body = rest[2:].strip(), []
                while i < len(lines) and lines[i].strip() != tag:
                    body.append(lines[i])
                    i += 1
                i += 1  # step over the closing tag
                payload = json.loads("\n".join(body))
            else:
                payload = json.loads(Path(rest).read_text())
            for line in colour_json(payload):
                out_line(line)
        elif kind == "~":
            t += float(rest)
        elif kind == "C":
            emit("\033[2J\033[H")
            t += LINE_PAUSE
        else:
            raise SystemExit(f"unknown directive on line: {raw!r}")

    emit(PROMPT)
    header = {"version": 2, "width": width, "height": height, "env": {"TERM": "xterm-256color"}}
    return "\n".join([json.dumps(header), *(json.dumps(e) for e in events)]) + "\n"


if __name__ == "__main__":
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    cols = int(sys.argv[3]) if len(sys.argv) > 3 else 92
    rows = int(sys.argv[4]) if len(sys.argv) > 4 else 26
    dst.write_text(build(src, cols, rows))
    print(f"wrote {dst} — {cols}x{rows}")
