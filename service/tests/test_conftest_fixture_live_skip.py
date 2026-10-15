"""The autouse ``_fast_netlogic_template_wait`` fixture (conftest) speeds up unit
tests but must leave the real template-wait constants alone for live tests."""
from __future__ import annotations

import os

import pytest

from service import core
from service.tests import conftest as ct

_LIVE_ENV = any(k.startswith("FTX_LIVE_") and v for k, v in os.environ.items())


@pytest.mark.skipif(_LIVE_ENV, reason="FTX_LIVE_* set: the fixture is intentionally off")
def test_fast_netlogic_wait_fixture_applies_to_non_live_tests():
    assert core._NETLOGIC_TEMPLATE_MAX_WAIT_SECONDS == 0.3
    assert core._NETLOGIC_SETTLE_SECONDS == 0.05
    assert core._NETLOGIC_SETTLE_POLL == 0.01


@pytest.mark.live
def test_fast_netlogic_wait_fixture_skipped_for_live_marker():
    assert core._NETLOGIC_TEMPLATE_MAX_WAIT_SECONDS != 0.3


def test_is_live_run_detects_marker_and_env(request, monkeypatch):
    for k in [k for k in os.environ if k.startswith("FTX_LIVE_")]:
        monkeypatch.delenv(k)
    assert ct._is_live_run(request) is False
    monkeypatch.setenv("FTX_LIVE_PROJECT", "P")
    assert ct._is_live_run(request) is True
