"""How a paused run hears that the human is done.

Two ways, and the waiting run watches both at once:

    1. The person presses Enter in the terminal where the run is paused.
    2. Someone runs `bag resume` (in a second terminal, or from a script). That drops a small
       flag file, <id>.resume, next to the intervention file, and the waiting run picks it up.

Reading the keyboard must not block, because the run also has to keep checking for the flag
file (and keep the browser responsive). So there is no input() here. Instead the keyboard is
polled: "has a key been pressed?", cheap and non-blocking.
"""

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

DEFAULT_DIR = Path("evidence/interventions")
ABORT_WORDS = {"q", "quit", "abort", "stop"}  # typed instead of just pressing Enter: give up on this run


class LineReader:
    """Collects typed characters and hands back a whole line once Enter is pressed.

    `ready()` says whether a key is waiting; `read_char()` takes one. Both are passed in, so the
    same logic works on Windows and elsewhere, and can be tested without a keyboard.
    """

    def __init__(self, ready: Callable[[], bool], read_char: Callable[[], str]):
        self._ready, self._read_char = ready, read_char
        self._buffer: list[str] = []

    def poll(self) -> str | None:
        """The line that was just completed, or None if Enter has not been pressed yet."""
        while self._ready():
            char = self._read_char()
            if char in ("\r", "\n"):
                line, self._buffer = "".join(self._buffer), []
                return line
            if char == "\x03":  # Ctrl+C arrives as a character when the terminal is polled like this
                raise KeyboardInterrupt
            if char in ("\b", "\x7f"):
                if self._buffer:
                    self._buffer.pop()
            else:
                self._buffer.append(char)
        return None


def make_enter_checker(stdin=None) -> Callable[[], str | None]:
    """A function that returns the typed line once Enter is pressed, else None. Never blocks.

    With no terminal (a script, a test, a pipe) it always returns None: only the resume flag
    file can then end the pause.
    """
    stdin = stdin or sys.stdin
    if stdin is None or not stdin.isatty():
        return lambda: None
    if os.name == "nt":
        import msvcrt

        return LineReader(msvcrt.kbhit, msvcrt.getwch).poll

    import select

    def ready() -> bool:
        return bool(select.select([stdin], [], [], 0)[0])

    return LineReader(ready, lambda: stdin.read(1)).poll


# ------------------------------------------------------------ the flag file


def flag_path(directory, intervention_id: str) -> Path:
    return Path(directory) / f"{intervention_id}.resume"


def request_resume(directory, intervention_id: str) -> Path:
    """What `bag resume` does: drop the flag file that the waiting run is watching for."""
    path = flag_path(directory, intervention_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("resume\n", encoding="utf-8")
    return path


def _intervention_files(directory) -> list[Path]:
    # <id>.json are interventions; <id>.human-events.json hold what the human did.
    return sorted(p for p in Path(directory).glob("*.json") if not p.name.endswith(".human-events.json"))


def list_interventions(directory) -> list[dict]:
    """Every intervention in the folder (unreadable files are skipped), oldest first."""
    found = []
    for path in _intervention_files(directory):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and "id" in data and "status" in data:
            found.append(data)
    return found


def find_intervention(directory, ref: str | None) -> dict:
    """Pick the intervention `bag resume` is about. Raises LookupError with a message to show the person.

    With no `ref`: the one that is waiting (it is an error if none or several are).
    With a `ref`: an exact id, or a prefix that matches exactly one.
    """
    everything = list_interventions(directory)
    if ref is None:
        waiting = [i for i in everything if i["status"] == "waiting"]
        if not waiting:
            raise LookupError("Nothing is waiting for a human right now.")
        if len(waiting) > 1:
            raise LookupError("More than one run is waiting. Say which: " + ", ".join(i["id"] for i in waiting))
        return waiting[0]
    matches = [i for i in everything if i["id"] == ref] or [i for i in everything if i["id"].startswith(ref)]
    if not matches:
        raise LookupError(f"No intervention '{ref}' in {directory}.")
    if len(matches) > 1:
        raise LookupError(f"'{ref}' matches several interventions: " + ", ".join(i["id"] for i in matches))
    if matches[0]["status"] != "waiting":
        raise LookupError(f"{matches[0]['id']} is not waiting (its status is '{matches[0]['status']}').")
    return matches[0]
