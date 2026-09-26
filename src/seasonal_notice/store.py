"""事件存储：契约校验、按 ``event_id`` 幂等去重、JSONL 追加与重放。

存储只负责落盘与去重，不做业务状态推进；服务层通过重放事件重建状态。
写入采用"校验 -> 查重 -> 追加单行 JSON -> flush"顺序，进程重启后
可以完整重放。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Mapping

from .contracts import ContractIssue, validate_event


class ContractViolation(ValueError):
    """事件不满足领域契约。"""

    def __init__(self, issues: list[ContractIssue]) -> None:
        self.issues = issues
        super().__init__("; ".join(f"{i.field}:{i.code}" for i in issues))


class DuplicateEventError(ValueError):
    def __init__(self, event_id: str) -> None:
        self.event_id = event_id
        super().__init__(f"事件已存在: {event_id}")


class EventStore:
    def __init__(self, path: str | Path, schema: Mapping[str, Any]) -> None:
        self.path = Path(path)
        self.schema = schema
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        for event in self._read_lines():
            self._seen.add(event["event_id"])

    @property
    def seen_event_ids(self) -> frozenset[str]:
        return frozenset(self._seen)

    def contains(self, event_id: str) -> bool:
        return event_id in self._seen

    def append(self, event: Mapping[str, Any]) -> bool:
        """校验并追加事件。

        返回 True 表示本次写入；event_id 已存在时抛 :class:`DuplicateEventError`，
        校验不通过时抛 :class:`ContractViolation`，均不写入任何内容。
        """
        issues = validate_event(event, self.schema)
        if issues:
            raise ContractViolation(issues)
        event_id = event["event_id"]
        with self._lock:
            if event_id in self._seen:
                raise DuplicateEventError(event_id)
            line = json.dumps(event, ensure_ascii=False, sort_keys=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
            self._seen.add(event_id)
        return True

    def append_if_absent(self, event: Mapping[str, Any]) -> bool:
        """幂等追加：event_id 已存在时静默返回 False。"""
        with self._lock:
            if event["event_id"] in self._seen:
                return False
        return self.append(event)

    def replay(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._read_lines())

    def _read_lines(self) -> Iterable[dict[str, Any]]:
        if not self.path.exists():
            return ()
        events: list[dict[str, Any]] = []
        for lineno, raw in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            events.append(json.loads(raw))
        return events


class JsonlLog:
    """咨询请求等非领域事件记录的简单 JSONL 日志，按键幂等。"""

    def __init__(self, path: str | Path, key: str = "id") -> None:
        self.path = Path(path)
        self.key = key
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        if self.path.exists():
            for raw in self.path.read_text(encoding="utf-8").splitlines():
                if raw.strip():
                    self._seen.add(json.loads(raw)[self.key])

    def contains(self, key_value: str) -> bool:
        return key_value in self._seen

    def append_if_absent(self, record: Mapping[str, Any]) -> bool:
        key_value = record[self.key]
        with self._lock:
            if key_value in self._seen:
                return False
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
            self._seen.add(key_value)
        return True

    def replay(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [
            json.loads(raw)
            for raw in self.path.read_text(encoding="utf-8").splitlines()
            if raw.strip()
        ]


class AppendLog:
    """只追加的 JSONL 工作状态日志。

    与 :class:`JsonlLog` 不同，不去重：同一实体（``ref``）可写多条状态记录，
    重放时按写入顺序后者覆盖前者，用于 queued→sent/paused 之类的状态流转。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, record: Mapping[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()

    def replay(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [
            json.loads(raw)
            for raw in self.path.read_text(encoding="utf-8").splitlines()
            if raw.strip()
        ]


def load_schema(path: str | Path | None = None) -> dict[str, Any]:
    if path is None:
        path = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"
    return json.loads(Path(path).read_text(encoding="utf-8"))


def utc_clock_factory() -> Callable[[], Any]:
    from datetime import datetime, timezone

    return lambda: datetime.now(timezone.utc)
