"""Offline unit tests for batch-edit timings, progress, chunking, journal, and
back-compat (epic: optix_bridge_edit batch progress reporting, chunking, and
client-timeout survival).

All tests are fully offline — bridge HTTP is monkeypatched; no Studio process,
no live bridge.  The module-level clock (time.monotonic) is monkeypatched where
deterministic ms assertions are required.

"""
from __future__ import annotations

import json
import time as _time

import pytest

from service import core
from service.tests.conftest import make_project


# ---------------------------------------------------------------------------
# Shared fixtures / helpers (mirror the ones in test_bridge_writes.py so this
# file is self-contained and doesn't import from another test module)
# ---------------------------------------------------------------------------

_OK_REPORT = {
    "ok": True, "op_count": 2, "strict": False,
    "errors": [], "warnings": [],
}
_BAD_REPORT = {
    "ok": False, "op_count": 2, "strict": False,
    "warnings": [],
    "errors": [{"op_index": 1, "code": "unresolved_reference",
                "message": "no node at 'UI/MainWindow/Later'"}],
}

# Canonical 2-op and 3-op test batches.
_TWO_OPS = [
    {"op": "create_widget", "screen": "UI/MainWindow", "name": "B1",
     "widget_type": "Rectangle"},
    {"op": "set_property", "path": "UI/MainWindow/B1", "name": "Width",
     "value": "40"},
]
_THREE_OPS = _TWO_OPS + [
    {"op": "set_property", "path": "UI/MainWindow/B1", "name": "Height",
     "value": "20"},
]


def _fake_validate(report, *, seen=None):
    """Fake _bridge_post_body that returns *report* without hitting the bridge."""
    def fake(cfg, path, payload, timeout=20.0, **_):
        if seen is not None:
            seen.append((path, payload))
        return 200, report
    return fake


def _patch_apply_ok(monkeypatch, *, tracker=None):
    """Patch _apply_one_edit to succeed immediately; optionally record op dicts."""
    def _ok(cfg, project, op):
        if tracker is not None:
            tracker.append(op.copy())
    monkeypatch.setattr(core, "_apply_one_edit", _ok)


@pytest.fixture(autouse=True)
def _clear_bridge_cache():
    core.reset_bridge_cache()
    yield
    core.reset_bridge_cache()


@pytest.fixture
def alpha(cfg, projects_root):
    """Config with a resolvable project 'Alpha' that matches the fake bridge."""
    make_project(projects_root, "Alpha")
    return cfg


# ---------------------------------------------------------------------------
# Helper: step clock — returns 0, step, 2*step, 3*step, … on each call.
# Inject via: monkeypatch.setattr(core.time, "monotonic", _step_clock(1.0))
# ---------------------------------------------------------------------------

def _step_clock(step: float = 1.0):
    """Factory: returns a fake time.monotonic that advances by *step* each call."""
    state = [0.0]
    def fake():
        v = state[0]
        state[0] += step
        return v
    return fake


def _seq_clock(values):
    """Factory: returns a fake time.monotonic that pops values from a list."""
    it = iter(values)
    def fake():
        return next(it)
    return fake


# ===========================================================================
# (1) Timings — deterministic clock assertions
# ===========================================================================

