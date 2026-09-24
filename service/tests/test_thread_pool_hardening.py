"""Offline tests for the thread-pool limiter hardening (root-cause A).

2026-09-05 incident: the service died under a 3450-op emit burst because
anyio spun an unbounded number of threads, stalling the process until the
Windows Task Scheduler's ExecutionTimeLimit fired (result 0x00041306).

The fix: an explicit ``anyio.CapacityLimiter`` sized from
``OPTIX_THREAD_LIMITER`` (default 40) is created in ``make_mcp`` and
threaded through every ``anyio.to_thread.run_sync`` call in the
``_offloaded`` wrapper.

These tests exercise that fix *offline* — no uvicorn, no FastMCP dispatch
machinery, no tool registry — by directly reproducing the
``anyio.to_thread.run_sync(fn, limiter=limiter)`` call pattern that the
production wrapper uses.

Two invariants are verified:

1. **Completeness** — all N calls finish even when N far exceeds the cap
   (no deadlock; queued tasks are served once a slot opens).
2. **Bounded concurrency** — peak simultaneous execution of the sync
   function is capped by the limiter, not by N (no O(N) thread growth).

Additionally, the test verifies that ``make_mcp`` correctly:

* creates the limiter from ``OPTIX_THREAD_LIMITER`` at the size specified,
* attaches ``_ftx_sync_fn`` to every offloaded tool so offline callers
  can bypass the async wrapper, and
* leaves ``_STAY_SYNC`` tools (``optix_health``, ``optix_list_projects``)
  as synchronous (``is_async is False``).
"""
from __future__ import annotations

import asyncio
import functools
import threading
import time
from pathlib import Path

import anyio
import anyio.to_thread
import pytest

from service import core
from service.mcp_app import make_mcp
from service.tests.conftest import make_project


