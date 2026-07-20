"""Chrome DevTools Protocol client for clicking the Optix runtime canvas.

Optix Web renders the whole HMI into a single <canvas>, so there are no DOM
targets — clicks must be coordinate-based. Synthetic DOM clicks do NOT
reliably reach Optix's internal hit-tester (the documented limit that used to
force "switches no-op → use RDP"). A CDP `Input.dispatchMouseEvent` is a
*trusted* OS-level event injected at the browser layer, which DOES clear the
hit-tester — that's the difference that makes automated canvas clicks work.

Talks to the `ftx-mcp-chrome-cdp` Chrome instance on the local debug
port (default 127.0.0.1:9222). `Page.captureScreenshot` returns the image
inline so screenshots save server-side (no out-of-band tab plumbing).

Two seams for tests: `_discover_page_ws` (HTTP target discovery) and
`_connect_ws` (the WebSocket). Both are module-level so tests inject a fake
transport without a live Chrome.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any


class CDPError(Exception):
    """A CDP command returned an error, or the transport failed."""


# ---- seams (monkeypatched in tests) -----------------------------------

def _discover_page_ws(cdp_url: str, timeout: float = 5.0) -> str:
    """GET {cdp_url}/json and return the first page target's WebSocket URL."""
    with urllib.request.urlopen(cdp_url.rstrip("/") + "/json", timeout=timeout) as r:
        targets = json.loads(r.read())
    pages = [t for t in targets if t.get("type") == "page"
             and t.get("webSocketDebuggerUrl")]
    if not pages:
        raise CDPError(f"no CDP page target at {cdp_url}")
    return pages[0]["webSocketDebuggerUrl"]


def probe(cdp_url: str, timeout: float = 2.0) -> dict:
    """Health of the CDP endpoint without opening a WebSocket.

    Returns {alive, has_page}: `alive` = the DevTools HTTP responds (Chrome is
    up), `has_page` = there is a drivable 'page' target. Never raises — a dead
    endpoint returns {alive: False, has_page: False}. A Chrome that's up but has
    all tabs closed returns {alive: True, has_page: False} (the tell that today
    reads as 'reachable' on a bare TCP probe but is actually unusable)."""
    base = cdp_url.rstrip("/")
    try:
        with urllib.request.urlopen(base + "/json/version", timeout=timeout) as r:
            r.read()
    except Exception:
        return {"alive": False, "has_page": False}
    try:
        with urllib.request.urlopen(base + "/json", timeout=timeout) as r:
            targets = json.loads(r.read())
        has_page = any(t.get("type") == "page" and t.get("webSocketDebuggerUrl")
                       for t in targets)
    except Exception:
        has_page = False
    return {"alive": True, "has_page": has_page}