def test_timings_op_ms_equals_1000_per_op(alpha, monkeypatch):
    """3-op batch with 1-second/call step clock: each op_timings entry ms=1000.

    time.monotonic is patched to advance 1 second per call.  In the apply
    loop each op contributes exactly two calls (start + end), so op_ms =
    int((end - start) * 1000) = 1000 for every op regardless of the chunk-
    check and final apply_ms calls that also consume counter ticks.
    """
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    _patch_apply_ok(monkeypatch)
    # Set chunk thresholds well above what the fake clock can trigger.
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_SECONDS", "99999")
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_OPS", "99999")

    monkeypatch.setattr(core.time, "monotonic", _step_clock(1.0))
    report_3 = {"ok": True, "op_count": 3, "strict": False,
                "errors": [], "warnings": []}
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(report_3))

    out = core.bridge_edit(alpha, "Alpha", _THREE_OPS)

    assert out["state"] == "succeeded"
    assert "op_timings" in out
    timings = out["op_timings"]
    assert len(timings) == 3
    for i, entry in enumerate(timings):
        assert entry["index"] == i
        assert entry["op"] == _THREE_OPS[i]["op"]
        assert entry["ms"] == 1000, (
            f"op_timings[{i}].ms expected 1000, got {entry['ms']} "
            "(clock step mismatch)"
        )
        assert entry["ok"] is True
    # apply_ms and validate_ms must be present (exact values depend on the
    # number of monotonic() calls the implementation makes, so only >= 0).
    assert isinstance(out["apply_ms"], int) and out["apply_ms"] >= 0
    assert isinstance(out["validate_ms"], int) and out["validate_ms"] >= 0


def test_timings_validate_ms_present_on_dry_run(alpha, monkeypatch):
    """validate_ms must be present even when dry_run=True; op_timings absent."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)

    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS, dry_run=True)

    assert out["state"] == "validated"
    assert "validate_ms" in out, "validate_ms must be present for dry_run"
    assert "op_timings" not in out, "op_timings must be absent for dry_run"
    assert "apply_ms" not in out, "apply_ms must be absent for dry_run"


def test_timings_validate_ms_present_on_validation_failure(alpha, monkeypatch):
    """validate_ms must be present when validation refuses; op_timings absent."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_BAD_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)

    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS)

    assert out["state"] == "validated"
    assert "validate_ms" in out, "validate_ms must be present on refusal"
    assert "op_timings" not in out, "op_timings must be absent on refusal"
    assert "apply_ms" not in out, "apply_ms must be absent on refusal"


