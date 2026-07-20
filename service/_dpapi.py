"""DPAPI (Data Protection API) shim for Windows-bound secret blobs.

ftx-mcp persists `tokens.json` as `tokens.json.dpapi`, encrypted under
the current Windows user via `CryptProtectData` — same trust boundary as
the existing `studio-password.bin`. This module is a thin ctypes wrapper
so the service does not need to take a hard dependency on `pywin32`.

On non-Windows hosts every entry point raises `UnsupportedPlatformError`. Linux
test runs that exercise `TokenStore` should pass plaintext `tokens.json`
paths instead — the suffix-detector in `auth.py` decides which path to
take. Tests that want to cover the DPAPI plumbing on Linux should monkey-
patch `unprotect`/`protect` rather than try to fake the OS.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Literal

Scope = Literal["CurrentUser", "LocalMachine"]


class UnsupportedPlatformError(RuntimeError):
    """Raised when DPAPI entry points are called off-Windows."""


def _is_windows() -> bool:
    return sys.platform == "win32"


def _require_windows() -> None:
    if not _is_windows():
        raise UnsupportedPlatformError(
            "DPAPI is only available on Windows; on Linux/macOS use a "
            "plaintext tokens.json path (set OPTIX_TOKENS_PATH or pass "
            "path=Path(...) to TokenStore directly). The .dpapi suffix "
            "is reserved for Windows production deployments."
        )


# ---- ctypes plumbing (only loaded on Windows) -----------------------

class _DataBlob(ctypes.Structure):
    _fields_ = (
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    )


def _bytes_to_blob(data: bytes) -> _DataBlob:
    buf = ctypes.create_string_buffer(data, len(data))
    blob = _DataBlob()
    blob.cbData = len(data)
    blob.pbData = ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))
    # Keep buf alive for the duration the blob is used by attaching it.
    blob._buf = buf  # type: ignore[attr-defined]
    return blob


def _blob_to_bytes(blob: _DataBlob) -> bytes:
    out = ctypes.string_at(blob.pbData, blob.cbData)
    # CryptUnprotectData / CryptProtectData allocate via LocalAlloc; the
    # caller is required to LocalFree the returned buffer.
    ctypes.windll.kernel32.LocalFree(blob.pbData)  # type: ignore[attr-defined]
    return out


_FLAG_LOCAL_MACHINE = 0x4  # CRYPTPROTECT_LOCAL_MACHINE


def _scope_flag(scope: Scope) -> int:
    if scope == "LocalMachine":
        return _FLAG_LOCAL_MACHINE
    if scope == "CurrentUser":
        return 0
    raise ValueError(f"unknown DPAPI scope: {scope!r}")


def protect(plaintext: bytes, *, scope: Scope = "CurrentUser",
            description: str = "ftx-mcp tokens") -> bytes:
    """DPAPI-encrypt `plaintext` and return the ciphertext blob.

    `scope='CurrentUser'` (default) ties decryption to the running
    Windows user — the same boundary `studio-password.bin` uses.
    `scope='LocalMachine'` lets any user on the host decrypt; ftx-mcp
    does not currently use it, but the flag is exposed for completeness.
    """
    _require_windows()
    in_blob = _bytes_to_blob(plaintext)
    out_blob = _DataBlob()
    desc = ctypes.c_wchar_p(description)
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        desc,
        None,  # pOptionalEntropy
        None,  # pvReserved
        None,  # pPromptStruct
        _scope_flag(scope),
        ctypes.byref(out_blob),
    )
    if not ok:
        err = ctypes.GetLastError()  # type: ignore[attr-defined]
        raise OSError(err, f"CryptProtectData failed (Win32 error {err})")
    return _blob_to_bytes(out_blob)


def unprotect(ciphertext: bytes, *, scope: Scope = "CurrentUser") -> bytes:
    """DPAPI-decrypt `ciphertext` and return plaintext bytes.

    The `scope` argument must match what was used at encrypt time. Wrong
    scope, wrong user, or a different machine all surface as `OSError`
    with the underlying Win32 error code; callers should treat any
    decryption failure as fatal (refuse to start) rather than papering
    over it — corrupt or wrong-user secrets must not silently degrade
    to "no tokens loaded".
    """
    _require_windows()
    in_blob = _bytes_to_blob(ciphertext)
    out_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,  # ppszDataDescr
        None,  # pOptionalEntropy
        None,  # pvReserved
        None,  # pPromptStruct
        _scope_flag(scope),
        ctypes.byref(out_blob),
    )
    if not ok:
        err = ctypes.GetLastError()  # type: ignore[attr-defined]
        raise OSError(err, f"CryptUnprotectData failed (Win32 error {err})")
    return _blob_to_bytes(out_blob)


__all__ = ["UnsupportedPlatformError", "protect", "unprotect"]
