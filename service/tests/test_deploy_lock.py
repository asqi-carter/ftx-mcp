"""Tests for service.deploy_lock — PID-aware stale-lock handling."""
from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path

import pytest

from service.deploy_lock import DeployLock, LockHeld


def test_lock_acquires_when_no_existing(state_dir: Path) -> None:
    lock = DeployLock(state_dir / "deploy.lock", caller="test")
    with lock.acquire():
        state = lock.read_state()
        assert state is not None
        assert state["pid"] == os.getpid()
        assert state["caller"] == "test"
    # released
    assert not (state_dir / "deploy.lock").exists()


def test_lock_blocks_when_held_by_live_pid(state_dir: Path) -> None:
    other_lock = state_dir / "deploy.lock"
    state_dir.mkdir(exist_ok=True)
    state = {
        "pid": os.getpid(),  # we are alive
        "started_at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        "caller": "elsewhere",
    }
    other_lock.write_text(json.dumps(state), encoding="utf-8")

    lock = DeployLock(other_lock, caller="newcomer")
    with pytest.raises(LockHeld) as excinfo:
        with lock.acquire():
            pass
    assert excinfo.value.lock_state["caller"] == "elsewhere"


def test_lock_breaks_when_pid_is_dead(state_dir: Path) -> None:
    dead_pid = 999_999_999  # unreachable
    state = {
        "pid": dead_pid,
        "started_at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        "caller": "ghost",
    }
    lock_path = state_dir / "deploy.lock"
    lock_path.write_text(json.dumps(state), encoding="utf-8")

    lock = DeployLock(lock_path, caller="newcomer")
    with lock.acquire():
        st = lock.read_state()
        assert st is not None
        assert st["pid"] == os.getpid()
        assert st["caller"] == "newcomer"


def test_lock_breaks_when_stale_by_age(state_dir: Path) -> None:
    state = {
        "pid": os.getpid(),
        "started_at": (_dt.datetime.now(_dt.UTC)
                       - _dt.timedelta(hours=2)).isoformat(timespec="seconds"),
        "caller": "ancient",
    }
    lock_path = state_dir / "deploy.lock"
    lock_path.write_text(json.dumps(state), encoding="utf-8")

    lock = DeployLock(lock_path, caller="newcomer", stale_seconds=60)
    with lock.acquire():
        st = lock.read_state()
        assert st is not None
        assert st["caller"] == "newcomer"


def test_lock_breaks_on_corrupt_state(state_dir: Path) -> None:
    lock_path = state_dir / "deploy.lock"
    lock_path.write_text("not-json", encoding="utf-8")
    lock = DeployLock(lock_path, caller="newcomer")
    with lock.acquire():
        st = lock.read_state()
        assert st is not None
        assert st["caller"] == "newcomer"


def test_write_self_is_atomic_exclusive(state_dir: Path) -> None:
    """_write_self creates the lock exclusively (O_CREAT|O_EXCL): a second
    writer that saw 'no lock' cannot overwrite the first and proceed. This is
    the serialization point that prevents two concurrent deploys from both
    holding the lock."""
    state_dir.mkdir(exist_ok=True)
    lock_path = state_dir / "deploy.lock"
    DeployLock(lock_path, caller="first")._write_self()  # first writer wins
    with pytest.raises(FileExistsError):
        DeployLock(lock_path, caller="second")._write_self()


def test_acquire_fails_closed_when_live_lock_appears_after_check(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a live lock is created between our staleness check and our exclusive
    create (the classic TOCTOU), acquisition must fail closed with LockHeld,
    not clobber the other holder."""
    state_dir.mkdir(exist_ok=True)
    lock_path = state_dir / "deploy.lock"
    lock = DeployLock(lock_path, caller="racer")

    live = {
        "pid": os.getpid(),  # alive + fresh -> not stale
        "started_at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        "caller": "winner",
    }
    original_read = lock.read_state
    calls = {"n": 0}

    def read_state_racing():
        # First read (staleness check) sees no lock; a concurrent winner then
        # creates it, so the exclusive create fails and the re-read sees it.
        calls["n"] += 1
        if calls["n"] == 1:
            lock_path.write_text(json.dumps(live), encoding="utf-8")
            return None
        return original_read()

    monkeypatch.setattr(lock, "read_state", read_state_racing)
    with pytest.raises(LockHeld) as excinfo:
        lock._try_acquire()
    assert excinfo.value.lock_state["caller"] == "winner"