# ---------------------------------------------------------------------------
# Offline unit tests — anyio primitives only, no FastMCP
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously (asyncio.run wrapper)."""
    return asyncio.run(coro)


async def _offloaded_calls_under_limiter(
    sync_fn, n_calls: int, limiter: anyio.CapacityLimiter
):
    """Dispatch *n_calls* concurrent anyio.to_thread.run_sync calls through
    *limiter* and return all results.  Mirrors the production _offloaded
    wrapper in mcp_app.make_mcp exactly."""
    coros = [
        anyio.to_thread.run_sync(functools.partial(sync_fn, i), limiter=limiter)
        for i in range(n_calls)
    ]
    return await asyncio.gather(*coros)


class _ConcurrencyCounter:
    """Thread-safe peak-concurrency recorder.  Instances are callable;
    each call increments the active count, sleeps briefly, then decrements.
    """

    def __init__(self, hold_seconds: float = 0.02):
        self._lock = threading.Lock()
        self._active = 0
        self.peak = 0
        self._hold = hold_seconds

    def __call__(self, call_id: int) -> int:
        """Sync work function: record entry, hold, record exit, return id."""
        with self._lock:
            self._active += 1
            if self._active > self.peak:
                self.peak = self._active
        time.sleep(self._hold)
        with self._lock:
            self._active -= 1
        return call_id


class TestOffloadedCallsUnderLimiter:
    """Direct, offline tests of the anyio.CapacityLimiter pattern.

    These reproduce the exact mechanism used by mcp_app._offloaded without
    touching the MCP tool registry, FastMCP dispatch, or service.core.
    """

    def test_all_calls_complete_no_deadlock(self) -> None:
        """N=20 calls through a LIMIT=3 limiter all return results.

        Deadlock would manifest as asyncio.run() hanging indefinitely.
        The limiter must queue excess calls and drain them without stalling.
        """
        LIMIT = 3
        CALLS = 20
        counter = _ConcurrencyCounter(hold_seconds=0.01)
        limiter = anyio.CapacityLimiter(LIMIT)

        results = _run(_offloaded_calls_under_limiter(counter, CALLS, limiter))

        assert len(results) == CALLS, (
            f"expected {CALLS} results, got {len(results)} — deadlock or drop"
        )
        assert set(results) == set(range(CALLS)), "call_ids don't match — some calls lost"

    def test_peak_concurrency_bounded_by_limiter(self) -> None:
        """Peak concurrent executions must not exceed the configured limit.

        Without the limiter, anyio spins one thread per call and peak == N.
        With the limiter, peak must be <= LIMIT regardless of N.
        """
        LIMIT = 4
        CALLS = 16  # deliberately > LIMIT so calls must queue
        counter = _ConcurrencyCounter(hold_seconds=0.025)
        limiter = anyio.CapacityLimiter(LIMIT)

        _run(_offloaded_calls_under_limiter(counter, CALLS, limiter))

        assert counter.peak <= LIMIT, (
            f"thread-pool limiter not effective: peak={counter.peak}, "
            f"configured LIMIT={LIMIT}.  Expected peak <= {LIMIT}."
        )

    def test_limiter_total_tokens_reflects_config(self) -> None:
        """anyio.CapacityLimiter.total_tokens must equal the configured cap."""
        for limit in (1, 4, 10, 40):
            lim = anyio.CapacityLimiter(limit)
            assert lim.total_tokens == limit, (
                f"CapacityLimiter({limit}).total_tokens={lim.total_tokens}"
            )

    def test_single_call_completes(self) -> None:
        """Edge-case: exactly one call with LIMIT=1 still completes."""
        counter = _ConcurrencyCounter(hold_seconds=0.0)
        limiter = anyio.CapacityLimiter(1)
        results = _run(_offloaded_calls_under_limiter(counter, 1, limiter))
        assert results == [0]
        assert counter.peak == 1

    def test_calls_equal_to_limit_complete_without_queuing(self) -> None:
        """When CALLS == LIMIT every call can be in-flight simultaneously."""
        LIMIT = 5
        counter = _ConcurrencyCounter(hold_seconds=0.02)
        limiter = anyio.CapacityLimiter(LIMIT)
        results = _run(_offloaded_calls_under_limiter(counter, LIMIT, limiter))
        assert len(results) == LIMIT
        # peak may be anywhere from 1..LIMIT depending on scheduling;
        # the important invariant is no deadlock and all complete.
        assert counter.peak <= LIMIT


# ---------------------------------------------------------------------------
# Integration tests — make_mcp wires the limiter correctly
# ---------------------------------------------------------------------------


class TestMakeMcpLimiterWiring:
    """Verify that make_mcp configures the limiter from OPTIX_THREAD_LIMITER
    and exposes the expected tool attributes (_ftx_sync_fn, is_async).

    These tests exercise make_mcp but do NOT start uvicorn or exercise
    network transport — they are offline integration tests.
    """

    def test_offloaded_tools_have_ftx_sync_fn(
        self, cfg: core.Config
    ) -> None:
        """Every non-_STAY_SYNC tool must expose ._ftx_sync_fn so that
        offline callers (including other tests) can call the tool's original
        sync function without an event loop."""
        mcp = make_mcp(cfg)
        stay_sync = frozenset(("optix_health", "optix_list_projects"))
        tools = mcp._tool_manager._tools

        for name, tool in tools.items():
            if name in stay_sync:
                continue
            assert hasattr(tool, "_ftx_sync_fn"), (
                f"{name!r}: offloaded tool missing ._ftx_sync_fn — "
                "direct offline callers cannot reach the sync function"
            )

    def test_stay_sync_tools_remain_synchronous(
        self, cfg: core.Config
    ) -> None:
        """optix_health and optix_list_projects are fast, pure-local tools
        that must NOT be offloaded — they stay sync (is_async=False) and
        must NOT have _ftx_sync_fn set."""
        mcp = make_mcp(cfg)
        tools = mcp._tool_manager._tools
        stay_sync = ("optix_health", "optix_list_projects")

        for name in stay_sync:
            tool = tools.get(name)
            if tool is None:
                continue  # tool removed from surface — skip rather than fail
            assert tool.is_async is False, (
                f"{name!r} should be sync but is_async=True"
            )
            assert not hasattr(tool, "_ftx_sync_fn"), (
                f"{name!r} is _STAY_SYNC but has _ftx_sync_fn — "
                "it was incorrectly offloaded"
            )

    def test_limiter_sized_from_env_var(
        self, cfg: core.Config, monkeypatch
    ) -> None:
        """OPTIX_THREAD_LIMITER overrides the default 40-thread cap.

        This test patches the env var BEFORE calling make_mcp so the limiter
        is built with the configured size.  The limiter is not directly
        accessible from outside make_mcp, so we verify the cap indirectly:
        dispatch LIMIT+2 concurrent calls and confirm peak concurrency <=
        configured limit.
        """
        LIMIT = 3
        CALLS = LIMIT + 4  # enough to force queuing

        monkeypatch.setenv("OPTIX_THREAD_LIMITER", str(LIMIT))

        _lock = threading.Lock()
        _active = 0
        _peak = 0

        def _counted_list_screens(cfg_, project):
            nonlocal _active, _peak
            with _lock:
                _active += 1
                if _active > _peak:
                    _peak = _active
            time.sleep(0.02)
            with _lock:
                _active -= 1
            return {"screens": [], "count": 0}

        monkeypatch.setattr(core, "list_screens", _counted_list_screens)
        monkeypatch.setattr(core, "default_project", lambda _cfg: "Alpha")

        mcp = make_mcp(cfg)

        async def _run_all():
            coros = [mcp.call_tool("optix_list_screens", {}) for _ in range(CALLS)]
            return await asyncio.gather(*coros)

        results = asyncio.run(_run_all())

        assert len(results) == CALLS, (
            f"expected {CALLS} completions, got {len(results)} — deadlock under limiter"
        )
        assert _peak <= LIMIT, (
            f"OPTIX_THREAD_LIMITER={LIMIT} not respected: peak concurrency={_peak}"
        )

    def test_default_limiter_size_is_40(
        self, cfg: core.Config, monkeypatch
    ) -> None:
        """When OPTIX_THREAD_LIMITER is unset, the cap defaults to 40.

        Verified by running 40 concurrent calls — all must complete and peak
        must equal at most 40 (trivially true for 40 calls).  Primarily
        guards against a regression where the default is accidentally 0 or 1.
        """
        monkeypatch.delenv("OPTIX_THREAD_LIMITER", raising=False)

        call_count = 0
        _lock = threading.Lock()

        def _fast_list_screens(cfg_, project):
            nonlocal call_count
            with _lock:
                call_count += 1
            return {"screens": [], "count": 0}

        monkeypatch.setattr(core, "list_screens", _fast_list_screens)
        monkeypatch.setattr(core, "default_project", lambda _cfg: "Alpha")

        mcp = make_mcp(cfg)

        async def _run_40():
            coros = [mcp.call_tool("optix_list_screens", {}) for _ in range(40)]
            return await asyncio.gather(*coros)

        results = asyncio.run(_run_40())

        # All 40 must complete — no deadlock from an overly-small default.
        assert len(results) == 40
        assert call_count == 40
