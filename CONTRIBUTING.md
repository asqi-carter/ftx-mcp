# Contributing to ftx-mcp

Thanks for your interest. This project has a few non-obvious test/CI realities
that contributors should know up-front.

## Repo status

Current release is `v1.0.0`. This project is meant to leverage llms for design edits and testing via a headless browser from the emulator runtime. 

## What automated checks cover vs. what they can't

`pytest` + `ruff` run cross-platform and exercise the pure-function modules
(`service/core.py`, `service/deploy_lock.py`, etc.) with a faked process
runner. **They do NOT exercise the real deploy path.**

The deploy path requires:
- Windows 11 (DPAPI is a Windows API; Studio CLI is Windows-only)
- FactoryTalk Optix Studio installed
- A real `.optix` project on disk
- Interactive logon session (DPAPI; SSH sessions cannot decrypt blobs
  written by an interactive logon)

None of those are available on a Linux runner. **A green test run on a
deploy-touching PR means the unit tests pass, not that the deploy works.**
PRs that change the deploy
path will be verified by a maintainer on a Windows box before merge. This is a
permanent constraint, not a temporary gap.

If your PR touches `service/core.py:deploy`, `service/core.py:verify_export_mtime`,
`service/deploy_lock.py`, or any `bootstrap/*.ps1`, please:
1. Note the change in the PR description.
2. Add or update unit tests where possible (mock the runner).
3. Mention which Windows + Studio version you tested on, if any.

## Local dev

```bash
git clone https://github.com/asqi-carter/ftx-mcp.git
cd ftx-mcp
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
ruff check .
mypy service/
```

The Linux dev loop is fine for `core.py` / `deploy_lock.py` work. For HTTP/MCP
surface work, run on Windows or use the in-process `make_fake_runner` fixture (see
`service/tests/conftest.py`) — the export-based deploy tests run cross-platform.

## Smoke checklist

A full smoke checklist is rehearsed cold on a clean Windows user account
before every release tag. If you ship something new to a critical path, call
it out in the PR so it gets a checklist entry.

## Code style

- Ruff handles formatting and linting (`ruff check .`)
- `.ps1` files must stay ASCII-only (and CRLF, enforced via `.gitattributes`).
  Windows PowerShell 5.1 mis-parses em-dash/curly-quote bytes under
  Windows-1252 and fails with baffling parse errors — use `--`, `'`, `"`.
- Type hints are encouraged but not strict
- Docstrings on MCP tools are a deliverable: include "Use this when" and
  "Do NOT use this when" — see `service/mcp_app.py` for examples. These are
  what the LLM reads at tool-selection time.

## Reporting issues

Issues most useful to the project:
- Studio CLI version mismatch behavior (1.7 vs 1.6 etc)
- Deploy verification edge cases (`runtime_probe` confirmed but runtime serving stale content, or `export_mtime` confirmed but swap target empty)
- DPAPI breakage (machine swap, user-account change, profile corruption)
- Concurrent deploy contention reproducible scenarios

For everything else, please open an issue describing what you're trying to
do and the relevant section of `docs/architecture.md`.
