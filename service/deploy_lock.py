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
        existing = self.read_state()
        if existing and not self.is_stale(existing):
            raise LockHeld(existing)
        self._write_self()

    def _write_self(self) -> None:
        state = {
            "pid": os.getpid(),
            "started_at": _now_iso(),
            "caller": self.caller,
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, self.path)

    def _release(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass
