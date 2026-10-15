# service/tests

Offline tests run with `python -m pytest service/tests` (the live modules skip
themselves without a bridge).

## Live-bridge tests

`test_bridge_live.py` drives a real Studio bridge. It is gated by
`FTX_LIVE_BRIDGE=1` and `FTX_LIVE_PROJECT=<open project>` (see the module
docstring for the full setup).

### Prerequisites after the five-step upgrade

The five-step checkout upgrade (`services.ps1 stop`, `git pull`,
`uninstall.ps1 -PurgeVenv`, `setup.ps1`, `services.ps1 start`; see the README)
rebuilds the venv without the test extras. Run `pip install -e ".[dev]"` again
(in the checkout's venv) before running any live test; without it `pytest` and
the other dev dependencies are missing.

### Default live invocation (excludes `slow_live`)

    FTX_LIVE_BRIDGE=1 FTX_LIVE_PROJECT=<p> python -m pytest service/tests/test_bridge_live.py -q -rs -m "not slow_live"

This is what the bench CI gate (`ci/validate-w11.ps1`) runs, so the Tier-2 suite
fits in 30 min.

### Slow live tests (`@pytest.mark.slow_live`)

The NetLogic race and re-create tests take 57-81 s each and are marked
`slow_live`:

- `test_netlogic_edit_delete_race_after_create_live`
- `test_netlogic_delete_node_race_after_create_live`
- `test_netlogic_recreate_after_delete_does_not_revert_edit_live`

Run them separately, on demand (before releases touching NetLogic
create/delete/restore code):

    FTX_LIVE_BRIDGE=1 FTX_LIVE_PROJECT=<p> python -m pytest service/tests/test_bridge_live.py -q -rs -m slow_live

A plain `pytest service/tests/test_bridge_live.py` (no `-m`) runs everything,
slow tests included. The marker is registered in `pyproject.toml`.

### Selecting tests on the bench: `-k` and `-m`

`scripts/bench/ftx live` takes a name expression (`-k`), a marker expression
(`-m`), or both. A bare fifth argument is still read as `-k`:

    scripts/bench/ftx live <rnd> <label> <proj> <port> -k 'bridge_sync or trend_pen'
    scripts/bench/ftx live <rnd> <label> <proj> <port> -m 'not slow_live'
    scripts/bench/ftx live <rnd> <label> <proj> <port> -m slow_live -k netlogic 'FTX_LIVE_HMI=1'

An optional last argument sets `NAME=value;NAME2=value` environment variables for
the run.

### Self-seeding, unique names and teardown

The `live` fixture creates `UI/MainWindow/Rectangle1` (`FTX_LIVE_NODE`) with
`create_widget` when it is missing; if that fails the test FAILS (it never
skips). Every node, driver, station and class a live test creates carries a
per-run unique suffix (`_nm(...)`) and is deleted in teardown. Tests that make a
`zzOpenTruth*` or `zzBridgeSync*` project close the Studio they launched and then
delete the project folder.

### Skip vs fail

A live test skips only when the bridge or an optional fixture is absent (no
armed bridge for `FTX_LIVE_PROJECT`, `FTX_LIVE_HMI` unset, missing scratch
node). Once the bridge is up, a failed setup step (for example a NetLogic
create) FAILS the test. Race and re-create tests use a unique class name per
run and delete their node and `.cs` in teardown.

### Full set

    FTX_LIVE_BRIDGE=1 FTX_LIVE_PROJECT=<p> python -m pytest service/tests/test_bridge_live.py -q -rs
