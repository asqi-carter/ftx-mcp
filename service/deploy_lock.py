"""PID-aware single-writer deploy lock (see docs/architecture.md, Concurrency).

The lock file lives at `<state_dir>/deploy.lock` and contains JSON with
pid + started_at + caller. Stale locks (dead PID, or older than
stale_seconds) are broken on acquire. A live lock raises LockHeld so
the HTTP/MCP layer can return a 409 with the in-flight info.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class LockHeld(Exception):
    """Raised when the deploy lock is held by a live, non-stale process."""

    def __init__(self, lock_state: dict):
        self.lock_state = lock_state
        super().__init__(f"deploy lock held: {lock_state}")


def _pid_alive(pid: int) -> bool:
    """Return True if `pid` is a live process. Cross-platform."""
    try:
        import psutil  # type: ignore
        return psutil.pid_exists(pid)
    except ImportError:
        pass
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if h == 0:
                return False
            kernel32.CloseHandle(h)
            return True
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except Exception:
        return True


def _now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


@dataclass
class DeployLock:
    path: Path
    caller: str = "unknown"
    stale_seconds: float = 600.0  # 10 min stale-lock threshold

    @contextmanager
    def acquire(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._try_acquire()
        try:
            yield
        finally:
            self._release()

    def read_state(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def is_stale(self, state: dict) -> bool:
        pid = state.get("pid")
        if not isinstance(pid, int):
            return True
        if not _pid_alive(pid):
            return True
        started_at = state.get("started_at")
        if isinstance(started_at, str):
            try:
                ts = _dt.datetime.fromisoformat(started_at).timestamp()
            except ValueError:
                return True
            if time.time() - ts > self.stale_seconds:
                return True
        return False

    def _try_acquire(self) -> None:
        # Try to win an exclusive create; if a lock file already exists, break
        # it only when it is NOT live (stale/dead/corrupt) and retry once. This
        # never unlinks a live lock (so a concurrent acquirer's valid lock is
        # safe) and never lets two acquirers both pass — the O_CREAT|O_EXCL in
        # _write_self is the single serialization point.
        for _ in range(2):
            existing = self.read_state()
            if existing and not self.is_stale(existing):
                raise LockHeld(existing)
            try:
                self._write_self()
                return
            except FileExistsError:
                current = self.read_state()
                if current and not self.is_stale(current):
                    raise LockHeld(current) from None
                # Not live (stale/dead/corrupt/unparseable): break it, then the
                # loop retries the exclusive create.
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
        # Both create attempts lost to a live concurrent acquirer. Fail closed.
        current = self.read_state()
        raise LockHeld(
            current or {"pid": None, "note": "held by concurrent acquirer"}
        ) from None

    def _write_self(self) -> None:
        """Atomically create the lock file, failing if it already exists.

        O_CREAT|O_EXCL makes the create the single serialization point: exactly
        one concurrent acquirer can create the file, the losers get
        FileExistsError. This replaces a check-then-os.replace(tmp, path) that
        unconditionally overwrote, letting two acquirers that both saw "no lock"
        each write and both proceed.
        """
        state = {
            "pid": os.getpid(),
            "started_at": _now_iso(),
            "caller": self.caller,
        }
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, json.dumps(state).encode("utf-8"))
        finally:
            os.close(fd)

    def _release(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass
