from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX has no msvcrt
    msvcrt = None


class LockAlreadyHeld(RuntimeError):
    """Raised when a single-instance lock is already held by another process."""


def _acquire_os_lock(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    if msvcrt is not None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    # No OS locking primitive available; best-effort no-op.


def _release_os_lock(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


class FileLock:
    """Cross-platform single-instance advisory file lock (Windows + POSIX)."""

    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def acquire(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Binary mode keeps msvcrt byte-range locking predictable across platforms.
        handle = self.path.open("a+b")
        try:
            _acquire_os_lock(handle)
        except OSError:
            handle.close()
            raise LockAlreadyHeld(f"lock already held at {self.path}") from None
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()} {datetime.now(UTC).isoformat()}".encode())
            handle.flush()
        except OSError:
            # Metadata is informational only; never fail the lock over it.
            pass
        self._handle = handle
        return self

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            _release_os_lock(self._handle)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, *exc_info) -> None:
        self.release()