def test_timings_failed_op_has_ok_false_in_op_timings(alpha, monkeypatch):
    """A failing op must appear in op_timings with ok=False; prior ops ok=True."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)

    def flaky(cfg, project, op):
        if op["op"] == "set_property":
            raise core.BridgeWriteFailed("boom")

    monkeypatch.setattr(core, "_apply_one_edit", flaky)

    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS)

    assert out["state"] == "partial"
    timings = out["op_timings"]
    assert timings[0]["ok"] is True  and timings[0]["op"] == "create_widget"
    assert timings[1]["ok"] is False and timings[1]["op"] == "set_property"
    assert isinstance(timings[1]["ms"], int) and timings[1]["ms"] >= 0
    assert "apply_ms" in out and out["apply_ms"] >= 0


# ===========================================================================
# (2) Progress callback — order, indices, applied field
# ===========================================================================

def test_progress_callback_order_and_fields(alpha, monkeypatch):
    """3-op batch: phase='validated' first, then one event per op.

    Indices must be strictly increasing; each post-op event's 'applied' must
    match the running count at that point and must equal out['applied'] for
    the final event.
    """
    report_3 = {"ok": True, "op_count": 3, "strict": False,
                "errors": [], "warnings": []}
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(report_3))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    _patch_apply_ok(monkeypatch)

    events: list[dict] = []
    out = core.bridge_edit(alpha, "Alpha", _THREE_OPS, on_progress=events.append)

    assert out["state"] == "succeeded"
    # 1 pre-loop (validated) + 3 post-op = 4 events.
    assert len(events) == 4

    pre = events[0]
    assert pre["phase"] == "validated"
    assert pre["index"] == 0
    assert pre["total"] == 3
    assert pre["op"] is None
    assert pre["batch_id"] == out["batch_id"]

    indices = [ev["index"] for ev in events[1:]]
    assert indices == list(range(1, 4)), "indices must be strictly 1, 2, 3"

    for rank, ev in enumerate(events[1:], start=1):
        assert ev["batch_id"] == out["batch_id"]
        assert ev["total"] == 3
        assert ev["op"] == _THREE_OPS[rank - 1]["op"]
        assert isinstance(ev["ms"], int) and ev["ms"] >= 0
        assert ev["applied"] == rank

    # The last event's 'applied' must agree with the final out['applied'].
    assert events[-1]["applied"] == out["applied"]


# ===========================================================================
# (3) Raising callback is harmless
# ===========================================================================

def test_raising_progress_callback_does_not_abort_batch(alpha, monkeypatch):
    """A callback that raises (lambda ev: 1/0) must not abort the batch.

    All 3 ops must be applied and state must be 'succeeded'.
    """
    report_3 = {"ok": True, "op_count": 3, "strict": False,
                "errors": [], "warnings": []}
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(report_3))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)

    call_count = [0]
    def counting_apply(cfg, project, op):
        call_count[0] += 1
    monkeypatch.setattr(core, "_apply_one_edit", counting_apply)

    out = core.bridge_edit(
        alpha, "Alpha", _THREE_OPS,
        on_progress=lambda ev: 1 / 0,   # always raises ZeroDivisionError
    )

    assert out["state"] == "succeeded"
    assert out["applied"] == 3
    assert call_count[0] == 3, "_apply_one_edit must be called 3 times"


# ===========================================================================
# (4) Chunk by ops — 10 ops, chunk_ops=4, two continues → cumulative applied=10
# ===========================================================================

def _make_10_ops():
    """Return a list of 10 distinct ops for chunk tests."""
    ops = [{"op": "create_widget", "screen": "UI/MainWindow",
            "name": "Root", "widget_type": "Rectangle"}]
    for i in range(1, 10):
        ops.append({"op": "set_property", "path": "UI/MainWindow/Root",
                    "name": f"Prop{i}", "value": str(i)})
    return ops


_TEN_OPS = _make_10_ops()
_TEN_REPORT = {"ok": True, "op_count": 10, "strict": False,
               "errors": [], "warnings": []}


def test_chunk_by_ops_first_chunk(alpha, monkeypatch):
    """10-op batch with chunk_ops=4: first call applies 4, remaining=6, chunk_reason='ops'."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_TEN_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_SECONDS", "99999")
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_OPS", "4")
    _patch_apply_ok(monkeypatch)

    out = core.bridge_edit(alpha, "Alpha", _TEN_OPS)

    assert out["state"] == "chunked"
    assert out["applied"] == 4
    assert out["remaining_ops"] == 6
    assert out["chunk_reason"] == "ops"
    assert "batch_id" in out and isinstance(out["batch_id"], str)


def test_chunk_by_ops_two_continues_give_cumulative_applied_10(alpha, monkeypatch):
    """10-op batch with chunk_ops=4: two continues reach state='succeeded', applied=10.

    Also verifies that all 10 ops are applied in their original order.
    """
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_TEN_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_SECONDS", "99999")
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_OPS", "4")

    applied_ops: list = []
    _patch_apply_ok(monkeypatch, tracker=applied_ops)

    # Enqueue: applies ops 0-3, chunks.
    out1 = core.bridge_edit(alpha, "Alpha", _TEN_OPS)
    assert out1["state"] == "chunked"
    assert out1["applied"] == 4
    batch_id = out1["batch_id"]

    # Continue 1: applies ops 4-7, chunks again.
    out2 = core.bridge_edit_continue(alpha, "Alpha", batch_id)
    assert out2["state"] == "chunked"
    assert out2["applied"] == 8
    assert out2["remaining_ops"] == 2

    # Continue 2: applies ops 8-9, finishes.
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_OPS", "99999")
    out3 = core.bridge_edit_continue(alpha, "Alpha", batch_id)
    assert out3["state"] == "succeeded"
    assert out3["applied"] == 10

    # Original op order must be preserved across all three calls.
    assert len(applied_ops) == 10
    for i, op in enumerate(applied_ops):
        assert op["op"] == _TEN_OPS[i]["op"], (
            f"op[{i}] mismatch: expected {_TEN_OPS[i]['op']!r}, got {op['op']!r}"
        )


# ===========================================================================
# (5) Chunk by time — monkeypatched clock, 40s/op, chunk_seconds=90
# ===========================================================================

