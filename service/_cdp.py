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
import sys
import urllib.request
from typing import Any


# Bounded CDP timeouts. A websocket CONNECT (TCP + upgrade handshake) to a
# live local Chrome answers in milliseconds, so 5 s is generous; a per-command
# READ (one recv) may legitimately take longer (a screenshot of a big canvas),
# so it gets 10 s. A background/throttled tab that exceeds either is activated
# (/json/activate/<id>) and retried once before we give up with
# ``cdp_tab_timeout`` -- nothing in the CDP path may block for the old 30 s.
CDP_CONNECT_TIMEOUT_S = 5.0
CDP_READ_TIMEOUT_S = 10.0


class CDPError(Exception):
    """A CDP command returned an error, or the transport failed."""


def is_timeout_exc(exc: BaseException) -> bool:
    """True for a socket / websocket timeout (``TimeoutError``,
    ``socket.timeout``, ``websocket.WebSocketTimeoutException``)."""
    return isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower()


def target_id_from_ws(ws_url: str | None) -> str:
    """The target id at the end of a ``ws://.../devtools/page/<id>`` URL."""
    return (ws_url or "").rstrip("/").rsplit("/", 1)[-1]


def _http_get_status(url: str, timeout: float) -> int:
    """GET ``url`` and return the HTTP status (seam: tests fake this)."""
    with urllib.request.urlopen(url, timeout=timeout) as r:
        r.read()
        return int(getattr(r, "status", 200) or 200)


def activate(cdp_url: str, target_id: str, timeout: float = CDP_CONNECT_TIMEOUT_S) -> bool:
    """Bring a tab to the foreground (GET /json/activate/<id>).

    Chrome throttles background tabs so their page-scoped commands can hang;
    activating first makes them answer. Idempotent. Never raises; a non-200
    answer or transport failure returns False and is otherwise ignored."""
    if not target_id:
        return False
    try:
        return _http_get_status(
            cdp_url.rstrip("/") + "/json/activate/" + str(target_id), timeout) == 200
    except Exception:
        return False


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


def list_pages(cdp_url: str, timeout: float = 5.0) -> list[dict]:
    """GET {cdp_url}/json and return every drivable page target
    (``type == "page"`` with a WebSocket URL). Raises OSError/URLError when the
    endpoint is down; returns [] when Chrome is up with no page."""
    with urllib.request.urlopen(cdp_url.rstrip("/") + "/json", timeout=timeout) as r:
        targets = json.loads(r.read())
    return [t for t in targets if t.get("type") == "page"
            and t.get("webSocketDebuggerUrl")]


