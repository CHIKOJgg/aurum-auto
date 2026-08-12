from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    def __init__(self, path: Path, channel_id: int):
        self.base_path = path
        self.path = path
        self.channel_id = channel_id
        self.data: dict[str, Any] = {
            "version": 1,
            "channel_id": channel_id,
            "last_seen_message_id": 0,
            "messages": {},
        }

    def _channel_path(self) -> Path:
        return self.base_path.with_name(
            f"{self.base_path.stem}.{abs(self.channel_id)}{self.base_path.suffix}"
        )

    def _load_path(self, path: Path) -> dict[str, Any]:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if int(loaded.get("channel_id", 0)) != self.channel_id:
                raise ValueError(f"State channel_id mismatch in {path}")
            if not isinstance(loaded.get("messages"), dict):
                raise ValueError(f"State messages field is invalid in {path}")
            return loaded
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("State file %s is corrupted: %s. Starting with clean state.", path, exc)
            corrupted_path = path.parent / f"{path.name}.corrupted.{int(time.time())}"
            try:
                path.rename(corrupted_path)
            except OSError as ren_exc:
                logger.warning("Could not rename corrupted state file %s: %s", path, ren_exc)
            return {
                "version": 1,
                "channel_id": self.channel_id,
                "last_seen_message_id": 0,
                "messages": {},
            }

    def load(self) -> None:
        if not self.base_path.exists():
            scoped_path = self._channel_path()
            if scoped_path.exists():
                self.path = scoped_path
                self.data = self._load_path(scoped_path)
            return

        try:
            base_loaded = json.loads(self.base_path.read_text(encoding="utf-8"))
            if int(base_loaded.get("channel_id", 0)) == self.channel_id:
                if not isinstance(base_loaded.get("messages"), dict):
                    raise ValueError("State messages field is invalid")
                self.path = self.base_path
                self.data = base_loaded
                return
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Base state file %s is corrupted: %s", self.base_path, exc)
            corrupted_path = self.base_path.parent / f"{self.base_path.name}.corrupted.{int(time.time())}"
            self.base_path.rename(corrupted_path)

        # Preserve the original channel state and transparently isolate every
        # test/alternate channel in its own file.
        scoped_path = self._channel_path()
        self.path = scoped_path
        if scoped_path.exists():
            self.data = self._load_path(scoped_path)

    @property
    def last_seen_message_id(self) -> int:
        return int(self.data.get("last_seen_message_id", 0))

    def mark(
        self,
        message_id: int,
        status: str,
        *,
        signal: dict[str, Any] | None = None,
        account_results: dict[str, Any] | None = None,
    ) -> None:
        record = self.data["messages"].setdefault(str(message_id), {})
        record["status"] = status
        record["updated_at"] = utc_now()
        if signal is not None:
            record["signal"] = signal
        if account_results is not None:
            record["accounts"] = account_results
        self.data["last_seen_message_id"] = max(
            self.last_seen_message_id, int(message_id)
        )
        self.save()

    def initialize_cutoff(self, latest_message_id: int) -> None:
        previous = self.last_seen_message_id
        if latest_message_id > previous:
            # A compact range record avoids downloading or storing old message text.
            self.data["startup_cutoff"] = {
                "from_exclusive": previous,
                "through_inclusive": latest_message_id,
                "status": "stale_before_startup",
                "at": utc_now(),
            }
            self.data["last_seen_message_id"] = latest_message_id
            self.save()
        elif not self.path.exists():
            self.save()

    def unfinished_messages(self) -> list[tuple[int, dict[str, Any]]]:
        """Return durable execution attempts that were interrupted mid-flight."""
        return [
            (int(message_id), record)
            for message_id, record in self.data["messages"].items()
            if record.get("status") in {"claimed", "executing"}
        ]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True)
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, self.path)