def ensure_page(cdp_url: str, url: str = "about:blank", timeout: float = 5.0) -> None:
    """If Chrome is alive but has no page target, open one (PUT /json/new).

    Idempotent — a no-op when a page already exists. Raises CDPError if a page
    still can't be created (endpoint dead, or /json/new refused). Modern Chrome
    requires PUT here (GET was deprecated); the URL rides the query verbatim, so
    keep it simple (about:blank) — the caller navigates afterward."""
    if probe(cdp_url, timeout=timeout)["has_page"]:
        return
    req = urllib.request.Request(
        cdp_url.rstrip("/") + "/json/new?" + url, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
    except Exception as e:
        raise CDPError(
            f"could not open a CDP page target at {cdp_url}: {e}") from e


def _connect_ws(ws_url: str, timeout: float = 30.0):
    """Open the CDP WebSocket. suppress_origin is load-bearing: Chrome is
    launched without --remote-allow-origins and 403s any WS request that
    carries an Origin header; omitting it marks us a non-browser client."""
    import websocket  # websocket-client
    return websocket.create_connection(
        ws_url, timeout=timeout, max_size=None, suppress_origin=True
    )


# ---- client -----------------------------------------------------------

# Named keys optix_cdp_key can dispatch: name -> (windows virtual keycode,
# char payload for the keyDown or None for raw). Enter carries "\r" so Optix
# receives the commit char; Tab carries "\t".
KEY_MAP: dict[str, tuple[int, str | None]] = {
    "Enter": (13, "\r"),
    "Escape": (27, None),
    "Tab": (9, "\t"),
    "Backspace": (8, None),
    "Delete": (46, None),
    "ArrowUp": (38, None),
    "ArrowDown": (40, None),
    "ArrowLeft": (37, None),
    "ArrowRight": (39, None),
}


class CDPClient:
    """Minimal request/response CDP session over one page target."""

    def __init__(self, cdp_url: str = "http://127.0.0.1:9222",
                 connect_timeout: float = 30.0):
        self._ws = _connect_ws(_discover_page_ws(cdp_url), connect_timeout)
        self._id = 0
        self.cmd("Page.enable")

    def cmd(self, method: str, **params: Any) -> dict:
        self._id += 1
        mid = self._id
        self._ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self._ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise CDPError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            # else: a protocol event — ignore and keep reading for our reply

    def navigate(self, url: str) -> None:
        self.cmd("Page.navigate", url=url)

    def reload(self) -> None:
        self.cmd("Page.reload")

    def current_url(self) -> str:
        """The URL of the page currently shown in this target, or "" if it
        can't be determined. Uses Page.getNavigationHistory so no extra domain
        enable is needed."""
        r = self.cmd("Page.getNavigationHistory")
        entries = r.get("entries") or []
        idx = r.get("currentIndex", -1)
        if 0 <= idx < len(entries):
            return entries[idx].get("url", "") or ""
        return ""

    def screenshot_jpeg(self, quality: int = 65) -> bytes:
        import base64
        r = self.cmd("Page.captureScreenshot", format="jpeg", quality=int(quality))
        return base64.b64decode(r["data"])

    def insert_text(self, text: str) -> None:
        """Insert a string at the current caret/selection (Input.insertText).

        One CDP call, lands wherever keyboard focus currently is — the caller
        must have focused an editable target first (a click). Preferred over
        per-char dispatchKeyEvent: no virtual-keycode synthesis to mismatch."""
        self.cmd("Input.insertText", text=str(text))

    def key(self, key: str) -> None:
        """keyDown + keyUp for one named key (Enter commits Optix field edits)."""
        vk, text = KEY_MAP[key]  # KeyError = caller's job to validate first
        down: dict[str, Any] = {
            "type": "keyDown" if text else "rawKeyDown",
            "key": key, "code": key,
            "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk,
        }
        if text:
            down["text"] = text
        self.cmd("Input.dispatchKeyEvent", **down)
        self.cmd("Input.dispatchKeyEvent", type="keyUp", key=key, code=key,
                 windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk)

    def select_all(self) -> None:
        """Ctrl+A on the focused element — select-all so an insert_text
        REPLACES the value (a TextBox click places a caret, it does not
        select; a SpinBox click already selects, and Ctrl+A is harmless)."""
        self.cmd("Input.dispatchKeyEvent", type="rawKeyDown", key="a", code="KeyA",
                 windowsVirtualKeyCode=65, nativeVirtualKeyCode=65, modifiers=2)
        self.cmd("Input.dispatchKeyEvent", type="keyUp", key="a", code="KeyA",
                 windowsVirtualKeyCode=65, nativeVirtualKeyCode=65, modifiers=2)

    def active_element_tag(self) -> str:
        """Tag name of the focused DOM element ("" if none). BODY/"" means no
        editable has focus — a type would silently no-op."""
        r = self.cmd("Runtime.evaluate",
                     expression="document.activeElement ? document.activeElement.tagName : ''",
                     returnByValue=True)
        return str((r.get("result") or {}).get("value") or "")

    def click(self, x: float, y: float) -> None:
        """A real left click at viewport (x, y): move → press → release."""
        for ev in (
            {"type": "mouseMoved"},
            {"type": "mousePressed", "button": "left", "buttons": 1, "clickCount": 1},
            {"type": "mouseReleased", "button": "left", "buttons": 1, "clickCount": 1},
        ):
            self.cmd("Input.dispatchMouseEvent", x=float(x), y=float(y), **ev)

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass
