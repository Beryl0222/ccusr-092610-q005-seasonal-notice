"""JSONL 事件日志：每条领域事件一行，追加写入并落盘。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class Journal:
    """以 JSONL 形式持久化领域事件，重启后按顺序重放恢复状态。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"事件日志第 {line_number} 行损坏: {exc}"
                    ) from exc
                if not isinstance(event, dict):
                    raise ValueError(f"事件日志第 {line_number} 行不是 JSON 对象")
                events.append(event)
        return events

    def append(self, event: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
