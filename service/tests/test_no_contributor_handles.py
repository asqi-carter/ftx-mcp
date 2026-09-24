"""Name gate: retired identifiers must not reappear in tracked files.

Tokens are compared by SHA-256 so this file names nothing itself.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_FORBIDDEN_SHA256 = frozenset({
    "ab31fc5e14555b645660e919a66535f792f96614b4d80e03293f742b507d0fa3",
    "d7b6067bdeb93caf7a18a7e2feaaadb4d2fc20bd1750eb45a8cab216216f1316",
    "7c266a6755eb5bfcbe258d1b66ca367e8adbf12acd00de8fa0975799119243e9",
})
_TOKEN = re.compile(r"[a-z0-9][a-z0-9.\-]*")
_BINARY = {".png", ".jpg", ".gif", ".ico", ".zip", ".dll", ".exe", ".pyc"}


def _tokens(line: str) -> set[str]:
    return {t.rstrip(".-") for t in _TOKEN.findall(line.lower())}


def _is_forbidden(token: str) -> bool:
    return hashlib.sha256(token.encode()).hexdigest() in _FORBIDDEN_SHA256


def _tracked_text_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True)
    files = [REPO / f for f in out.stdout.decode().split("\0") if f]
    return [f for f in files if f.suffix.lower() not in _BINARY and f.is_file()]


def test_tokenizer_splits_emails_and_punctuation():
    assert _tokens("x (Foo) a.b@c-d.com.") == {"x", "foo", "a.b", "c-d.com"}


def test_tracked_files_carry_no_retired_identifier():
    hits = []
    for f in _tracked_text_files():
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if any(_is_forbidden(t) for t in _tokens(line)):
                hits.append(f"{f.relative_to(REPO)}:{i}")
    assert not hits, "retired identifier present in tracked files: " + ", ".join(hits[:10])