def test_chunk_by_time_with_fake_clock(alpha, monkeypatch):
    """Monkeypatched clock at 40s/op with chunk_seconds=90 chunks after 3 ops.

    Clock layout (5-op batch, no on_progress):
      validate_t0, validate_end,
      apply_t0,
      op0_start, op0_end, op0_chunk_check,
      op1_start, op1_end, op1_chunk_check,
      op2_start, op2_end, op2_chunk_check (triggers: 120s >= 90s),
      apply_ms_call (chunk path)

    After ops 0-2 are applied, remaining ops [3, 4] trigger state='chunked',
    chunk_reason='time'.
    """
    # 5-op batch so there are ops remaining after op2.
    five_ops = _TEN_OPS[:5]
    report_5 = {"ok": True, "op_count": 5, "strict": False,
                "errors": [], "warnings": []}
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(report_5))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_SECONDS", "90")
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_OPS", "99999")
    _patch_apply_ok(monkeypatch)

    # Pre-computed clock sequence.
    # elapsed_after_op_i = clock[chunk_check_i] - apply_t0
    #   op0 elapsed = 40.004 - 0.002 =  40.002 < 90  → no chunk
    #   op1 elapsed = 80.006 - 0.002 =  80.004 < 90  → no chunk
    #   op2 elapsed = 120.008 - 0.002 = 120.006 >= 90 → CHUNK
    clock_values = [
        # validate phase
        0.000, 0.001,
        # apply_t0
        0.002,
        # op0
        0.003, 40.003, 40.004,
        # op1
        40.005, 80.005, 80.006,
        # op2 → chunk fires here
        80.007, 120.007, 120.008,
        # apply_ms call inside the chunk branch
        120.009,
    ]
    monkeypatch.setattr(core.time, "monotonic", _seq_clock(clock_values))

    out = core.bridge_edit(alpha, "Alpha", five_ops)

    assert out["state"] == "chunked", f"expected chunked, got {out['state']!r}"
    assert out["chunk_reason"] == "time"
    assert out["applied"] == 3
    assert out["remaining_ops"] == 2


# ===========================================================================
# (6) Validation called exactly once across enqueue + two continues
# ===========================================================================

def test_validation_called_exactly_once_across_continues(alpha, monkeypatch):
    """_bridge_post_body (the validate round-trip) must be called exactly ONCE
    for the whole lifecycle: initial enqueue + two bridge_edit_continue calls.
    """
    seen: list = []
    monkeypatch.setattr(core, "_bridge_post_body",
                        _fake_validate(_TEN_REPORT, seen=seen))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_SECONDS", "99999")
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_OPS", "4")
    _patch_apply_ok(monkeypatch)

    out1 = core.bridge_edit(alpha, "Alpha", _TEN_OPS)
    assert out1["state"] == "chunked"
    batch_id = out1["batch_id"]
    validate_calls_after_enqueue = len(seen)

    core.bridge_edit_continue(alpha, "Alpha", batch_id)
    assert len(seen) == validate_calls_after_enqueue, (
        "continue must NOT call the bridge validator"
    )

    monkeypatch.setenv("OPTIX_BATCH_CHUNK_OPS", "99999")
    core.bridge_edit_continue(alpha, "Alpha", batch_id)
    assert len(seen) == validate_calls_after_enqueue, (
        "second continue must also NOT call the bridge validator"
    )

    assert validate_calls_after_enqueue == 1, (
        f"expected exactly 1 validate call total, got {validate_calls_after_enqueue}"
    )


# ===========================================================================
# (7) Journal round-trip
# ===========================================================================

