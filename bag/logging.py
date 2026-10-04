import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from bag.safety import Redactor

DEFAULT_EVIDENCE_DIR = Path("evidence")
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class RunLogError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_run_id(kind: str, label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return "-".join(part for part in (stamp, kind, slug) if part)


class RunLogger:
    def __init__(self, kind, label, evidence_dir=DEFAULT_EVIDENCE_DIR, run_id=None, redactor=None):
        self.kind = kind
        self.label = label
        self.redactor = redactor or Redactor()
        self.run_id = run_id or make_run_id(kind, label)
        if not RUN_ID.fullmatch(self.run_id) or ".." in self.run_id:
            raise RunLogError(f"'{self.run_id}' is not a usable run id: use letters, digits, '.', '_' and '-'.")
        if run_id is None:
            first, counter = self.run_id, 1
            while (Path(evidence_dir) / self.run_id).exists():
                counter += 1
                self.run_id = f"{first}-{counter}"
        self.directory = Path(evidence_dir) / self.run_id
        if self.directory.exists() and any(self.directory.iterdir()):
            raise RunLogError(f"{self.directory} already has files. Pick another run id, or remove that folder.")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.steps_path = self.directory / "steps.jsonl"
        self.result_path = self.directory / "result.json"
        self.screenshots_dir = self.directory / "screenshots"
        self.started_at = _now()
        self._clock_start = time.monotonic()
        self.lines = 0
        self.steps_path.touch()

    def step(self, source: str, event: str, step: int | None = None, **details) -> None:
        self.lines += 1
        record = {"seq": self.lines, "time": _now(), "source": source, "event": event, "step": step, **details}
        line = json.dumps(self.redactor.data(record), ensure_ascii=False, default=str)
        with self.steps_path.open("a", encoding="utf-8") as file:
            file.write(line + "\n")

    def screenshot(self, name: str, png: bytes) -> Path:
        self.screenshots_dir.mkdir(exist_ok=True)
        base = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "screenshot"
        path, counter = self.screenshots_dir / f"{base}.png", 1
        while path.exists():
            counter += 1
            path = self.screenshots_dir / f"{base}-{counter}.png"
        path.write_bytes(png)
        return path

    def finish(self, status: str, **summary) -> Path:
        shots = sorted(p.name for p in self.screenshots_dir.glob("*.png")) if self.screenshots_dir.exists() else []
        result = {
            "run_id": self.run_id,
            "kind": self.kind,
            "label": self.label,
            "status": status,
            "started_at": self.started_at,
            "finished_at": _now(),
            "seconds": round(time.monotonic() - self._clock_start, 2),
            "lines_logged": self.lines,
            "screenshots": shots,
            **summary,
        }
        text = json.dumps(self.redactor.data(result), indent=2, ensure_ascii=False, default=str)
        temp = self.result_path.with_suffix(".tmp")
        temp.write_text(text, encoding="utf-8")
        temp.replace(self.result_path)
        return self.result_path
