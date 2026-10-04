"""The audit log: one line per safety decision, so a reviewer can see what was allowed and why."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("bag.safety")


class AuditLog:
    def __init__(self, path, redactor):
        self.path = Path(path) if path else None  # no path: log to the logger only
        self.redactor = redactor

    def write(self, record: dict) -> None:
        """Append one decision. Everything passes through redaction before it is written."""
        record = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), **record}
        line = json.dumps(self.redactor.data(record), ensure_ascii=False)
        logger.info(line)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as file:
                file.write(line + "\n")