def test_journal_round_trip_full_lifecycle(alpha, monkeypatch):
    """Full round-trip: journal tracks state across two chunks.

    After the first chunk:
      - journal file exists, state='chunked', remaining_ops len=6
    After the last continue:
      - journal state='succeeded', applied=10, remaining_ops=[]
      - bridge_edit_status(batch_id) returns that exact document
    """
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_TEN_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_SECONDS", "99999")
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_OPS", "4")
    _patch_apply_ok(monkeypatch)

    # --- Enqueue (chunk 0) ---
    out1 = core.bridge_edit(alpha, "Alpha", _TEN_OPS)
    assert out1["state"] == "chunked"
    batch_id = out1["batch_id"]

    # Journal must exist and reflect the chunked state.
    journal_path = alpha.state_dir / "batches" / f"{batch_id}.json"
    assert journal_path.exists(), "journal must be written on first chunk"
    j1 = json.loads(journal_path.read_text(encoding="utf-8"))
    assert j1["state"] == "chunked"
    assert len(j1["remaining_ops"]) == 6

    # --- Continue 1 (chunk 1 → chunk again) ---
    out2 = core.bridge_edit_continue(alpha, "Alpha", batch_id)
    assert out2["state"] == "chunked"

    # --- Continue 2 (chunk 2 → succeeded) ---
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_OPS", "99999")
    out3 = core.bridge_edit_continue(alpha, "Alpha", batch_id)
    assert out3["state"] == "succeeded"
    assert out3["applied"] == 10

    # Journal must now reflect the terminal succeeded state.
    j_final = json.loads(journal_path.read_text(encoding="utf-8"))
    assert j_final["state"] == "succeeded"
    assert j_final["applied"] == 10
    assert j_final["remaining_ops"] == []

    # bridge_edit_status must return that same document.
    status = core.bridge_edit_status(alpha, "Alpha", batch_id=batch_id)
    assert status["state"] == "succeeded"
    assert status["applied"] == 10
    assert status["batch_id"] == batch_id
    assert status["remaining_ops"] == []


# ===========================================================================
# (8) Status without id: project-filtered, newest-first
# ===========================================================================

def test_status_no_id_project_filtered_newest_first(alpha, tmp_path):
    """Without batch_id: returns only the calling project's journals, newest-first."""
    batches_dir = alpha.state_dir / "batches"
    batches_dir.mkdir(parents=True, exist_ok=True)

    import os as _os
    import time as _time_mod

    # Write journals with deliberate mtime offsets so the sort order is known.
    # alpha_old, alpha_new belong to Alpha; beta belongs to Beta.
    base_mtime = _time_mod.time() - 300  # 5 minutes ago baseline

    def _write_journal(name, project, offset):
        path = batches_dir / f"{name}.json"
        path.write_text(
            json.dumps({"batch_id": name, "project": project,
                        "state": "succeeded", "op_count": 1, "applied": 1}),
            encoding="utf-8",
        )
        mtime = base_mtime + offset
        _os.utime(path, (mtime, mtime))
        return path

    _write_journal("alpha_old", "Alpha", offset=0)    # oldest Alpha
    _write_journal("alpha_new", "Alpha", offset=100)  # newest Alpha
    _write_journal("beta_batch", "Beta",  offset=200) # Beta — must be excluded

    status = core.bridge_edit_status(alpha, "Alpha")

    assert status["project"] == "Alpha"
    ids = [b["batch_id"] for b in status["batches"]]

    # Beta must be excluded.
    assert "beta_batch" not in ids

    # Alpha batches must appear, newest-first.
    assert "alpha_new" in ids
    assert "alpha_old" in ids
    if "alpha_new" in ids and "alpha_old" in ids:
        assert ids.index("alpha_new") < ids.index("alpha_old"), (
            "newest journal must come before oldest"
        )


# ===========================================================================
# (9) Unknown id returns dict with error='unknown_batch' (no pytest.raises)
# ===========================================================================

def test_unknown_batch_id_returns_structured_dict_not_exception(alpha):
    """bridge_edit_status with an unknown batch_id must return a structured dict.

    The error must be a RETURN VALUE — pytest.raises is intentionally absent.
    """
    result = core.bridge_edit_status(alpha, "Alpha", batch_id="totally_unknown_id")

    assert isinstance(result, dict), "must return a dict, not raise"
    assert result["error"] == "unknown_batch"
    assert result["state"] == "failed"
    assert result["batch_id"] == "totally_unknown_id"
    assert "known" in result


