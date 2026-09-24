"""Benchmark test for _write_batch_journal across a 3000-op synthetic batch.

Measures:
  1. Total wall time for all 3000 calls.
  2. Wall time of the first 10 calls vs the last 10 calls and their ratio
     (the O(n²) signature check — ratio > 1 suggests super-linear growth).
  3. Peak RSS delta (resource.getrusage on POSIX, psutil peak working set on Windows)
     before and after the run.

The test passes solely on the basis that all 3000 calls complete without
exception or OOM — no performance-regression assert on timing or RSS values.

"""
from __future__ import annotations

import time as _time

import psutil

from service import core

try:  # POSIX only; Windows has no `resource` module
    import resource
except ImportError:  # pragma: no cover - platform branch
    resource = None


def _peak_rss_kb() -> int:
    if resource is not None:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    mem = psutil.Process().memory_info()
    return getattr(mem, "peak_wset", mem.rss) // 1024


_BATCH_ID = "bench-batch-0001"
_NUM_OPS = 3000


def test_write_batch_journal_3000_ops(tmp_path):
    """Benchmark _write_batch_journal across a 3000-op synthetic batch.

    Calls ``_write_batch_journal`` exactly 3000 times on a fresh temp
    ``state_dir``, growing the ``data`` dict by one op-result entry per call
    to mirror real usage (the function rewrites the entire JSON blob each
    time, so a growing payload is what drives any potential O(n²) cost).

    Prints to stdout:

    * Total wall time for all 3000 calls.
    * Wall time of the first 10 calls vs the last 10 calls and their ratio.
    * Peak RSS delta (``ru_maxrss``) before and after.

    **Pass condition**: all 3000 calls complete without exception or OOM.
    No assertion on timing or RSS values is made.
    """
    # ------------------------------------------------------------------
    # Minimal Config — only state_dir matters for _write_batch_journal.
    # ------------------------------------------------------------------
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    studio_exe = tmp_path / "FTOptixStudio.exe"
    studio_exe.write_text("fake")

    cfg = core.Config(
        projects_root=projects_root,
        studio_exe=studio_exe,
        state_dir=state_dir,
    )

    # ------------------------------------------------------------------
    # Build the initial data dict that grows by one op-result per call.
    # Starting with a realistic header set; op_results accumulates to
    # reflect the journal's real shape after N ops have been applied.
    # ------------------------------------------------------------------
    data: dict = {
        "batch_id": _BATCH_ID,
        "project": "BenchmarkProject",
        "state": "applying",
        "op_count": _NUM_OPS,
        "applied": 0,
        "started_at": "2026-09-06T00:00:00+00:00",
        "updated_at": "2026-09-06T00:00:00+00:00",
        "chunk_index": 0,
        "pid": 12345,
        "op_timings": [],
        "remaining_ops": [],
        "report": {"ok": True, "op_count": _NUM_OPS,
                   "strict": False, "errors": [], "warnings": []},
    }

    # ------------------------------------------------------------------
    # RSS snapshot before the run.
    # ------------------------------------------------------------------
    rss_before_kb: int = _peak_rss_kb()

    # ------------------------------------------------------------------
    # Main benchmark loop — 3000 calls, each with a slightly larger dict.
    # ------------------------------------------------------------------
    call_times: list[float] = []
    total_start = _time.perf_counter()

    for i in range(_NUM_OPS):
        # Grow the dict: add one more timing entry (mirrors actual usage
        # where op_timings accumulates throughout the batch lifecycle).
        data["applied"] = i + 1
        data["op_timings"].append({
            "index": i,
            "op": "set_property",
            "ok": True,
            "ms": i % 500,
        })

        t0 = _time.perf_counter()
        core._write_batch_journal(cfg, data)
        call_times.append(_time.perf_counter() - t0)

    total_elapsed_s = _time.perf_counter() - total_start

    # ------------------------------------------------------------------
    # RSS snapshot after the run.
    # ------------------------------------------------------------------
    rss_after_kb: int = _peak_rss_kb()

    # ------------------------------------------------------------------
    # Compute summary metrics.
    # ------------------------------------------------------------------
    first10_s = sum(call_times[:10])
    last10_s = sum(call_times[-10:])
    ratio = last10_s / first10_s if first10_s > 0 else float("inf")
    rss_delta_kb = rss_after_kb - rss_before_kb

    # ------------------------------------------------------------------
    # Print results — always visible with `pytest -s`.
    # ------------------------------------------------------------------
    print(f"\n--- _write_batch_journal benchmark ({_NUM_OPS} ops) ---")
    print(f"Total wall time    : {total_elapsed_s * 1000:.1f} ms")
    print(f"First-10 wall time : {first10_s * 1000:.3f} ms")
    print(f"Last-10 wall time  : {last10_s * 1000:.3f} ms")
    print(
        f"Last/First ratio   : {ratio:.2f}x"
        f"  (>1 = potential O(n²) growth signature)"
    )
    print(
        f"Peak RSS delta     : {rss_delta_kb:+d} KB"
        f"  (ru_maxrss before={rss_before_kb} KB, after={rss_after_kb} KB)"
    )
    print("--- end benchmark ---\n")

    # ------------------------------------------------------------------
    # Pass condition: all 3000 calls completed without exception or OOM.
    # ------------------------------------------------------------------
    assert len(call_times) == _NUM_OPS, (
        f"Expected {_NUM_OPS} journal writes; only {len(call_times)} completed"
    )
