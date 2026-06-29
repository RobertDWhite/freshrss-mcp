"""freshrss-mcp — an MCP server in front of a FreshRSS instance.

One FastMCP streamable-HTTP app on :8080 that exposes a small set of tools for
reading and managing a FreshRSS account over its Google Reader-compatible API
("GReader API", served by FreshRSS at /api/greader.php).

  * MCP endpoint (bearer-gated):  https://<MCP_HOST>/mcp
      Tools: list_feeds, list_categories, list_articles, get_article,
      mark_read, mark_unread, star, unstar, mark_all_read, add_feed, whoami.
  * /healthz is open (liveness/readiness).

Auth (client -> this server): a static bearer token, MCP_TOKEN. When MCP_TOKEN
is unset the gate is disabled (handy for local dev); set it in any real deploy.

Auth (this server -> FreshRSS): GReader ClientLogin. Configure with
FRESHRSS_URL, FRESHRSS_USER and FRESHRSS_API_PASSWORD. The API password is the
per-user "API password" set in FreshRSS under Settings -> Profile, and is
distinct from the web-login password. The GReader API must also be enabled
globally (Settings -> Authentication -> "Allow API access").

No state is stored locally; every tool call proxies to FreshRSS. The ClientLogin
token is cached in memory and transparently refreshed on a 401.
"""

import logging
import os
import threading
import time

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

MCP_TOKEN = os.environ.get("MCP_TOKEN", "")
PORT = int(os.environ.get("PORT", "8080"))
# Base URL of the FreshRSS instance (no trailing /api/...). e.g.
# http://freshrss.freshrss.svc.cluster.local or https://rss.example.com
FRESHRSS_URL = os.environ.get("FRESHRSS_URL", "").rstrip("/")
FRESHRSS_USER = os.environ.get("FRESHRSS_USER", "")
FRESHRSS_API_PASSWORD = os.environ.get("FRESHRSS_API_PASSWORD", "")
HTTP_TIMEOUT = float(os.environ.get("FRESHRSS_TIMEOUT", "30"))

# GReader well-known stream ids.
READING_LIST = "user/-/state/com.google/reading-list"
READ = "user/-/state/com.google/read"
STARRED = "user/-/state/com.google/starred"

log = logging.getLogger("freshrss-mcp")

mcp = FastMCP(
    "freshrss",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


# --- FreshRSS GReader client ---------------------------------------------

class FreshRSSError(RuntimeError):
    """Raised when FreshRSS returns an error or is misconfigured."""


class GReader:
    """Minimal Google Reader-compatible API client for FreshRSS.

    Thread-safe: a lock guards token (re)issuance. The auth token is cached and
    re-fetched automatically when FreshRSS answers 401.
    """

    def __init__(self, base_url: str, user: str, api_password: str):
        if not base_url or not user or not api_password:
            raise FreshRSSError(
                "FreshRSS is not configured: set FRESHRSS_URL, FRESHRSS_USER "
                "and FRESHRSS_API_PASSWORD"
            )
        self.base = base_url
        self.api = f"{base_url}/api/greader.php"
        self.user = user
        self._password = api_password
        self._auth: str | None = None
        self._lock = threading.Lock()
        self._client = httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True)

    # -- auth --
    def _login(self) -> str:
        r = self._client.post(
            f"{self.api}/accounts/ClientLogin",
            data={"Email": self.user, "Passwd": self._password},
        )
        if r.status_code != 200 or "Auth=" not in r.text:
            raise FreshRSSError(
                "FreshRSS ClientLogin failed "
                f"(HTTP {r.status_code}); check FRESHRSS_USER / "
                "FRESHRSS_API_PASSWORD and that API access is enabled"
            )
        for line in r.text.splitlines():
            if line.startswith("Auth="):
                return line[len("Auth="):].strip()
        raise FreshRSSError("FreshRSS ClientLogin returned no Auth token")

    def _token(self) -> str:
        with self._lock:
            if self._auth is None:
                self._auth = self._login()
            return self._auth

    def _headers(self) -> dict:
        return {"Authorization": f"GoogleLogin auth={self._token()}"}

    def _request(self, method: str, path: str, *, params=None, data=None, _retry=True):
        url = path if path.startswith("http") else f"{self.api}{path}"
        r = self._client.request(method, url, params=params, data=data, headers=self._headers())
        if r.status_code == 401 and _retry:
            with self._lock:
                self._auth = None  # force re-login
            return self._request(method, path, params=params, data=data, _retry=False)
        if r.status_code >= 400:
            raise FreshRSSError(f"FreshRSS {method} {path} -> HTTP {r.status_code}: {r.text[:300]}")
        return r

    def get_json(self, path: str, params=None) -> dict:
        params = {**(params or {}), "output": "json"}
        return self._request("GET", path, params=params).json()

    def post_json(self, path: str, data=None) -> dict:
        """POST a read query that returns JSON (e.g. stream/items/contents).

        Unlike `post`, this does not attach a write token — these endpoints read
        rather than mutate, so the GReader edit token is unnecessary.
        """
        body = {**(data or {}), "output": "json"}
        return self._request("POST", path, data=body).json()

    # -- POST write token (required for edit-tag / subscription edits) --
    def write_token(self) -> str:
        return self._request("GET", "/reader/api/0/token").text.strip()

    def post(self, path: str, data: dict) -> str:
        body = {**data, "T": self.write_token()}
        return self._request("POST", path, data=body).text


_greader: GReader | None = None
_greader_lock = threading.Lock()


def client() -> GReader:
    global _greader
    with _greader_lock:
        if _greader is None:
            _greader = GReader(FRESHRSS_URL, FRESHRSS_USER, FRESHRSS_API_PASSWORD)
        return _greader


# --- helpers --------------------------------------------------------------

def _short_id(item_id: str) -> str:
    """The trailing segment of a GReader item id (the form edit-tag accepts)."""
    return item_id.rsplit("/", 1)[-1]


def _category_stream(category: str) -> str:
    return category if category.startswith("user/-/label/") else f"user/-/label/{category}"


def _feed_stream(feed_id: str) -> str:
    return feed_id if feed_id.startswith("feed/") else f"feed/{feed_id}"


def _fmt_article(item: dict) -> dict:
    """Project a GReader stream item down to the fields callers care about."""
    canonical = (item.get("canonical") or [{}])
    url = canonical[0].get("href") if canonical else None
    origin = item.get("origin") or {}
    categories = item.get("categories") or []
    return {
        "id": item.get("id"),
        "title": item.get("title"),
        "author": item.get("author") or None,
        "url": url or (item.get("alternate") or [{}])[0].get("href"),
        "feed": origin.get("title"),
        "feed_id": origin.get("streamId"),
        "published": item.get("published"),
        "read": f"{READ}" in categories,
        "starred": f"{STARRED}" in categories,
        "summary": (item.get("summary") or item.get("content") or {}).get("content"),
    }


def _id_list(ids) -> list[str]:
    if isinstance(ids, str):
        ids = [ids]
    if not ids:
        raise ValueError("provide at least one article id")
    return [str(i) for i in ids]


# --- MCP tools ------------------------------------------------------------

@mcp.tool()
def whoami() -> dict:
    """Return the authenticated FreshRSS user and the instance base URL.

    Useful as a connectivity/credentials check. Returns {userId, userName, url}.
    """
    info = client().get_json("/reader/api/0/user-info")
    return {
        "userId": info.get("userId"),
        "userName": info.get("userName"),
        "url": FRESHRSS_URL,
    }


@mcp.tool()
def list_feeds() -> list[dict]:
    """List subscribed feeds with their category and current unread count.

    Returns a list of {id, title, url, site_url, category, unread}. The `id` is
    the feed's GReader stream id (e.g. "feed/3"); pass it as `feed_id` to
    list_articles, mark_all_read, etc.
    """
    subs = client().get_json("/reader/api/0/subscription/list").get("subscriptions", [])
    unread = _unread_map()
    out = []
    for s in subs:
        cats = s.get("categories") or []
        out.append({
            "id": s.get("id"),
            "title": s.get("title"),
            "url": s.get("url"),
            "site_url": s.get("htmlUrl"),
            "category": cats[0].get("label") if cats else None,
            "unread": unread.get(s.get("id"), 0),
        })
    return sorted(out, key=lambda f: (-f["unread"], (f["title"] or "").lower()))


@mcp.tool()
def list_categories() -> list[dict]:
    """List categories/folders (and labels) with their total unread count.

    Returns a list of {id, label, unread}. Pass `label` as `category` to
    list_articles or mark_all_read.
    """
    tags = client().get_json("/reader/api/0/tag/list").get("tags", [])
    unread = _unread_map()
    out = []
    for t in tags:
        sid = t.get("id", "")
        if "/label/" not in sid:
            continue
        out.append({
            "id": sid,
            "label": t.get("label") or sid.rsplit("/", 1)[-1],
            "unread": unread.get(sid, 0),
        })
    return sorted(out, key=lambda c: (-c["unread"], c["label"].lower()))


def _unread_map() -> dict:
    counts = client().get_json("/reader/api/0/unread-count").get("unreadcounts", [])
    return {c.get("id"): c.get("count", 0) for c in counts}


@mcp.tool()
def list_articles(
    filter: str = "unread",
    feed_id: str = "",
    category: str = "",
    count: int = 20,
    contains: str = "",
    continuation: str = "",
    oldest_first: bool = False,
) -> dict:
    """List articles, newest first, optionally scoped to a feed or category.

    filter: "unread" (default), "starred", or "all".
    feed_id: limit to one feed (its stream id from list_feeds, e.g. "feed/3").
    category: limit to one category/folder (its label from list_categories).
        feed_id takes precedence if both are given.
    count: max articles to return (1-100, default 20).
    contains: case-insensitive substring filter applied to title + summary
        (client-side; useful since the GReader API has no full-text search).
    continuation: opaque token from a previous call's `continuation` to page on.
    oldest_first: return oldest first instead of newest first.

    Returns {articles: [...], continuation: str|None}. Each article has
    {id, title, author, url, feed, feed_id, published, read, starred, summary}.
    Use the returned `id` values with mark_read/star/etc.
    """
    count = max(1, min(int(count), 100))
    if filter == "starred":
        stream = STARRED
    elif feed_id:
        stream = _feed_stream(feed_id)
    elif category:
        stream = _category_stream(category)
    else:
        stream = READING_LIST

    params: dict = {"n": count}
    if filter == "unread":
        params["xt"] = READ  # exclude already-read
    if oldest_first:
        params["r"] = "o"
    if continuation:
        params["c"] = continuation

    data = client().get_json(f"/reader/api/0/stream/contents/{stream}", params)
    articles = [_fmt_article(i) for i in data.get("items", [])]
    if contains:
        needle = contains.lower()
        articles = [
            a for a in articles
            if needle in (a["title"] or "").lower() or needle in (a["summary"] or "").lower()
        ]
    return {"articles": articles, "continuation": data.get("continuation")}


@mcp.tool()
def get_article(id: str) -> dict:
    """Fetch a single article by its GReader item id, including full content.

    Returns {id, title, author, url, feed, feed_id, published, read, starred,
    content}. `content` is the full article HTML (vs. list_articles' summary).
    """
    # stream/items/contents must be POST (GReader rejects it as GET with 400).
    data = client().post_json(
        "/reader/api/0/stream/items/contents",
        {"i": _short_id(id)},
    )
    items = data.get("items", [])
    if not items:
        raise FreshRSSError(f"article {id!r} not found")
    art = _fmt_article(items[0])
    content = items[0].get("content") or items[0].get("summary") or {}
    art["content"] = content.get("content")
    art.pop("summary", None)
    return art


def _edit_tag(ids, add: str = "", remove: str = "") -> dict:
    ids = _id_list(ids)
    data: dict = {"i": [_short_id(i) for i in ids]}
    if add:
        data["a"] = add
    if remove:
        data["r"] = remove
    resp = client().post("/reader/api/0/edit-tag", data)
    ok = resp.strip().upper() == "OK"
    return {"ok": ok, "count": len(ids), "response": resp.strip()[:200]}


@mcp.tool()
def mark_read(ids: list[str]) -> dict:
    """Mark one or more articles as read. `ids` are GReader item ids."""
    return _edit_tag(ids, add=READ)


@mcp.tool()
def mark_unread(ids: list[str]) -> dict:
    """Mark one or more articles as unread. `ids` are GReader item ids."""
    return _edit_tag(ids, remove=READ)


@mcp.tool()
def star(ids: list[str]) -> dict:
    """Star (favorite) one or more articles. `ids` are GReader item ids."""
    return _edit_tag(ids, add=STARRED)


@mcp.tool()
def unstar(ids: list[str]) -> dict:
    """Remove the star from one or more articles. `ids` are GReader item ids."""
    return _edit_tag(ids, remove=STARRED)


@mcp.tool()
def mark_all_read(feed_id: str = "", category: str = "", older_than_seconds: int = 0) -> dict:
    """Mark a whole stream as read.

    With no arguments: marks the entire reading list (everything) read. Pass
    feed_id to limit to one feed, or category to limit to one folder. Optionally
    only mark items older than `older_than_seconds` ago (0 = no limit).
    Returns {ok, stream}.
    """
    if feed_id:
        stream = _feed_stream(feed_id)
    elif category:
        stream = _category_stream(category)
    else:
        stream = READING_LIST
    data = {"s": stream}
    if older_than_seconds and older_than_seconds > 0:
        # GReader `ts` is an exclusive upper bound in microseconds.
        data["ts"] = str(int((time.time() - older_than_seconds) * 1_000_000))
    resp = client().post("/reader/api/0/mark-all-as-read", data)
    return {"ok": resp.strip().upper() == "OK", "stream": stream, "response": resp.strip()[:200]}


@mcp.tool()
def add_feed(url: str, category: str = "") -> dict:
    """Subscribe to a new feed by URL, optionally filing it under a category.

    url: the feed URL (or a site URL FreshRSS can discover a feed for).
    category: optional folder label to place the new subscription in.
    Returns the FreshRSS quickadd result.
    """
    if not url:
        raise ValueError("url is required")
    resp = client().post("/reader/api/0/subscription/quickadd", {"quickadd": url})
    result = {"requested": url, "response": resp.strip()[:500]}
    if category:
        # Best-effort: move the freshly added feed into the requested category.
        try:
            client().post(
                "/reader/api/0/subscription/edit",
                {"ac": "edit", "s": _maybe_added_stream(url), "a": _category_stream(category)},
            )
            result["category"] = category
        except Exception as exc:  # noqa: BLE001 - never fail the subscribe on this
            result["category_warning"] = str(exc)
    return result


def _maybe_added_stream(url: str) -> str:
    """Find the stream id of the subscription whose feed/site url matches `url`."""
    subs = client().get_json("/reader/api/0/subscription/list").get("subscriptions", [])
    for s in subs:
        if url in (s.get("url") or "") or url in (s.get("htmlUrl") or ""):
            return s.get("id")
    return f"feed/{url}"


# --- HTTP plumbing --------------------------------------------------------

async def _healthz(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


class Gate(BaseHTTPMiddleware):
    """Bearer auth for the MCP endpoint only; everything else 404s.

    The bearer is required on /mcp (and /mcp/*). Any other path — including the
    OAuth discovery probes MCP clients make on startup (/.well-known/oauth-*,
    /register) — returns 404, NOT 401. A 401 on those probes makes a client
    believe OAuth is required and attempt an interactive authorization flow,
    which hangs in headless/sandboxed clients (e.g. Claude Cowork) and surfaces
    as a connection timeout. /healthz stays open.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path == "/healthz":
            return await call_next(request)
        if path == "/mcp" or path.startswith("/mcp/"):
            if MCP_TOKEN and request.headers.get("authorization", "") != f"Bearer {MCP_TOKEN}":
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)
        return PlainTextResponse("not found", status_code=404)


app = mcp.streamable_http_app()
app.add_middleware(Gate)
app.router.routes.append(Route("/healthz", _healthz, methods=["GET"]))


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    if not MCP_TOKEN:
        log.warning("MCP_TOKEN is unset; the MCP endpoint is UNAUTHENTICATED")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