# ===========================================================================
# (10) Terminal batches refuse continue — parametrized
# ===========================================================================

def _write_journal_direct(alpha, batch_id, state, *, pid=None, remaining_ops=None):
    """Write a synthetic journal file for the given batch_id and state."""
    batches_dir = alpha.state_dir / "batches"
    batches_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "batch_id": batch_id,
        "project": "Alpha",
        "state": state,
        "op_count": 2,
        "applied": 1 if state != "validated" else 0,
        "started_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:01+00:00",
        "pid": pid if pid is not None else 1,
        "chunk_index": 0,
        "op_timings": [],
        "report": _OK_REPORT,
        "remaining_ops": remaining_ops if remaining_ops is not None else list(_TWO_OPS),
    }
    (batches_dir / f"{batch_id}.json").write_text(
        json.dumps(data), encoding="utf-8"
    )


@pytest.mark.parametrize("terminal_state", [
    "succeeded",
    "partial",
    "validated",
])
def test_terminal_batch_refuses_continue(alpha, monkeypatch, terminal_state):
    """bridge_edit_continue must refuse every non-chunked terminal state.

    Returns state='failed', error='batch_not_resumable', batch_state=<terminal_state>.
    """
    batch_id = f"terminal_{terminal_state[:6]}"
    _write_journal_direct(alpha, batch_id, terminal_state)

    cont = core.bridge_edit_continue(alpha, "Alpha", batch_id)

    assert cont["state"] == "failed"
    assert cont["error"] == "batch_not_resumable"
    assert cont["batch_id"] == batch_id
    assert cont["batch_state"] == terminal_state


def test_terminal_batch_refuses_continue_dead_pid(alpha, monkeypatch):
    """bridge_edit_continue refuses state='applying' when the pid is dead.

    Returns state='abandoned' with a nudge to inspect the live model before
    re-authoring.
    """
    batch_id = "applying_dead_pid"
    # Use a pid that is guaranteed not to exist on any platform.
    dead_pid = 2_000_000_000
    _write_journal_direct(alpha, batch_id, "applying", pid=dead_pid)

    cont = core.bridge_edit_continue(alpha, "Alpha", batch_id)

    assert cont["state"] == "abandoned"
    assert cont["error"] == "batch_abandoned"
    assert cont["batch_id"] == batch_id
    assert "optix_describe_node" in cont.get("nudge", ""), (
        "nudge must point the caller toward describe_node"
    )


# ===========================================================================
# (11) Mid-chunk failure — failure during a continue call
# ===========================================================================

def test_mid_chunk_failure_during_continue(alpha, monkeypatch):
    """A failure mid-apply during bridge_edit_continue yields state='partial'.

    Checks:
    - out['state'] == 'partial'
    - out['failed_op'] is present with the correct op name
    - journal is written with state='partial' (terminal — refuses further continues)
    - the failing op appears in op_timings with ok=False
    """
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_TEN_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_SECONDS", "99999")
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_OPS", "4")
    _patch_apply_ok(monkeypatch)

    # Enqueue: chunks after 4 ops.
    out1 = core.bridge_edit(alpha, "Alpha", _TEN_OPS)
    assert out1["state"] == "chunked"
    batch_id = out1["batch_id"]

    # For the continue, make the SECOND op of that chunk fail.
    call_count = [0]
    def fail_on_second(cfg, project, op):
        call_count[0] += 1
        if call_count[0] == 2:
            raise core.BridgeWriteFailed("simulated mid-chunk failure")
    monkeypatch.setattr(core, "_apply_one_edit", fail_on_second)
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_OPS", "99999")

    out2 = core.bridge_edit_continue(alpha, "Alpha", batch_id)

    assert out2["state"] == "partial"
    assert "failed_op" in out2
    assert "op_timings" in out2
    # The failing op (local index 1 in the continue call) must have ok=False.
    failed_timing = next(
        (t for t in out2["op_timings"] if t["ok"] is False), None
    )
    assert failed_timing is not None, "failing op must appear in op_timings with ok=False"

    # The journal must now record the terminal partial state.
    journal_path = alpha.state_dir / "batches" / f"{batch_id}.json"
    j = json.loads(journal_path.read_text(encoding="utf-8"))
    assert j["state"] == "partial"
    assert "failed_op" in j

    # Attempting a further continue must be refused.
    cont_after = core.bridge_edit_continue(alpha, "Alpha", batch_id)
    assert cont_after["state"] == "failed"
    assert cont_after["error"] == "batch_not_resumable"


