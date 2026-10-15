"""Creating ops are non-idempotent and settle a timeout by reading the model
(PRD 0056, ftx-mcp-w3d.4).

Offline: a stateful fake bridge that times out once and then answers
``name_exists``. No creating op may be re-sent after a timeout; the would-be
path is read back instead.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from service import core
from service.tests.conftest import make_project


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    core.reset_bridge_cache()
    core._RECENT_CREATE_TIMEOUTS.clear()
    monkeypatch.setattr(core, "CREATE_VERIFY_DELAY", 0)
    monkeypatch.setattr(core, "_NETLOGIC_VERIFY_DELAY", 0)
    yield
    core.reset_bridge_cache()
    core._RECENT_CREATE_TIMEOUTS.clear()


@pytest.fixture
def alpha(cfg, projects_root):
    make_project(projects_root, "Alpha")
    return cfg


# op -> (endpoint, call(cfg), created path, node_class of the created node,
#        paths that must be absent for the op to count as done)
CASES = {
    "create_folder": ("/bridge/model/folder",
                      lambda c: core.bridge_create_folder(c, "Alpha", "Model", "N1"),
                      "Model/N1", "Object", None),
    "create_object": ("/bridge/model/object",
                      lambda c: core.bridge_create_object(c, "Alpha", "Model", "N1"),
                      "Model/N1", "Object", None),
    "create_variable": ("/bridge/model/variable",
                        lambda c: core.bridge_create_variable(
                            c, "Alpha", "N1", parent="Model", datatype="Int32"),
                        "Model/N1", "Variable", None),
    "create_widget": ("/bridge/ui/widget",
                      lambda c: core.bridge_create_widget(c, "Alpha", "UI/S", "N1", "Label"),
                      "UI/S/N1", "Object", None),
    "create_type": ("/bridge/model/type",
                    lambda c: core.bridge_create_type(c, "Alpha", "N1", "Types"),
                    "Types/N1", "ObjectType", None),
    "create_alias": ("/bridge/node/alias",
                     lambda c: core.bridge_create_alias(c, "Alpha", "Model", "N1"),
                     "Model/N1", "Variable", None),
    "create_netlogic": ("/bridge/model/netlogic",
                        lambda c: core.bridge_create_netlogic(c, "Alpha", "Model", "N1"),
                        "Model/N1", "Object", None),
    "convert_to_type": ("/bridge/node/convert-to-type",
                        lambda c: core.bridge_convert_to_type(
                            c, "Alpha", "Model/Src", "N1", "Types"),
                        "Types/N1", "ObjectType", None),
    "move": ("/bridge/node/move",
             lambda c: core.bridge_move_node(c, "Alpha", "Model/N1", "Dest"),
             "Dest/N1", "Object", "Model/N1"),
}


class FakeBridge:
    """Stateful bridge. ``script`` lists what each successive POST to the
    creating endpoint does: ``timeout_landed`` (node created, answer lost),
    ``timeout_lost`` (nothing created, answer lost), ``name_exists``, ``ok``."""

    def __init__(self, endpoint, path, cls, script, *, existing_cls=None):
        self.endpoint, self.path, self.cls = endpoint, path, cls
        self.script = list(script)
        self.nodes: dict[str, dict] = {}
        self.posts: list[str] = []
        if existing_cls:
            self.nodes[path] = {"path": path, "node_class": existing_cls}

    def _create(self):
        self.nodes[self.path] = {"path": self.path, "node_class": self.cls,
                                 "children": [], "properties": []}

    def __call__(self, cfg, path, method="GET", timeout=5.0, **kw):
        if path.startswith("/bridge/health"):
            return 200, json.dumps({"bridge_version": "1.0.9", "project": "Alpha",
                                    "model_loaded": True}).encode()
        if path.startswith("/bridge/nodes"):
            p = unquote(parse_qs(urlparse(path).query)["path"][0])
            node = self.nodes.get(p)
            if node is None:
                return 404, json.dumps({"error": {"code": "node_not_found"}}).encode()
            return 200, json.dumps(node).encode()
        if path.startswith(self.endpoint):
            assert method == "POST"
            assert kw.get("retry_ambiguous") is False, "creating POST must not be re-sendable"
            self.posts.append(path)
            step = self.script.pop(0) if self.script else "ok"
            if step == "timeout_landed":
                self._create()
                raise core.BridgeWriteTimeout("timed out")
            if step == "timeout_lost":
                raise core.BridgeWriteTimeout("timed out")
            if step == "name_exists":
                return 200, json.dumps({"error": {"code": "name_exists",
                                                  "message": "dup"}}).encode()
            self._create()
            return 200, json.dumps({"ok": True, "created_path": self.path}).encode()
        # anything else (e.g. the move source) is absent
        return 404, json.dumps({"error": {"code": "node_not_found"}}).encode()


def _setup(monkeypatch, op, script, **kw):
    endpoint, call, path, cls, _gone = CASES[op]
    fake = FakeBridge(endpoint, path, cls, script, **kw)
    monkeypatch.setattr(core, "_bridge_http", fake)
    return fake, call, path


@pytest.mark.parametrize("op", sorted(CASES))
def test_timeout_with_node_present_is_verified_success_and_not_resent(
        alpha, monkeypatch, op):
    fake, call, path = _setup(monkeypatch, op, ["timeout_landed"])
    out = call(alpha)
    assert out["ok"] is True, out
    assert out["verified_after_timeout"] is True
    assert core._norm_node_path(out["created_path"]) == path
    assert len(fake.posts) == 1  # never re-sent


@pytest.mark.parametrize("op", sorted(CASES))
def test_timeout_with_node_absent_returns_the_timeout_error(alpha, monkeypatch, op):
    monkeypatch.setattr(core, "_NETLOGIC_VERIFY_ATTEMPTS", 2)
    monkeypatch.setattr(core, "CREATE_VERIFY_ATTEMPTS", 2)
    fake, call, _path = _setup(monkeypatch, op, ["timeout_lost"])
    with pytest.raises(core.BridgeWriteTimeout):
        call(alpha)
    assert len(fake.posts) == 1


@pytest.mark.parametrize("op", sorted(CASES))
def test_timeout_once_then_name_exists_with_matching_node_is_verified(
        alpha, monkeypatch, op):
    """First attempt times out unconfirmed; the node lands later; the retry is
    answered name_exists -> succeeded, verified_after_timeout."""
    monkeypatch.setattr(core, "_NETLOGIC_VERIFY_ATTEMPTS", 1)
    monkeypatch.setattr(core, "CREATE_VERIFY_ATTEMPTS", 1)
    fake, call, _path = _setup(monkeypatch, op, ["timeout_lost", "name_exists"])
    with pytest.raises(core.BridgeWriteTimeout):
        call(alpha)
    fake._create()  # Studio finishes the create after the timeout
    out = call(alpha)
    assert out["ok"] is True and out["verified_after_timeout"] is True, out
    assert len(fake.posts) == 2


# create_alias / move have no class or type to compare: any node at the path matches.
CLASS_CHECKED = sorted(set(CASES) - {"create_alias", "move"})


@pytest.mark.parametrize("op", CLASS_CHECKED)
def test_timeout_once_then_name_exists_with_other_node_is_name_exists_other(
        alpha, monkeypatch, op):
    monkeypatch.setattr(core, "_NETLOGIC_VERIFY_ATTEMPTS", 1)
    monkeypatch.setattr(core, "CREATE_VERIFY_ATTEMPTS", 1)
    fake, call, path = _setup(monkeypatch, op, ["timeout_lost", "name_exists"])
    with pytest.raises(core.BridgeWriteTimeout):
        call(alpha)
    fake.nodes[path] = {"path": path, "node_class": "Method"}  # someone else's node
    if op == "create_widget":
        with pytest.raises(core.BridgeWriteFailed) as e:
            call(alpha)
        out = e.value.op_result
    else:
        out = call(alpha)
    assert out["ok"] is False and out["code"] == "name_exists_other", out
    assert out["existing_class"] == "Method"


def test_name_exists_without_a_prior_timeout_is_a_genuine_duplicate(alpha, monkeypatch):
    fake, call, _p = _setup(monkeypatch, "create_folder", ["name_exists"],
                            existing_cls="Object")
    with pytest.raises(core.BridgeWriteFailed) as e:
        call(alpha)
    assert "name_exists" in str(e.value)


def test_timeout_verify_with_non_matching_node_is_name_exists_other(alpha, monkeypatch):
    fake, call, path = _setup(monkeypatch, "create_folder", ["timeout_lost"],
                              existing_cls="Variable")
    out = call(alpha)
    assert out["ok"] is False and out["code"] == "name_exists_other"
    assert out["existing_class"] == "Variable"
    assert len(fake.posts) == 1


def test_bridge_write_defaults_to_non_idempotent(alpha, monkeypatch):
    seen: list = []

    def fake_post(cfg, path, timeout=8.0, idempotent=True):
        seen.append((path.split("?")[0], idempotent))
        return 200, {"ok": True}

    monkeypatch.setattr(core, "_bridge_post_json", fake_post)
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    core._bridge_write(alpha, "Alpha", "create_folder", "/bridge/model/folder", {"a": "b"})
    core._bridge_write(alpha, "Alpha", "set_property", "/bridge/node/property",
                       {"a": "b"}, idempotent=True)
    assert seen == [("/bridge/model/folder", False), ("/bridge/node/property", True)]


def test_set_ops_opt_in_to_idempotent_and_creating_ops_do_not(alpha, monkeypatch):
    seen: dict = {}

    def fake_write(cfg, project, op, endpoint, params, **kw):
        seen[op] = kw.get("idempotent", False)
        return {"ok": True, "created_path": "Model/N1", "node_class": "Object"}

    monkeypatch.setattr(core, "_bridge_write", fake_write)
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    core.bridge_set_property(alpha, "Alpha", "UI/L", "Text", "x")
    core.bridge_create_folder(alpha, "Alpha", "Model", "N1")
    core.bridge_create_alias(alpha, "Alpha", "Model", "N1")
    assert seen["set_property"] is True
    assert seen["create_folder"] is False and seen["create_alias"] is False


# -- batch: honest result -------------------------------------------------------

def _clean_validate(monkeypatch):
    monkeypatch.setattr(core, "_bridge_post_body",
                        lambda cfg, path, payload, timeout=20.0: (
                            200, {"ok": True, "op_count": len(payload["ops"]),
                                  "errors": [], "warnings": []}))


def test_batch_counts_verified_ops_and_never_says_unchanged(alpha, monkeypatch):
    _clean_validate(monkeypatch)
    fake, _call_, _p = _setup(monkeypatch, "create_folder", ["timeout_landed"])
    ops = [{"op": "create_folder", "parent": "Model", "name": "N1"}]
    out = core.bridge_edit(alpha, "Alpha", ops)
    assert out["state"] == "succeeded" and out["applied"] == 1, out
    assert out["verified_ops"] == [0]
    assert out["op_timings"][0]["verified_after_timeout"] is True
    assert "unchanged" not in json.dumps(out)


def test_batch_failure_after_verified_op_is_partial_not_unchanged(alpha, monkeypatch):
    _clean_validate(monkeypatch)
    # op 0 times out but landed (verified); op 1 is a genuine duplicate
    fake, _call_, _p = _setup(monkeypatch, "create_folder",
                              ["timeout_landed", "name_exists"])
    ops = [{"op": "create_folder", "parent": "Model", "name": "N1"},
           {"op": "create_folder", "parent": "Model", "name": "N2"},
           {"op": "set_property", "path": "Model/N1", "name": "X", "value": "1"}]
    out = core.bridge_edit(alpha, "Alpha", ops)
    assert out["state"] == "partial" and out["applied"] == 1, out
    assert "unchanged" not in out["nudge"]
    assert "verified_after_timeout" in out["nudge"]


def test_batch_unconfirmed_timeout_says_outcome_unknown_not_unchanged(alpha, monkeypatch):
    monkeypatch.setattr(core, "CREATE_VERIFY_ATTEMPTS", 1)
    _clean_validate(monkeypatch)
    fake, _call_, _p = _setup(monkeypatch, "create_folder", ["timeout_lost"])
    ops = [{"op": "create_folder", "parent": "Model", "name": "N1"}]
    out = core.bridge_edit(alpha, "Alpha", ops)
    assert out["state"] == "failed" and out["applied"] == 0, out
    assert "unchanged" not in out["nudge"] and "UNKNOWN" in out["nudge"]


def test_batch_timeout_then_name_exists_batch_retry_is_verified(alpha, monkeypatch):
    monkeypatch.setattr(core, "CREATE_VERIFY_ATTEMPTS", 1)
    _clean_validate(monkeypatch)
    fake, _call_, _p = _setup(monkeypatch, "create_folder", ["timeout_lost", "name_exists"])
    ops = [{"op": "create_folder", "parent": "Model", "name": "N1"}]
    first = core.bridge_edit(alpha, "Alpha", ops)
    assert first["state"] == "failed"
    fake._create()
    second = core.bridge_edit(alpha, "Alpha", ops)
    assert second["state"] == "succeeded" and second["applied"] == 1, second
    assert second["verified_ops"] == [0]


def test_batch_name_exists_other_is_a_failed_op_with_existing_class(alpha, monkeypatch):
    monkeypatch.setattr(core, "CREATE_VERIFY_ATTEMPTS", 1)
    _clean_validate(monkeypatch)
    fake, _call_, path = _setup(monkeypatch, "create_folder", ["timeout_lost", "name_exists"])
    ops = [{"op": "create_folder", "parent": "Model", "name": "N1"}]
    core.bridge_edit(alpha, "Alpha", ops)
    fake.nodes[path] = {"path": path, "node_class": "Variable"}
    out = core.bridge_edit(alpha, "Alpha", ops)
    assert out["state"] == "failed" and out["applied"] == 0, out
    assert "name_exists_other" in out["failed_op"]["error"]
    assert "Variable" in out["nudge"] and "unchanged" not in out["nudge"]
