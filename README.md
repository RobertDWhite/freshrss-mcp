# freshrss-mcp

An [MCP](https://modelcontextprotocol.io) server that puts a small, well-typed
toolset in front of a [FreshRSS](https://freshrss.org/) instance, so an AI
assistant (Claude, etc.) can read and manage your feeds: list subscriptions,
browse unread/starred articles, read full content, mark read/unread, star, add
feeds, and mark whole streams read.

It talks to FreshRSS over its built-in **Google Reader-compatible API**
(`/api/greader.php`), so it works with any standard FreshRSS install — no
plugins or schema changes required.

The server speaks MCP over **streamable HTTP** on a single port and gates the
endpoint with a static bearer token. `/healthz` is open for liveness/readiness.

## Tools

| Tool | Description |
| --- | --- |
| `whoami` | Authenticated user + instance URL — a quick connectivity/credentials check. |
| `list_feeds` | Subscribed feeds with category and unread count. |
| `list_categories` | Categories/folders with total unread count. |
| `list_articles` | Articles (newest first), filterable by `unread`/`starred`/`all`, by feed or category, with an optional `contains` substring filter and pagination. |
| `get_article` | Full HTML content of a single article by id. |
| `mark_read` / `mark_unread` | Toggle read state for one or more article ids. |
| `star` / `unstar` | Toggle the star/favorite for one or more article ids. |
| `mark_all_read` | Mark an entire stream (all, a feed, or a category) read, optionally only items older than N seconds. |
| `add_feed` | Subscribe to a new feed URL, optionally filed under a category. |

Article ids returned by `list_articles`/`get_article` are the values you pass
back to the mutating tools.

## Configuration

All configuration is via environment variables (see [`.env.example`](.env.example)):

| Variable | Required | Description |
| --- | --- | --- |
| `FRESHRSS_URL` | yes | Base URL of your FreshRSS instance, **without** `/api/...` (e.g. `https://rss.example.com`). |
| `FRESHRSS_USER` | yes | FreshRSS username. |
| `FRESHRSS_API_PASSWORD` | yes | The per-user **API password** (see below) — *not* the web login password. |
| `MCP_TOKEN` | recommended | Bearer token clients must send. If unset, the MCP endpoint is **unauthenticated** — only acceptable for purely local use. |
| `PORT` | no | Listen port (default `8080`). |
| `FRESHRSS_TIMEOUT` | no | Per-request timeout to FreshRSS, seconds (default `30`). |

### Enabling the FreshRSS API

1. In FreshRSS, go to **Settings → Authentication** and enable
   **"Allow API access"** (the global toggle for the GReader/Fever APIs).
2. Go to **Settings → Profile** and set an **API password**. This is a
   dedicated password used only by API clients; it is separate from your login
   password. Put it in `FRESHRSS_API_PASSWORD`.

## Running locally

```bash
cp .env.example .env        # then edit .env with your instance details
pip install -r requirements.txt
set -a && . ./.env && set +a
python server.py
# MCP endpoint:  http://localhost:8080/mcp
# Health check:  http://localhost:8080/healthz
```

Or with Docker:

```bash
docker build -t freshrss-mcp .
docker run --rm -p 8080:8080 --env-file .env freshrss-mcp
```

## Connecting a client

Add it as a streamable-HTTP MCP server. With the Claude Code CLI:

```bash
claude mcp add --transport http freshrss https://your-host/mcp \
  --header "Authorization: Bearer $MCP_TOKEN"
```

Any MCP client that supports HTTP transports works the same way: point it at
`https://your-host/mcp` and send `Authorization: Bearer <MCP_TOKEN>`.

## Security notes

- **No secrets live in this repo.** Credentials are supplied only at runtime via
  environment variables. `.env` is gitignored; only `.env.example` (placeholders)
  is committed.
- Always set `MCP_TOKEN` for any networked deployment — without it the endpoint
  is open to anyone who can reach it.
- The server stores no data on disk; every call proxies to FreshRSS. The
  ClientLogin token is held only in memory and refreshed automatically on 401.
- Prefer giving the MCP its own FreshRSS user/API password so access can be
  revoked independently of your main account.

## How it works

`server.py` is a single [FastMCP](https://github.com/modelcontextprotocol/python-sdk)
streamable-HTTP app. A small `GReader` client handles ClientLogin auth (token
cached in memory, auto-refreshed on 401) and the handful of GReader endpoints
the tools need. A Starlette middleware enforces the bearer token on everything
except `/healthz`.

## License

MIT