# ===========================================================================
# (12) Journal write failure does not abort the batch
# ===========================================================================

def test_journal_write_failure_does_not_abort_batch(alpha, monkeypatch):
    """_write_batch_journal raising must never prevent the batch from completing.

    The batch must return state='succeeded' even when every journal write raises.
    """
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    _patch_apply_ok(monkeypatch)

    journal_calls: list = []

    def _bad_journal(cfg, data):
        journal_calls.append(data.get("state"))
        raise OSError("disk full – simulated journal failure")

    monkeypatch.setattr(core, "_write_batch_journal", _bad_journal)

    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS)

    assert out["state"] == "succeeded"
    assert out["applied"] == 2
    # The helper was attempted at least once (initial + per-op + final writes).
    assert len(journal_calls) > 0, "_write_batch_journal must be attempted"


# ===========================================================================
# (13) Back-compat — existing success/failure paths and refused-batch journal
# ===========================================================================

def test_backcompat_clean_report_applies_ops(alpha, monkeypatch):
    """Regression: test_bridge_edit_applies_after_a_clean_report equivalent.

    A clean report must lead to state='succeeded', all ops applied.
    This test mirrors the back-compat contract so future refactors cannot
    silently break the validate-then-apply flow.
    """
    applied: list = []
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op["op"]))

    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS)

    assert out["state"] == "succeeded"
    assert out["applied"] == 2 and out["op_count"] == 2
    assert applied == ["create_widget", "set_property"]


def test_backcompat_validation_failure_applies_nothing(alpha, monkeypatch):
    """Regression: test_bridge_edit_applies_nothing_when_validation_fails equivalent.

    A bad report must lead to state='validated', applied=0, nothing applied.
    """
    applied: list = []
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_BAD_REPORT))
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op["op"]))

    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS)

    assert out["state"] == "validated"
    assert out["applied"] == 0 and applied == []
    assert out["report"]["errors"][0]["op_index"] == 1
    assert "op_index" in out["nudge"]


def test_backcompat_refused_batch_leaves_no_journal_file(alpha, monkeypatch):
    """Refused batches (destructive-op unknown field) must not write a journal.

    The journal is only written when the apply loop starts (after a clean report).
    A batch refused at the pre-validation stage (ok=False before the loop) must
    leave no journal file in <state_dir>/batches/.
    """
    # A delete op with an unknown field is hard-refused (ok=False) before any apply.
    bad_op = {
        "op": "delete",
        "path": "UI/Screens/Foo",
        "name": "AttachedPanelLoader",  # `name` is not a valid delete field
    }
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: (_ for _ in ()).throw(
                            AssertionError("must not reach apply")))

    out = core.bridge_edit(alpha, "Alpha", [bad_op])

    assert out["state"] == "validated"
    assert out["report"]["ok"] is False

    batches_dir = alpha.state_dir / "batches"
    if batches_dir.exists():
        journal_files = list(batches_dir.glob("*.json"))
        assert journal_files == [], (
            f"refused batch must leave no journal; found: {journal_files}"
        )