def open_tab(cdp_url: str, url: str = "about:blank", timeout: float = 5.0) -> dict:
    """Open a NEW tab (PUT /json/new) and return its target dict
    ({id, url, webSocketDebuggerUrl, ...}). Raises CDPError on failure."""
    req = urllib.request.Request(
        cdp_url.rstrip("/") + "/json/new?" + url, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            tab = json.loads(r.read())
    except Exception as e:
        raise CDPError(f"could not open a CDP tab at {cdp_url}: {e}") from e
    if not isinstance(tab, dict) or not tab.get("webSocketDebuggerUrl"):
        raise CDPError(f"CDP /json/new at {cdp_url} returned no page target")
    return tab


def close_tab(cdp_url: str, target_id: str, timeout: float = 5.0) -> bool:
    """Close one tab by target id (GET /json/close/<id>). Never raises;
    returns True when Chrome accepted the close."""
    try:
        with urllib.request.urlopen(
                cdp_url.rstrip("/") + "/json/close/" + str(target_id),
                timeout=timeout) as r:
            r.read()
        return True
    except Exception:
        return False


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


NOT_ATTACHED_MSG = "Not attached to an active page"


def page_probe(cdp_url: str, timeout: float = 5.0,
               ws_url: str | None = None) -> dict:
    """Page-level health check: ``Runtime.evaluate`` of ``1+1`` on the page target.

    ``probe()`` only proves the DevTools HTTP endpoint answers and a page target
    is *listed*; Chrome can list a target yet answer every page-scoped command
    with ``Not attached to an active page``. This opens the page target's
    WebSocket and evaluates ``1+1`` so that state is detected.

    Never raises. Returns ``{healthy, not_attached, detail}``: ``healthy`` only
    when the evaluation returned 2; ``not_attached`` is True for the
    'Not attached to an active page' response specifically. ``ws_url`` probes
    that specific target instead of the first page target.

    ``Page.enable`` is exercised first: it is what every page session opens
    with and what ``Page.reload`` / ``Page.captureScreenshot`` depend on, so a
    target that answers Runtime but not Page is still reported unhealthy.
    ``Page.getNavigationHistory`` and ``Page.getFrameTree`` follow because they
    require an attached page (``Page.enable`` / ``Runtime.evaluate`` alone pass
    on some detached sessions, e.g. after ``optix_emulator(action=restart)``).
    """
    ws = None
    try:
        ws = _connect_ws(ws_url or _discover_page_ws(cdp_url, timeout=timeout),
                         timeout)
        for mid, method, params in (
                (1, "Page.enable", {}),
                # A detached session can still answer Page.enable and
                # Runtime.evaluate yet fail Page.reload / captureScreenshot
                # ('Not attached to an active page'). These two need a live,
                # attached page and have no side effects.
                (2, "Page.getNavigationHistory", {}),
                (3, "Page.getFrameTree", {}),
                (4, "Runtime.evaluate",
                 {"expression": "1+1", "returnByValue": True})):
            ws.send(json.dumps({"id": mid, "method": method, "params": params}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == mid:
                    break
            if "error" in msg:
                break
    except Exception as e:  # transport/discovery failure == not healthy
        text = str(e)
        return {"healthy": False, "not_attached": NOT_ATTACHED_MSG in text,
                "timed_out": is_timeout_exc(e),
                "detail": f"page probe failed: {text}"}
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
    if "error" in msg:
        text = json.dumps(msg["error"])
        return {"healthy": False, "not_attached": NOT_ATTACHED_MSG in text,
                "detail": f"{method}: {text}"}
    value = ((msg.get("result") or {}).get("result") or {}).get("value")
    if value != 2:
        return {"healthy": False, "not_attached": False,
                "detail": f"Runtime.evaluate 1+1 returned {value!r}"}
    return {"healthy": True, "not_attached": False, "detail": "page answered 1+1"}


def main_frame_probe(cdp_url: str, ws_url: str | None = None,
                     timeout: float = 5.0) -> dict:
    """Ask one exact page target for its main frame via ``Page.getFrameTree``.

    ``ws_url`` pins the target (the cached per-project tab); omitted = the
    first page target. Never raises. Returns ``{answered, detail}``;
    ``answered`` is True only when the reply carries ``frameTree.frame`` (the
    main frame). An error reply (e.g. 'Not attached to an active page'), a
    transport failure or a frame-less result all count as not answered.
    """
    ws = None
    try:
        ws = _connect_ws(ws_url or _discover_page_ws(cdp_url, timeout=timeout),
                         timeout)
        ws.send(json.dumps({"id": 1, "method": "Page.getFrameTree",
                            "params": {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                break
    except Exception as e:  # noqa: BLE001 - transport failure == not answered
        return {"answered": False,
                "detail": f"Page.getFrameTree failed: {e}"}
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
    if "error" in msg:
        return {"answered": False,
                "detail": f"Page.getFrameTree: {json.dumps(msg['error'])}"}
    frame = ((msg.get("result") or {}).get("frameTree") or {}).get("frame")
    if not frame:
        return {"answered": False,
                "detail": "Page.getFrameTree returned no main frame"}
    return {"answered": True, "detail": "main frame answered"}


def recreate_page(cdp_url: str, timeout: float = 5.0,
                  force_ws_url: str | None = None) -> dict | None:
    """Replace detached page targets with a fresh tab.

    Probes every listed page target; those that answer 'Not attached' (or
    otherwise fail ``page_probe``) are detached. A new about:blank tab is
    opened FIRST (so Chrome never drops to zero tabs), then the detached
    targets are closed. Returns the new tab dict, or None when nothing could
    be recreated (endpoint down, ``/json/new`` refused). Never raises. Opens a
    tab even when no listed page is detached only if there are no pages at all.

    ``force_ws_url`` names a page target (by WebSocket URL) that a caller
    already saw fail with 'Not attached to an active page' on a real call
    (e.g. ``Page.reload``). It is treated as detached even if ``page_probe``
    passes on it, so a session the probe cannot distinguish is still replaced.
    """
    try:
        pages = list_pages(cdp_url, timeout=timeout)
    except Exception:
        return None
    detached = [p for p in pages
                if p["webSocketDebuggerUrl"] == force_ws_url
                or not page_probe(cdp_url, timeout=timeout,
                                  ws_url=p["webSocketDebuggerUrl"])["healthy"]]
    if pages and not detached:
        return None
    try:
        tab = open_tab(cdp_url, timeout=timeout)
    except CDPError:
        return None
    for p in detached:
        close_tab(cdp_url, p.get("id", ""), timeout=timeout)
    return tab


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
                 connect_timeout: float = CDP_CONNECT_TIMEOUT_S,
                 ws_url: str | None = None,
                 read_timeout: float = CDP_READ_TIMEOUT_S):
        # ws_url pins the session to one specific tab (the per-project tab
        # registry); omitted = the first page target (legacy behaviour).
        ws_url = ws_url or _discover_page_ws(cdp_url)
        self.ws_url = ws_url
        self._ws = _connect_ws(ws_url, connect_timeout)
        self._id = 0
        self._load_fired = False
        try:
            # connect_timeout bounded the handshake; every later recv gets the
            # (longer) per-command read timeout.
            settimeout = getattr(self._ws, "settimeout", None)
            if settimeout is not None:
                settimeout(read_timeout)
            self.cmd("Page.enable")
        except BaseException:
            self.close()   # never leak the socket of a half-open session
            raise

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
            if msg.get("method") == "Page.loadEventFired":
                self._load_fired = True
            # else: a protocol event — keep reading for our reply

    def navigate(self, url: str) -> None:
        self._load_fired = False
        self.cmd("Page.navigate", url=url)

    def reload(self, ignore_cache: bool = False) -> None:
        """Reload the current page. ignore_cache=True (CDP Page.reload's
        ignoreCache) bypasses Chrome's disk/memory cache for this load —
        use it for a forced 'fresh' recapture: after optix_emulator(action='restart')
        the runtime process is new but the tab/URL is unchanged, so a
        plain reload can still be served from cache instead of actually
        re-fetching from (and reconnecting to) the restarted runtime."""
        self._load_fired = False
        self.cmd("Page.reload", ignoreCache=bool(ignore_cache))

    def main_frame_url(self) -> str:
        """URL of the main frame (Page.getFrameTree), or "" if unknown."""
        r = self.cmd("Page.getFrameTree")
        return ((r.get("frameTree") or {}).get("frame") or {}).get("url", "") or ""

    def wait_for_load(self, timeout: float) -> bool:
        """Block until the page finished loading or ``timeout`` seconds pass.

        Completion is ``Page.loadEventFired`` (seen while reading replies) or
        ``document.readyState == 'complete'`` (polled). Returns True when the
        load completed, False on timeout. Never raises on a timeout; CDP
        errors from the poll propagate to the caller."""
        import time as _time
        deadline = _time.monotonic() + max(0.0, timeout)
        while True:
            if self._load_fired:
                return True
            r = self.cmd("Runtime.evaluate", expression="document.readyState",
                         returnByValue=True)
            if (r.get("result") or {}).get("value") == "complete":
                return True
            if _time.monotonic() >= deadline:
                return False
            _time.sleep(0.1)

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

    def screenshot_jpeg(self, quality: int = 65, clip: dict | None = None) -> bytes:
        """Capture the page as JPEG. `clip` (CDP's native {x, y, width,
        height, scale}) restricts the capture to a sub-rectangle in CSS
        pixels — the caller resolves a `region` request to this shape via
        viewport_size() before calling."""
        import base64
        kwargs: dict[str, Any] = {"format": "jpeg", "quality": int(quality)}
        if clip is not None:
            kwargs["clip"] = clip
        r = self.cmd("Page.captureScreenshot", **kwargs)
        return base64.b64decode(r["data"])

    def set_viewport(self, width: int, height: int, scale: float = 1.0) -> bool:
        """Override the emulated device metrics (CDP
        Emulation.setDeviceMetricsOverride) so the Optix WebPresentationEngine
        renders to fill (width, height) instead of the chrome-cdp launch
        window (--window-size=800,600 in bootstrap/install-chrome-cdp.ps1,
        which measures as a 769x434 *visible* slice once scrollbar/chrome
        overhead is subtracted — the source of the clipped-HMI bug this
        exists to fix). Page.getLayoutMetrics reflects the override
        afterward, so viewport_size() — and every click/region computed
        from it — stays in the SAME coordinate space as a screenshot taken
        under the same override.

        Safe no-op: an older Chrome/CDP build that rejects the command (or
        any transport hiccup) returns False instead of raising. The caller
        proceeds with whatever window size is already in effect — a failed
        override must never abort a screenshot/click."""
        try:
            self.cmd(
                "Emulation.setDeviceMetricsOverride",
                width=int(width), height=int(height),
                deviceScaleFactor=float(scale), mobile=False,
            )
            return True
        except CDPError as e:
            print(f"[ftx-mcp] Emulation.setDeviceMetricsOverride failed "
                  f"({width}x{height}@{scale}), falling back to current "
                  f"window size: {e}", file=sys.stderr)
            return False

    def viewport_size(self) -> tuple[float, float]:
        """CSS-pixel viewport size via Page.getLayoutMetrics, used to resolve
        a normalized (<=1.0 fraction) `region` to absolute pixels ahead of a
        clipped screenshot. Prefers cssVisualViewport (the coordinate space
        Page.captureScreenshot's `clip` and Input.dispatchMouseEvent both use);
        falls back to layoutViewport for older Chrome/CDP response shapes."""
        r = self.cmd("Page.getLayoutMetrics")
        vp = r.get("cssVisualViewport") or r.get("visualViewport") or r.get("layoutViewport") or {}
        w = vp.get("clientWidth", vp.get("width", 0))
        h = vp.get("clientHeight", vp.get("height", 0))
        return float(w), float(h)

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

    # One expression, one round trip: everything the error-page / unreachable
    # overlay check needs (see core._page_error_check). Returned by value.
    PAGE_STATE_JS = (
        "(function(){try{"
        "var b=document.body,t=b?(b.innerText||''):'';"
        "var ov=false;"
        "try{var els=document.querySelectorAll('[class*=\"disconnect\" i]');"
        "for(var i=0;i<els.length;i++){if(els[i].getClientRects().length){ov=true;break;}}"
        "}catch(e){}"
        "return {url:String(location.href||''),title:String(document.title||''),"
        "text:t.slice(0,4000),overlay:ov};"
        "}catch(e){return null;}})()"
    )

    def page_state(self) -> dict | None:
        """Snapshot ``{url, title, text, overlay}`` of the current page with a
        SINGLE Runtime.evaluate (no extra round trips). Returns None when the
        page did not answer with an object (callers treat that as unknown)."""
        r = self.cmd("Runtime.evaluate", expression=self.PAGE_STATE_JS,
                     returnByValue=True)
        if r.get("exceptionDetails"):
            return None
        value = (r.get("result") or {}).get("value")
        return value if isinstance(value, dict) else None

    def body_inner_text(self) -> str:
        """``document.body.innerText`` of the current page, via Runtime.evaluate.

        The Optix WebUI renders to the DOM (no canvas), so this yields the exact
        visible strings. Raises CDPError when the evaluation throws or the page
        has no body (so no text can be claimed).
        """
        r = self.cmd("Runtime.evaluate",
                     expression="document.body ? document.body.innerText : null",
                     returnByValue=True)
        if r.get("exceptionDetails"):
            detail = (r["exceptionDetails"].get("exception") or {}).get(
                "description") or r["exceptionDetails"].get("text") or "exception"
            raise CDPError(f"Runtime.evaluate: {detail}")
        value = (r.get("result") or {}).get("value")
        if not isinstance(value, str):
            raise CDPError("Runtime.evaluate: document.body.innerText unavailable "
                           "(page has no body)")
        return value

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
