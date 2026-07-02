from __future__ import annotations

import pytest

from bot.util.filelock import FileLock, LockAlreadyHeld


def test_file_lock_acquires_and_creates_file(tmp_path):
    lock_path = tmp_path / "bot.sqlite3.paper.lock"
    lock = FileLock(lock_path).acquire()
    try:
        assert lock_path.exists()
    finally:
        lock.release()
    # After release the metadata (pid + timestamp) is readable.
    assert lock_path.read_bytes().strip() != b""


def test_second_lock_on_same_path_is_rejected(tmp_path):
    lock_path = tmp_path / "bot.sqlite3.paper.lock"
    first = FileLock(lock_path).acquire()
    try:
        with pytest.raises(LockAlreadyHeld):
            FileLock(lock_path).acquire()
    finally:
        first.release()


def test_lock_can_be_reacquired_after_release(tmp_path):
    lock_path = tmp_path / "bot.sqlite3.paper.lock"
    first = FileLock(lock_path).acquire()
    first.release()
    second = FileLock(lock_path).acquire()
    second.release()


def test_context_manager_releases_on_exit(tmp_path):
    lock_path = tmp_path / "bot.sqlite3.paper.lock"
    with FileLock(lock_path):
        pass
    # Re-acquiring after the context manager exits must succeed.
    FileLock(lock_path).acquire().release()