def test_backcompat_default_widget_type_warning_leaves_no_journal_when_refused(
    alpha, monkeypatch,
):
    """A batch refused after destructive-field hard-fail must leave no journal.

    The default_widget_type warning is only a warning (batch may succeed), but
    when combined with a destructive op unknown field the batch is refused
    before the apply loop — no journal file should be written.
    """
    # Combine a create_widget (no widget_type → default_widget_type warning)
    # with a delete op carrying an unknown field (hard refused).
    ops = [
        {"op": "create_widget", "screen": "UI/MainWindow", "name": "NewBtn"},
        {"op": "delete", "path": "UI/Screens/Foo", "name": "bogus_field"},
    ]
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    _patch_apply_ok(monkeypatch)

    out = core.bridge_edit(alpha, "Alpha", ops)

    # Destructive field hard-fail takes effect regardless of default_widget_type.
    assert out["state"] == "validated"
    assert out["report"]["ok"] is False

    batches_dir = alpha.state_dir / "batches"
    if batches_dir.exists():
        assert list(batches_dir.glob("*.json")) == [], (
            "refused batch must leave no journal"
        )


def test_op_timings_carry_per_op_outcome_detail(alpha, monkeypatch):
    """1.0.8 battle test: wire_event's `via`, attach_*'s `relative_sources` and
    reorder's `achieved` were computed by the bridge and then dropped by the
    batch loop, so a batch caller could not tell updated_in_place from created."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(
        {"ok": True, "op_count": 3, "strict": False, "errors": [], "warnings": []}))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    results = iter([
        {"ok": True, "via": "updated_in_place", "handler": "x"},
        {"ok": True, "relative_sources": ["../Level"]},
        {"ok": True},
    ])
    monkeypatch.setattr(core, "_apply_one_edit", lambda cfg, project, op: next(results))
    out = core.bridge_edit(alpha, "Alpha", _THREE_OPS)
    t = out["op_timings"]
    assert t[0]["detail"] == {"via": "updated_in_place"}
    assert t[1]["detail"] == {"relative_sources": ["../Level"]}
    assert "detail" not in t[2]


def test_gridlayout_create_warns_about_columns(alpha, monkeypatch):
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(
        {"ok": True, "op_count": 1, "strict": True, "errors": [], "warnings": []}))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    applied: list = []
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op) or {"ok": True})
    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "create_widget", "screen": "UI/MainWindow", "name": "G", "type": "GridLayout"},
    ], strict=True)
    codes = [w["code"] for w in out["report"]["warnings"]]
    assert "gridlayout_without_columns" in codes
    assert applied, "a warning, not a refusal - strict must not block it"


# ===========================================================================
# (9) batch_id is a filename component: separators / traversal refused
# ===========================================================================

_BAD_IDS = ["../../outside", "..\\..\\outside", "C:/Windows/win", "/etc/passwd",
            "a/b", "", "x" * 65, "id.json"]


@pytest.mark.parametrize("bad", [b for b in _BAD_IDS if b])
def test_status_refuses_traversal_batch_id(alpha, tmp_path, bad):
    # A readable journal-shaped file OUTSIDE batches/ must not be reachable.
    outside = alpha.state_dir / "outside.json"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text(json.dumps({"secret": 1}), encoding="utf-8")
    status = core.bridge_edit_status(alpha, "Alpha", bad)
    assert status["error"] == "invalid_batch_id"
    assert "secret" not in json.dumps(status)


@pytest.mark.parametrize("bad", [b for b in _BAD_IDS if b])
def test_continue_refuses_traversal_batch_id(alpha, bad):
    with pytest.raises(core.InvalidBatchId):
        core.bridge_edit_continue(alpha, "Alpha", bad)


@pytest.mark.parametrize("bad", [b for b in _BAD_IDS if b])
def test_apply_refuses_traversal_batch_id(alpha, monkeypatch, bad):
    _patch_apply_ok(monkeypatch)
    with pytest.raises(core.InvalidBatchId):
        core.bridge_edit(alpha, "Alpha", _TWO_OPS, batch_id=bad)
    assert not (alpha.state_dir / "outside.json").exists()


def test_load_journal_never_leaves_batches_dir(alpha):
    target = alpha.state_dir / "outside.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"secret": 1}), encoding="utf-8")
    assert core._load_batch_journal(alpha, "../outside") is None


def test_generated_and_hex_ids_are_valid():
    assert core._valid_batch_id("0123abcdef45")
    assert core._valid_batch_id("my_batch-1")
