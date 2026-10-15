"""#33: deleting the last NetLogic instance must not destroy the user's C# source."""
from __future__ import annotations

import json

import pytest

from service import core
from service.tests.conftest import make_project

_USER_CS = "public class R3Dt2Logic : BaseNetLogic { [ExportMethod] public void Mine() {} }\n"
_TEMPLATE_CS = "public class R3Dt2Logic : BaseNetLogic { public void Method1() {} }\n"


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    core.reset_bridge_cache()
    monkeypatch.setattr(core, "_NETLOGIC_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(core, "_NETLOGIC_SETTLE_POLL", 0.01)
    yield
    core.reset_bridge_cache()


@pytest.fixture
def proj(cfg, projects_root):
    p = make_project(projects_root, "Alpha")
    net = p / "ProjectFiles" / "NetSolution"
    net.mkdir(parents=True)
    f = net / "R3Dt2Logic.cs"
    f.write_text(_USER_CS)
    return cfg, f


def _bridge(monkeypatch, on_write=None):
    def fake(cfg, path, method="GET", timeout=5.0, **_kw):
        if path.startswith("/bridge/health"):
            return 200, json.dumps({"bridge_version": "1.0.9", "project": "Alpha",
                                    "model_loaded": True}).encode()
        if on_write is not None:
            on_write(path)
        return 200, json.dumps({"ok": True, "deleted": "x", "via": "delete",
                                "created_path": "Model/R3Dt2Logic",
                                "kind": "design_time"}).encode()
    monkeypatch.setattr(core, "_bridge_http", fake)


def test_delete_last_netlogic_instance_preserves_source_and_warns(proj, monkeypatch):
    cfg, f = proj
    _bridge(monkeypatch, lambda path: f.unlink() if "/node/delete" in path else None)
    out = core.bridge_delete_node(cfg, "Alpha", "Model/R3Dt2Logic")
    assert f.read_text() == _USER_CS
    assert out["ok"] is True
    assert out["warnings"][0]["code"] == "netlogic_source_preserved"
    assert out["netlogic_source"]["last_instance"] is True
    assert out["netlogic_source"]["preserved"] is True
    assert "R3Dt2Logic.cs" in out["warning"]


def test_delete_netlogic_still_used_elsewhere_keeps_file_no_warning(proj, monkeypatch):
    cfg, f = proj
    _bridge(monkeypatch)  # Studio keeps the file: another node uses the class
    out = core.bridge_delete_node(cfg, "Alpha", "Model/R3Dt2Logic")
    assert f.read_text() == _USER_CS
    assert "warnings" not in out and "netlogic_source" not in out


def test_delete_plain_node_without_matching_source_unchanged(proj, monkeypatch):
    cfg, f = proj
    _bridge(monkeypatch)
    out = core.bridge_delete_node(cfg, "Alpha", "Model/Folder1")
    assert out["ok"] is True and "warnings" not in out
    assert f.read_text() == _USER_CS


def test_create_netlogic_does_not_overwrite_user_source_with_template(proj, monkeypatch):
    cfg, f = proj
    _bridge(monkeypatch,
            lambda path: f.write_text(_TEMPLATE_CS) if "/model/netlogic" in path else None)
    out = core.bridge_create_netlogic(cfg, "Alpha", "Model", "R3Dt2Logic",
                                      kind="design_time")
    assert f.read_text() == _USER_CS
    assert out["warnings"][0]["code"] == "netlogic_source_preserved"


def test_create_netlogic_with_existing_source_untouched_is_quiet(proj, monkeypatch):
    cfg, f = proj
    _bridge(monkeypatch)
    out = core.bridge_create_netlogic(cfg, "Alpha", "Model", "R3Dt2Logic")
    assert f.read_text() == _USER_CS
    assert "warnings" not in out


def test_batch_delete_surfaces_warning(proj, monkeypatch):
    cfg, f = proj
    _bridge(monkeypatch, lambda path: f.unlink() if "/node/delete" in path else None)
    out: dict = {}
    res = core.bridge_delete_node(cfg, "Alpha", "Model/R3Dt2Logic")
    core._surface_op_warnings(out, 0, {"op": "delete"}, res)
    assert out["warnings"][0]["op_index"] == 0
    assert out["warnings"][0]["code"] == "netlogic_source_preserved"
    assert f.read_text() == _USER_CS


# ---- PRD 0033: timeout-then-node_not_found, restore after a raised error ----

_NOT_FOUND = {"error": {"code": "node_not_found", "message": "no node at: x"}}
_EDITED_CS = _USER_CS + "// edited after creation\n"


@pytest.fixture
def edited(proj, monkeypatch):
    """A project whose NetLogic .cs was edited after creation."""
    cfg, f = proj
    f.write_text(_EDITED_CS)
    monkeypatch.setattr(core, "_DELETE_VERIFY_DELAY", 0.0)
    return cfg, f


def _timeout_bridge(monkeypatch, f, *, node_gone=True, post_calls=None):
    """First delete POST times out (Studio deleted the node + the .cs meanwhile);
    any later delete POST answers 404 node_not_found."""
    def fake(cfg, path, method="GET", timeout=5.0, **_kw):
        if path.startswith("/bridge/health"):
            return 200, json.dumps({"bridge_version": "1.0.9", "project": "Alpha",
                                    "model_loaded": True}).encode()
        if path.startswith("/bridge/node/delete"):
            n = len(post_calls) if post_calls is not None else 0
            if post_calls is not None:
                post_calls.append(timeout)
            if n == 0:
                f.unlink(missing_ok=True)
                raise core.BridgeWriteTimeout("timed out")
            return 404, json.dumps(_NOT_FOUND).encode()
        if path.startswith("/bridge/nodes"):
            if node_gone:
                return 404, json.dumps(_NOT_FOUND).encode()
            return 200, json.dumps({"node_class": "NetLogic", "properties": []}).encode()
        return 200, b"{}"
    monkeypatch.setattr(core, "_bridge_http", fake)


def test_delete_timeout_then_node_not_found_is_success_and_preserves_source(
        edited, monkeypatch):
    cfg, f = edited
    posts: list = []
    _timeout_bridge(monkeypatch, f, node_gone=True, post_calls=posts)
    out = core.bridge_delete_node(cfg, "Alpha", "Model/R3Dt2Logic")
    assert out["ok"] is True
    assert out["verified_after_timeout"] is True
    assert len(posts) == 1, "a verified-gone node must not be re-sent"
    assert posts[0] >= 30, "delete must use a long timeout"
    assert f.read_text() == _EDITED_CS
    codes = [w["code"] for w in out["warnings"]]
    assert "netlogic_source_preserved" in codes
    assert out["netlogic_source"]["preserved"] is True


def test_delete_timeout_retry_node_not_found_when_verify_inconclusive(
        edited, monkeypatch):
    """Node still listed after the timeout -> one re-send; its node_not_found
    means the first attempt landed, so the delete is a success."""
    cfg, f = edited
    posts: list = []
    _timeout_bridge(monkeypatch, f, node_gone=False, post_calls=posts)
    out = core.bridge_delete_node(cfg, "Alpha", "Model/R3Dt2Logic")
    assert out["ok"] is True and out["verified_after_timeout"] is True
    assert len(posts) == 2
    assert f.read_text() == _EDITED_CS


def test_delete_node_not_found_on_first_response_is_still_an_error(edited, monkeypatch):
    cfg, f = edited
    def fake(cfg, path, method="GET", timeout=5.0, **_kw):
        if path.startswith("/bridge/health"):
            return 200, json.dumps({"bridge_version": "1.0.9", "project": "Alpha",
                                    "model_loaded": True}).encode()
        return 404, json.dumps(_NOT_FOUND).encode()
    monkeypatch.setattr(core, "_bridge_http", fake)
    with pytest.raises(core.BridgeWriteFailed, match="node_not_found"):
        core.bridge_delete_node(cfg, "Alpha", "Model/R3Dt2Logic")
    assert f.read_text() == _EDITED_CS


def test_delete_timeout_node_still_present_raises_and_restores_snapshot(
        edited, monkeypatch):
    """Snapshot restore after a raised error: the delete never lands, Studio
    clobbers the .cs meanwhile; the error propagates but the source is back."""
    cfg, f = edited
    def fake(cfg, path, method="GET", timeout=5.0, **_kw):
        if path.startswith("/bridge/health"):
            return 200, json.dumps({"bridge_version": "1.0.9", "project": "Alpha",
                                    "model_loaded": True}).encode()
        if path.startswith("/bridge/node/delete"):
            f.write_text(_TEMPLATE_CS)
            raise core.BridgeWriteTimeout("timed out")
        return 200, json.dumps({"node_class": "NetLogic", "properties": []}).encode()
    monkeypatch.setattr(core, "_bridge_http", fake)
    with pytest.raises(core.BridgeWriteTimeout) as ei:
        core.bridge_delete_node(cfg, "Alpha", "Model/R3Dt2Logic")
    assert f.read_text() == _EDITED_CS
    assert ei.value.netlogic_source["preserved"] is True


def test_delete_bridge_error_restores_snapshot(edited, monkeypatch):
    cfg, f = edited
    def fake(cfg, path, method="GET", timeout=5.0, **_kw):
        if path.startswith("/bridge/health"):
            return 200, json.dumps({"bridge_version": "1.0.9", "project": "Alpha",
                                    "model_loaded": True}).encode()
        f.unlink(missing_ok=True)
        return 500, json.dumps({"error": {"code": "boom", "message": "x"}}).encode()
    monkeypatch.setattr(core, "_bridge_http", fake)
    with pytest.raises(core.BridgeWriteFailed):
        core.bridge_delete_node(cfg, "Alpha", "Model/R3Dt2Logic")
    assert f.read_text() == _EDITED_CS


def test_edit_op_delete_timeout_then_node_not_found_is_succeeded(edited, monkeypatch):
    """optix_bridge_edit op=delete: state succeeded, applied 1, source kept."""
    cfg, f = edited
    _timeout_bridge(monkeypatch, f, node_gone=True, post_calls=[])
    report = {"ok": True, "op_count": 1, "strict": False, "errors": [], "warnings": []}
    monkeypatch.setattr(core, "_bridge_post_body", lambda *a, **k: (200, dict(report)))
    out = core.bridge_edit(cfg, "Alpha", [{"op": "delete", "path": "Model/R3Dt2Logic"}])
    assert out["state"] == "succeeded" and out["applied"] == 1
    assert f.read_text() == _EDITED_CS
    assert any(w["code"] == "netlogic_source_preserved" for w in out["warnings"])


def test_next_create_netlogic_does_not_regenerate_template_over_preserved_source(
        edited, monkeypatch):
    cfg, f = edited
    _timeout_bridge(monkeypatch, f, node_gone=True, post_calls=[])
    core.bridge_delete_node(cfg, "Alpha", "Model/R3Dt2Logic")
    assert f.read_text() == _EDITED_CS
    _bridge(monkeypatch,
            lambda path: f.write_text(_TEMPLATE_CS) if "/model/netlogic" in path else None)
    out = core.bridge_create_netlogic(cfg, "Alpha", "Model", "R3Dt2Logic",
                                      kind="design_time")
    assert f.read_text() == _EDITED_CS
    assert out["netlogic_source"]["preserved"] is True
    assert out["warnings"][0]["code"] == "netlogic_source_preserved"


def test_delete_design_time_netlogic_timeout_preserves_source(edited, monkeypatch):
    cfg, f = edited
    _timeout_bridge(monkeypatch, f, node_gone=True, post_calls=[])
    out = core.bridge_delete_node(cfg, "Alpha", "UI/Logics/R3Dt2Logic")
    assert out["ok"] is True and f.read_text() == _EDITED_CS


# ---- PRD 0034 #37 (ftx-mcp-aqs.2): stable, atomic snapshot restore ----------

def _studio_rewrite(f) -> None:
    """Studio's late write-back: truncate, then write at an offset (the file is
    left NUL-padded / mixed, never a clean template)."""
    with open(f, "r+b") as fh:
        fh.truncate(0)
        fh.seek(7)
        fh.write(_TEMPLATE_CS.encode())


@pytest.fixture
def fast_restore(monkeypatch):
    monkeypatch.setattr(core, "_NETLOGIC_MAX_WAIT_SECONDS", 2.0)


def _hook_replace(monkeypatch, after):
    """Wrap os.replace as used by core; ``after(n, dst)`` runs after the n-th
    real replace (n starts at 1). Returns the list of (src, dst) calls."""
    calls: list = []
    real = core.os.replace

    def fake(src, dst):
        real(src, dst)
        calls.append((core.Path(src), core.Path(dst)))
        after(len(calls), core.Path(dst))
    monkeypatch.setattr(core.os, "replace", fake)
    return calls


def test_restore_uses_same_dir_temp_and_os_replace_and_leaves_no_temp(
        edited, fast_restore, monkeypatch):
    cfg, f = edited
    calls = _hook_replace(monkeypatch, lambda n, dst: None)
    _bridge(monkeypatch, lambda path: f.unlink() if "/node/delete" in path else None)
    out = core.bridge_delete_node(cfg, "Alpha", "Model/R3Dt2Logic")
    assert out["netlogic_source"]["preserved"] is True
    assert len(calls) == 1
    src, dst = calls[0]
    assert src.parent == dst.parent == f.parent and src != dst and dst == f
    assert f.read_bytes() == _EDITED_CS.encode()
    assert b"\x00" not in f.read_bytes()
    assert [p.name for p in f.parent.iterdir()] == [f.name]


def test_late_studio_rewrite_during_restore_is_re_restored_byte_identical(
        edited, fast_restore, monkeypatch):
    """Studio truncates + offset-writes the file right after our first restore;
    the bounded re-restore puts the exact snapshot back."""
    cfg, f = edited
    calls = _hook_replace(
        monkeypatch, lambda n, dst: _studio_rewrite(dst) if n == 1 else None)
    _bridge(monkeypatch, lambda path: f.unlink() if "/node/delete" in path else None)
    out = core.bridge_delete_node(cfg, "Alpha", "Model/R3Dt2Logic")
    assert len(calls) == 2
    assert f.read_bytes() == _EDITED_CS.encode()
    assert b"\x00" not in f.read_bytes()
    assert out["netlogic_source"]["preserved"] is True
    assert out["warnings"][0]["code"] == "netlogic_source_preserved"
    assert not (f.parent.parent / ".ftx-mcp-backups").exists()


def test_late_studio_rewrite_after_delete_is_waited_out_then_restored(
        edited, fast_restore, monkeypatch):
    """Studio's write-back lands a few polls after the delete returned: the
    stability wait sees the file change, waits for it to settle, then restores."""
    import threading
    cfg, f = edited
    monkeypatch.setattr(core, "_NETLOGIC_SETTLE_SECONDS", 0.15)
    monkeypatch.setattr(core, "_NETLOGIC_SETTLE_POLL", 0.01)

    def on_write(path):
        if "/node/delete" in path:
            threading.Timer(0.05, lambda: _studio_rewrite(f)).start()
    _bridge(monkeypatch, on_write)
    out = core.bridge_delete_node(cfg, "Alpha", "Model/R3Dt2Logic")
    assert f.read_bytes() == _EDITED_CS.encode()
    assert out["netlogic_source"]["preserved"] is True


def test_persistent_mismatch_writes_backup_and_restore_failed_warning(
        edited, fast_restore, monkeypatch):
    cfg, f = edited
    calls = _hook_replace(monkeypatch,
                          lambda n, dst: _studio_rewrite(dst) if dst == f else None)
    _bridge(monkeypatch, lambda path: f.unlink() if "/node/delete" in path else None)
    out = core.bridge_delete_node(cfg, "Alpha", "Model/R3Dt2Logic")
    assert len([c for c in calls if c[1] == f]) == core._NETLOGIC_RESTORE_ATTEMPTS
    bdir = f.parent.parent / ".ftx-mcp-backups"
    baks = list(bdir.glob("R3Dt2Logic.*.cs.bak"))
    assert len(baks) == 1
    assert baks[0].read_bytes() == _EDITED_CS.encode()
    assert out["netlogic_source"]["preserved"] is False
    assert out["netlogic_source"]["backup"] == str(baks[0])
    w = [w for w in out["warnings"] if w["code"] == "netlogic_source_restore_failed"]
    assert len(w) == 1 and str(baks[0]) in w[0]["message"]
    assert w[0]["backup"] == str(baks[0])


# ---- #37 root cause: create_netlogic waits for Studio's template write ----

_TPL = "public class NewLogic : BaseNetLogic {\n" + "    // template line\n" * 40 + "}\n"


@pytest.fixture
def newproj(cfg, projects_root):
    p = make_project(projects_root, "Alpha")
    net = p / "ProjectFiles" / "NetSolution"
    net.mkdir(parents=True)
    return cfg, net / "NewLogic.cs"


def _create_bridge(monkeypatch, on_create):
    def fake(cfg, path, method="GET", timeout=5.0, **_kw):
        if path.startswith("/bridge/health"):
            return 200, json.dumps({"bridge_version": "1.0.9", "project": "Alpha",
                                    "model_loaded": True}).encode()
        if "/model/netlogic" in path:
            on_create()
        return 200, json.dumps({"ok": True, "created_path": "Model/NewLogic",
                                "kind": "design_time"}).encode()
    monkeypatch.setattr(core, "_bridge_http", fake)


@pytest.mark.parametrize("kind", [None, "design_time"])
def test_create_netlogic_waits_for_late_positioned_template_write(
        newproj, monkeypatch, kind):
    """Studio keeps writing the template (at its own offset, via an open handle)
    AFTER the create call returned. create_netlogic must not return until the
    file has been quiet for the window."""
    import threading
    import time as _t
    cfg, f = newproj
    monkeypatch.setattr(core, "_NETLOGIC_SETTLE_SECONDS", 0.4)
    monkeypatch.setattr(core, "_NETLOGIC_TEMPLATE_MAX_WAIT_SECONDS", 5.0)
    done = threading.Event()
    state = {"finished_at": None}

    def writer():
        half = len(_TPL) // 2
        _t.sleep(0.1)
        fh = open(f, "wb")
        fh.write(_TPL[:half].encode())      # first half lands...
        fh.flush()
        for off in (half, half + 20, len(_TPL) - 5):  # ...then positioned writes
            _t.sleep(0.15)
            fh.seek(off)
            fh.write(_TPL.encode()[off:off + 20])
            fh.flush()
        _t.sleep(0.15)
        fh.seek(0)
        fh.write(_TPL.encode())             # final full write
        fh.flush()
        fh.close()
        state["finished_at"] = _t.monotonic()
        done.set()

    started: list = []

    def on_create():
        t = threading.Thread(target=writer, daemon=True)
        started.append(t)
        t.start()

    _create_bridge(monkeypatch, on_create)
    out = core.bridge_create_netlogic(cfg, "Alpha", "Model", "NewLogic", kind=kind)
    returned_at = _t.monotonic()
    assert done.is_set(), "create returned while Studio was still writing the template"
    assert returned_at >= state["finished_at"]
    assert f.read_text() == _TPL
    assert out["netlogic_template"]["ready"] is True
    assert "warnings" not in out
    started[0].join()


def test_create_netlogic_template_write_pending_on_timeout(newproj, monkeypatch):
    """A file that never settles within the bounded wait yields the
    netlogic_template_write_pending warning and is not claimed ready."""
    import threading
    import time as _t
    cfg, f = newproj
    monkeypatch.setattr(core, "_NETLOGIC_SETTLE_SECONDS", 0.2)
    monkeypatch.setattr(core, "_NETLOGIC_TEMPLATE_MAX_WAIT_SECONDS", 0.5)
    stop = threading.Event()

    def churn():
        n = 0
        while not stop.is_set():
            n += 1
            f.write_text(_TPL + f"// {n}\n")
            _t.sleep(0.02)

    th = threading.Thread(target=churn, daemon=True)
    _create_bridge(monkeypatch, th.start)
    t0 = _t.monotonic()
    try:
        out = core.bridge_create_netlogic(cfg, "Alpha", "Model", "NewLogic")
    finally:
        stop.set()
        th.join()
    assert _t.monotonic() - t0 < 3.0  # bounded
    assert out["warnings"][0]["code"] == "netlogic_template_write_pending"
    assert out["netlogic_template"]["ready"] is False


def test_create_netlogic_missing_template_is_pending_not_ready(newproj, monkeypatch):
    cfg, f = newproj
    _create_bridge(monkeypatch, lambda: None)
    out = core.bridge_create_netlogic(cfg, "Alpha", "Model", "NewLogic")
    assert out["warnings"][0]["code"] == "netlogic_template_write_pending"
    assert out["netlogic_template"] == {
        "file": "ProjectFiles/NetSolution/NewLogic.cs", "ready": False,
        "waited_seconds": out["netlogic_template"]["waited_seconds"],
        "reason": "missing"}


def test_create_netlogic_empty_template_is_pending(newproj, monkeypatch):
    cfg, f = newproj
    _create_bridge(monkeypatch, lambda: f.write_bytes(b""))
    out = core.bridge_create_netlogic(cfg, "Alpha", "Model", "NewLogic")
    assert out["warnings"][0]["code"] == "netlogic_template_write_pending"
    assert out["netlogic_template"]["reason"] == "empty"


def test_create_netlogic_settled_template_is_ready_and_quiet(newproj, monkeypatch):
    cfg, f = newproj
    _create_bridge(monkeypatch, lambda: f.write_text(_TPL))
    out = core.bridge_create_netlogic(cfg, "Alpha", "Model", "NewLogic")
    assert out["netlogic_template"]["ready"] is True
    assert "warnings" not in out


def test_batch_surface_carries_template_pending_warning(newproj, monkeypatch):
    cfg, f = newproj
    _create_bridge(monkeypatch, lambda: None)
    res = core.bridge_create_netlogic(cfg, "Alpha", "Model", "NewLogic")
    out: dict = {}
    core._surface_op_warnings(out, 2, {"op": "create_netlogic"}, res)
    assert out["warnings"][0]["code"] == "netlogic_template_write_pending"


# ---- ftx-mcp-phv.2: backup-first, byte-verify at return, late guard ---------

def _delete_unlinks(monkeypatch, f):
    _bridge(monkeypatch, lambda path: f.unlink() if "/node/delete" in path else None)


def _baks(f):
    bdir = f.parent.parent / ".ftx-mcp-backups"
    return [p for p in bdir.glob("R3Dt2Logic.*.cs.bak")
            if ".drifted." not in p.name] if bdir.exists() else []


def test_backup_is_written_before_delete_post_and_removed_after_verify(
        edited, fast_restore, monkeypatch):
    cfg, f = edited
    seen: dict = {}

    def on_write(path):
        if "/node/delete" in path:
            seen["baks"] = [p.read_bytes() for p in _baks(f)]
            f.unlink()
    _bridge(monkeypatch, on_write)
    out = core.bridge_delete_node(cfg, "Alpha", "Model/R3Dt2Logic")
    assert seen["baks"] == [_EDITED_CS.encode()], "backup must exist before the POST"
    assert out["netlogic_source"]["preserved"] is True
    assert _baks(f) == [], "backup removed only after a successful final verify"


def test_untouched_file_leaves_no_backup_behind(edited, fast_restore, monkeypatch):
    cfg, f = edited
    _bridge(monkeypatch)
    out = core.bridge_delete_node(cfg, "Alpha", "Model/R3Dt2Logic")
    assert "netlogic_source" not in out
    assert _baks(f) == []


def test_late_rewrite_after_stability_window_before_return_is_not_preserved_true(
        edited, fast_restore, monkeypatch):
    """Studio rewrites the file just after the stability window closed but before
    the call returns: the return-time byte compare catches it, the file is
    restored again and `preserved` is only true because the bytes match."""
    cfg, f = edited
    real_wait = core._netlogic_wait_stable
    waits: list = []

    def wait(full, *a, **k):
        sig = real_wait(full, *a, **k)
        waits.append(1)
        if len(waits) == 2:  # right after the post-restore settle
            _studio_rewrite(f)
        return sig
    monkeypatch.setattr(core, "_netlogic_wait_stable", wait)
    calls = _hook_replace(monkeypatch, lambda n, dst: None)
    _delete_unlinks(monkeypatch, f)
    out = core.bridge_delete_node(cfg, "Alpha", "Model/R3Dt2Logic")
    assert f.read_bytes() == _EDITED_CS.encode()
    assert len([c for c in calls if c[1] == f]) == 2
    assert out["netlogic_source"]["preserved"] is True
    assert _baks(f) == []


def test_late_rewrite_that_never_stops_reports_not_preserved_and_names_backup(
        edited, fast_restore, monkeypatch):
    cfg, f = edited
    real_wait = core._netlogic_wait_stable

    def wait(full, *a, **k):
        sig = real_wait(full, *a, **k)
        f.write_bytes(b"\x00" * 7 + _TEMPLATE_CS.encode())  # after every settle
        return sig
    monkeypatch.setattr(core, "_netlogic_wait_stable", wait)
    _delete_unlinks(monkeypatch, f)
    out = core.bridge_delete_node(cfg, "Alpha", "Model/R3Dt2Logic")
    assert out["netlogic_source"]["preserved"] is False
    baks = _baks(f)
    assert len(baks) == 1 and baks[0].read_bytes() == _EDITED_CS.encode()
    w = [w for w in out["warnings"] if w["code"] == "netlogic_source_restore_failed"]
    assert len(w) == 1 and str(baks[0]) in w[0]["message"]


def test_positioned_write_extending_file_with_nuls_is_restored(
        edited, fast_restore, monkeypatch):
    """Studio seeks past EOF and writes: the file is the snapshot + NULs + tail.
    It starts with the snapshot but is longer, so it is modified."""
    cfg, f = edited

    def extend(n, dst):
        if n == 1:
            with open(dst, "r+b") as fh:
                fh.seek(len(_EDITED_CS) + 16)
                fh.write(b"\n")
    _hook_replace(monkeypatch, extend)
    _delete_unlinks(monkeypatch, f)
    out = core.bridge_delete_node(cfg, "Alpha", "Model/R3Dt2Logic")
    data = f.read_bytes()
    assert data == _EDITED_CS.encode() and b"\x00" not in data
    assert out["netlogic_source"]["preserved"] is True


def test_classify_treats_nuls_and_snapshot_prefix_as_modified(tmp_path):
    f = tmp_path / "A.cs"
    snap = b"class A {}\n"
    f.write_bytes(snap)
    assert core._netlogic_classify(f, snap) is None
    f.write_bytes(snap + b"\x00" * 8)
    assert core._netlogic_classify(f, snap) == "modified"
    f.write_bytes(snap + b"// more\n")
    assert core._netlogic_classify(f, snap) == "modified"
    f.write_bytes(snap[:5] + b"\x00" + snap[6:])
    assert core._netlogic_classify(f, snap) == "modified"
    f.unlink()
    assert core._netlogic_classify(f, snap) == "missing"


def test_settle_waits_while_bridge_reports_busy_and_is_bounded(
        edited, fast_restore, monkeypatch):
    """The settled signal is gated on the bridge, not just a fixed window; the
    wait is bounded by _NETLOGIC_MAX_WAIT_SECONDS."""
    import time as _t
    cfg, f = edited
    monkeypatch.setattr(core, "_NETLOGIC_MAX_WAIT_SECONDS", 0.6)
    state = {"busy_until": _t.monotonic() + 0.4, "health": 0}

    def fake(cfg, path, method="GET", timeout=5.0, **_kw):
        if path.startswith("/bridge/health"):
            state["health"] += 1
            loaded = _t.monotonic() >= state["busy_until"]
            return 200, json.dumps({"bridge_version": "1.0.9", "project": "Alpha",
                                    "model_loaded": loaded}).encode()
        return 200, json.dumps({"ok": True}).encode()
    monkeypatch.setattr(core, "_bridge_http", fake)
    t0 = _t.monotonic()
    core._netlogic_wait_stable(f, core._netlogic_bridge_cfg(cfg, "Alpha"))
    assert 0.4 <= _t.monotonic() - t0 < 2.0

    state["busy_until"] = _t.monotonic() + 3600  # never finishes: still bounded
    t0 = _t.monotonic()
    core._netlogic_wait_stable(f, core._netlogic_bridge_cfg(cfg, "Alpha"))
    assert 0.5 <= _t.monotonic() - t0 < 2.0


def test_settle_waits_for_build_tree_quiescence(edited, fast_restore, monkeypatch):
    """A compile touching NetSolution/obj restarts the window even though the
    .cs itself is quiet."""
    import threading
    import time as _t
    cfg, f = edited
    monkeypatch.setattr(core, "_NETLOGIC_SETTLE_SECONDS", 0.2)
    obj = f.parent / "obj"
    obj.mkdir()
    stop = threading.Event()

    def compile_churn():
        n = 0
        while not stop.is_set() and n < 10:
            n += 1
            (obj / "out.dll").write_bytes(b"x" * n)
            _t.sleep(0.05)
    th = threading.Thread(target=compile_churn)
    t0 = _t.monotonic()
    th.start()
    core._netlogic_wait_stable(f)
    stop.set()
    th.join()
    assert _t.monotonic() - t0 >= 0.5 + 0.2 - 0.1


def test_rewrite_after_return_is_caught_by_next_call_guard(
        edited, fast_restore, monkeypatch):
    cfg, f = edited
    _delete_unlinks(monkeypatch, f)
    out = core.bridge_delete_node(cfg, "Alpha", "Model/R3Dt2Logic")
    assert out["netlogic_source"]["preserved"] is True
    assert "netlogic_source_restored_late" not in [w["code"] for w in out["warnings"]]
    # Studio's deferred write-back lands after the call returned (NUL-padded).
    f.write_bytes(b"\x00" * 5 + _TEMPLATE_CS.encode())
    _bridge(monkeypatch)
    nxt = core.bridge_create_netlogic(cfg, "Alpha", "Model", "OtherLogic")
    assert f.read_bytes() == _EDITED_CS.encode()
    late = [w for w in nxt["warnings"] if w["code"] == "netlogic_source_restored_late"]
    assert len(late) == 1 and late[0]["file"].endswith("R3Dt2Logic.cs")
    # the rewritten content is kept so a deliberate edit is never lost
    assert core.Path(late[0]["drifted_copy"]).read_bytes() == (
        b"\x00" * 5 + _TEMPLATE_CS.encode())
    assert _baks(f) == []
    # re-checked clean now: no second warning
    assert core._netlogic_late_guard(cfg, "Alpha") == []


def test_late_guard_restores_deleted_file_and_attaches_on_bridge_write(
        edited, fast_restore, monkeypatch):
    cfg, f = edited
    _delete_unlinks(monkeypatch, f)
    core.bridge_delete_node(cfg, "Alpha", "Model/R3Dt2Logic")
    f.unlink()
    _bridge(monkeypatch)
    res = core._bridge_write(cfg, "Alpha", "set_property", "/bridge/node/property",
                             {"path": "Model/X", "value": "1"})
    assert f.read_bytes() == _EDITED_CS.encode()
    assert [w["code"] for w in res["warnings"]] == ["netlogic_source_restored_late"]


def test_late_guard_ignores_restores_older_than_60s(edited, fast_restore, monkeypatch):
    cfg, f = edited
    _delete_unlinks(monkeypatch, f)
    core.bridge_delete_node(cfg, "Alpha", "Model/R3Dt2Logic")
    assert core._NETLOGIC_LATE_GUARD_SECONDS == 60.0
    monkeypatch.setattr(core, "_NETLOGIC_LATE_GUARD_SECONDS", 0.0)
    f.write_bytes(b"\x00" * 5 + _TEMPLATE_CS.encode())
    assert core._netlogic_late_guard(cfg, "Alpha") == []
    assert f.read_bytes() == b"\x00" * 5 + _TEMPLATE_CS.encode()


def test_late_guard_failure_names_backup(edited, fast_restore, monkeypatch):
    cfg, f = edited
    _delete_unlinks(monkeypatch, f)
    core.bridge_delete_node(cfg, "Alpha", "Model/R3Dt2Logic")
    f.write_bytes(b"\x00" + _TEMPLATE_CS.encode())
    real_wait = core._netlogic_wait_stable

    def wait(full, *a, **k):
        sig = real_wait(full, *a, **k)
        f.write_bytes(b"\x00" * 7 + _TEMPLATE_CS.encode())
        return sig
    monkeypatch.setattr(core, "_netlogic_wait_stable", wait)
    ws = core._netlogic_late_guard(cfg, "Alpha")
    assert [w["code"] for w in ws] == ["netlogic_source_restore_failed"]
    baks = _baks(f)
    assert len(baks) == 1 and str(baks[0]) in ws[0]["message"]


# ---- ftx-mcp-czz.1: late guard restores only the corruption signature -------

def test_late_guard_leaves_clean_edit_alone(edited, fast_restore, monkeypatch):
    cfg, f = edited
    _delete_unlinks(monkeypatch, f)
    core.bridge_delete_node(cfg, "Alpha", "Model/R3Dt2Logic")
    f.write_text(_TEMPLATE_CS)
    assert core._netlogic_late_guard(cfg, "Alpha") == []
    assert f.read_text() == _TEMPLATE_CS
    assert [p for p in f.parent.rglob("*") if ".drifted" in p.name] == []
    assert [p for p in f.parent.parent.rglob("*.drifted*")] == []


def test_late_guard_restores_nul_padded_file(edited, fast_restore, monkeypatch):
    cfg, f = edited
    _delete_unlinks(monkeypatch, f)
    core.bridge_delete_node(cfg, "Alpha", "Model/R3Dt2Logic")
    f.write_bytes(_EDITED_CS.encode() + b"\x00" * 64)
    ws = core._netlogic_late_guard(cfg, "Alpha")
    assert [w["code"] for w in ws] == ["netlogic_source_restored_late"]
    assert f.read_bytes() == _EDITED_CS.encode()


def test_late_guard_restores_snapshot_plus_appended_tail(
        edited, fast_restore, monkeypatch):
    cfg, f = edited
    _delete_unlinks(monkeypatch, f)
    core.bridge_delete_node(cfg, "Alpha", "Model/R3Dt2Logic")
    f.write_bytes(_EDITED_CS.encode() + b"// tail\n")
    ws = core._netlogic_late_guard(cfg, "Alpha")
    assert [w["code"] for w in ws] == ["netlogic_source_restored_late"]
    assert f.read_bytes() == _EDITED_CS.encode()


def test_late_guard_drops_legitimate_edit_from_registry(
        edited, fast_restore, monkeypatch):
    cfg, f = edited
    _delete_unlinks(monkeypatch, f)
    core.bridge_delete_node(cfg, "Alpha", "Model/R3Dt2Logic")
    assert core._RECENT_NETLOGIC_RESTORES
    f.write_text(_TEMPLATE_CS)
    assert core._netlogic_late_guard(cfg, "Alpha") == []
    assert not core._RECENT_NETLOGIC_RESTORES
    assert f.read_text() == _TEMPLATE_CS


# ---- ftx-mcp-czz.2: deliberate source changes clear the registry entry ------

_A_CS = _EDITED_CS
_B_CS = _EDITED_CS + "// B: written on disk after the delete\n"  # looks like drift


def _registered(f):
    return str(f) in core._RECENT_NETLOGIC_RESTORES


def _delete_and_register(cfg, f, monkeypatch):
    _delete_unlinks(monkeypatch, f)
    core.bridge_delete_node(cfg, "Alpha", "Model/R3Dt2Logic")
    assert _registered(f)


def test_recreate_same_class_clears_registry_entry_before_create_runs(
        edited, fast_restore, monkeypatch):
    cfg, f = edited
    _delete_and_register(cfg, f, monkeypatch)
    seen: dict = {}

    def on_write(path):
        if "/model/netlogic" in path:
            seen["registered_at_post"] = _registered(f)
    _bridge(monkeypatch, on_write)
    core.bridge_create_netlogic(cfg, "Alpha", "Model", "R3Dt2Logic")
    assert seen["registered_at_post"] is False


def test_recreate_other_class_keeps_registry_entry(edited, fast_restore, monkeypatch):
    cfg, f = edited
    _delete_and_register(cfg, f, monkeypatch)
    _bridge(monkeypatch)
    core.bridge_create_netlogic(cfg, "Alpha", "Model", "OtherLogic")
    assert _registered(f)


def test_issue_42_sequence_does_not_revert_b(edited, fast_restore, monkeypatch):
    """create, write A, delete, write B on disk, re-create within 60 s."""
    cfg, f = edited  # A is on disk (created earlier, then edited)
    _delete_and_register(cfg, f, monkeypatch)
    assert f.read_text() == _A_CS
    f.write_text(_B_CS)
    _bridge(monkeypatch)
    out = core.bridge_create_netlogic(cfg, "Alpha", "Model", "R3Dt2Logic")
    assert f.read_text() == _B_CS
    assert "warnings" not in out
    assert core._netlogic_late_guard(cfg, "Alpha") == []
    assert f.read_text() == _B_CS


def test_delete_node_clears_registry_entry(edited, fast_restore, monkeypatch):
    cfg, f = edited
    _delete_and_register(cfg, f, monkeypatch)
    f.write_text(_B_CS)
    seen: dict = {}
    _bridge(monkeypatch, lambda p: seen.update(reg=_registered(f))
            if "/node/delete" in p else None)
    core.bridge_delete_node(cfg, "Alpha", "Model/R3Dt2Logic")
    assert seen["reg"] is False
    assert f.read_text() == _B_CS


def test_batch_delete_clears_registry_entry(edited, fast_restore, monkeypatch):
    cfg, f = edited
    _delete_and_register(cfg, f, monkeypatch)
    seen: dict = {}
    monkeypatch.setattr(core, "bridge_delete_node",
                        lambda c, p, path: seen.update(reg=_registered(f)) or {"ok": True})
    core._apply_one_edit(cfg, "Alpha", {"op": "delete", "path": "Model/R3Dt2Logic"})
    assert seen["reg"] is False


def test_deploy_source_write_clears_registry_entry(edited, fast_restore, monkeypatch):
    from service.tests.conftest import make_export_handler, make_fake_runner
    cfg, f = edited
    _delete_and_register(cfg, f, monkeypatch)
    monkeypatch.setattr(core, "_tcp_probe", lambda *a, **kw: True)

    class _Rt:
        def stop(self, *_a): pass
        def start(self, *_a): pass
    req = core.DeployRequest(
        edits=[{"path": "ProjectFiles/NetSolution/R3Dt2Logic.cs", "content": _B_CS}],
        commit_message="czz.2", run_after_deploy=False)
    core.deploy(cfg, "Alpha", req, runner=make_fake_runner(make_export_handler()),
                runtime=_Rt())
    assert not _registered(f)
    assert f.read_text() == _B_CS


def test_project_open_clears_registry_entry(edited, fast_restore, monkeypatch):
    cfg, f = edited
    _delete_and_register(cfg, f, monkeypatch)
    core.project_open(cfg, "Alpha", wait_seconds=0)
    assert not _registered(f)


def test_project_close_clears_registry_entry(edited, fast_restore, monkeypatch):
    cfg, f = edited
    _delete_and_register(cfg, f, monkeypatch)
    core._close_launched_studio(2 ** 22 + 12345)  # no such pid
    assert not _registered(f)


# ---- ftx-mcp-czz.3: create over an existing .cs backs up and warns ----------

def test_create_netlogic_backs_up_existing_source_before_post(
        edited, fast_restore, monkeypatch):
    cfg, f = edited
    seen: dict = {}

    def on_write(path):
        if "/model/netlogic" in path:
            seen["baks"] = [p.read_bytes() for p in _baks(f)]
    _bridge(monkeypatch, on_write)
    core.bridge_create_netlogic(cfg, "Alpha", "Model", "R3Dt2Logic")
    assert seen["baks"] == [_EDITED_CS.encode()]


def test_create_netlogic_warns_source_existed_with_backup_when_changed(
        edited, fast_restore, monkeypatch):
    cfg, f = edited
    _bridge(monkeypatch,
            lambda path: f.write_text(_TEMPLATE_CS) if "/model/netlogic" in path else None)
    out = core.bridge_create_netlogic(cfg, "Alpha", "Model", "R3Dt2Logic")
    w = [w for w in out["warnings"] if w["code"] == "netlogic_source_existed"]
    assert len(w) == 1
    baks = _baks(f)
    assert len(baks) == 1 and baks[0].read_bytes() == _EDITED_CS.encode()
    assert w[0]["backup"] == str(baks[0]) and str(baks[0]) in w[0]["message"]


def test_create_netlogic_no_source_existed_warning_when_unchanged(
        edited, fast_restore, monkeypatch):
    cfg, f = edited
    _bridge(monkeypatch)
    out = core.bridge_create_netlogic(cfg, "Alpha", "Model", "R3Dt2Logic")
    assert "warnings" not in out
    assert _baks(f) == []
