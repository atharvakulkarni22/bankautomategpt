"""The recorder: saves every step of a discovery run to a JSON file.

The file is rewritten after each step, so a crash or a stop still leaves a
usable recording. It holds placeholders only, never real inputs or secrets.
Later steps turn a recording into a reusable artifact.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from bag.safety import Redactor

DEFAULT_DIR = Path("evidence/recordings")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Recorder:
    def __init__(self, goal, input_names, start_url, model, directory=DEFAULT_DIR, redactor=None):
        # Everything written to the file goes through the redactor: secrets removed, account
        # numbers masked. A recording must never become a second copy of customer data.
        self.redactor = redactor or Redactor()
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", goal.lower()).strip("-")[:40] or "run"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.path = directory / f"{stamp}-{slug}.json"
        self.data = {
            "version": 1,
            "goal": self.redactor.text(goal),
            "inputs": sorted(input_names),  # names only, never values
            "start_url": self.redactor.text(start_url),
            "model": model,
            "started_at": _now(),
            "finished_at": None,
            "stop_reason": None,
            "message": "",
            "outputs": {},
            "steps": [],
        }
        self._save()

    def add_step(self, index, url, reason, status, result, action=None, candidates=(), raw=None, human_events=None, repaired=None):
        """Record one step. `action` is None when the model's output was unusable (then `raw` is kept)."""
        step = {
            "index": index,
            "time": _now(),
            "url": url,
            "reason": reason,
            "action": action.model_dump(exclude_none=True, exclude_defaults=True) if action else None,
            "target_candidates": [c.model_dump(exclude_none=True, exclude_defaults=True) for c in candidates],
            "status": status,  # ok | error | blocked | invalid | human
            "result": result,
        }
        if raw is not None:
            step["raw"] = raw
        if repaired is not None:
            step["repaired"] = repaired
        if human_events is not None:  # a human took over at this step: what they clicked and typed
            step["human_events"] = human_events
        self.data["steps"].append(self.redactor.data(step))
        self._save()

    def finish(self, stop_reason, outputs, message=""):
        self.data.update(finished_at=_now(), stop_reason=stop_reason, outputs=self.redactor.data(dict(outputs)),
                         message=self.redactor.text(message))
        self._save()

    def _save(self):
        # Write to a temp file, then swap it in, so a crash never leaves half a file.
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        temp.replace(self.path)
