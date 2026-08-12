from __future__ import annotations

from pathlib import Path
from typing import BinaryIO


class InstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self.handle: BinaryIO | None = None

    def __enter__(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0, 2)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        try:
            import msvcrt

            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
        except (ImportError, OSError):
            try:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (ImportError, OSError) as exc:
                self.handle.close()
                self.handle = None
                raise RuntimeError("Another bot instance is already running") from exc
        return self

    def __exit__(self, *_: object) -> None:
        if self.handle is None:
            return
        try:
            try:
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            except (ImportError, OSError):
                try:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass  # Best-effort unlock; handle.close() releases the lock.
        finally:
            self.handle.close()
            self.handle = None
