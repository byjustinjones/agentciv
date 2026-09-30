"""HTTP front-end (stdlib ``http.server``) for the AgentCiv server.

Implements every endpoint of docs/DESIGN.md §12 and the barter endpoints of
§13 (``POST /diplomacy``, ``GET /inbox``) plus a few conveniences
(``GET /api`` endpoint index, ``GET /api/games/{id}`` summary). JSON in, JSON
out, permissive CORS, and every error is a JSON ``{"error": ...}`` with a
proper status code. Nothing a client sends can crash the server thread.
"""
from __future__ import annotations

import gzip
import hmac
import json
import logging
import mimetypes
import secrets
import sys
import threading
import time
from http import HTTPStatus
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from ..engine import rules_json
from .guide import api_index, api_quickref_markdown
from .manager import MAX_WAIT, ApiError, GameManager, _player_key, available_bots

log = logging.getLogger("agentciv.server")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WEB_DIR = REPO_ROOT / "web"
RULES_MD_PATH = REPO_ROOT / "docs" / "RULES.md"
MAX_BODY = 2 * 1024 * 1024
SSE_KEEPALIVE = 15.0
GZIP_MIN = 1400                  # compress JSON/text responses larger than this (if the client accepts gzip)
GZIP_CACHE_BYTES = 48 * 1024 * 1024
REQUEST_TIMEOUT = 30.0           # socket timeout: idle keep-alive connections and slow uploads are dropped
MAX_CONNECTIONS = 512            # concurrent connections (one thread each); more get an immediate 503

_rules_md_cache: tuple[float, bytes] | None = None


def rules_markdown() -> bytes:
    """docs/RULES.md (re-read when it changes; rendered from the constants if missing)."""
    global _rules_md_cache
    try:
        mtime = RULES_MD_PATH.stat().st_mtime
    except OSError:
        mtime = -1.0
    if _rules_md_cache is None or _rules_md_cache[0] != mtime:
        try:
            data = RULES_MD_PATH.read_bytes()
        except OSError:
            from ..engine import rulesdoc
            data = rulesdoc.render().encode()
        _rules_md_cache = (mtime, data)
    return _rules_md_cache[1]


class _GzipCache:
    """Small LRU of gzipped response bodies keyed by the body itself (bytes
    hash/compare are cheap next to compression; cached view bytes are reused
    objects, so identical bodies hit)."""

    def __init__(self, limit: int = GZIP_CACHE_BYTES):
        self.limit = limit
        self.size = 0
        self.items: OrderedDict[bytes, bytes] = OrderedDict()
        self.lock = threading.Lock()

    def get(self, body: bytes) -> bytes:
        with self.lock:
            z = self.items.get(body)
            if z is not None:
                self.items.move_to_end(body)
                return z
        z = gzip.compress(body, compresslevel=5, mtime=0)
        cost = len(body) + len(z)
        if cost <= self.limit // 4:
            with self.lock:
                if body not in self.items:
                    self.items[body] = z
                    self.size += cost
                    while self.size > self.limit and self.items:
                        k, v = self.items.popitem(last=False)
                        self.size -= len(k) + len(v)
        return z


class AgentCivServer(ThreadingHTTPServer):
    """ThreadingHTTPServer carrying the GameManager and web root."""

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 256  # listen backlog: bursts of spectators/agents connecting at once

    def __init__(self, address, manager: GameManager, web_dir: str | Path | None = None,
                 max_connections: int = MAX_CONNECTIONS, spectator_key: str | None = None):
        self.manager = manager
        self.spectator_key = spectator_key
        self.gzip_cache = _GzipCache()
        self.web_dir = Path(web_dir).resolve() if web_dir else DEFAULT_WEB_DIR
        self._thread: threading.Thread | None = None
        self.max_connections = max_connections
        self._conn_slots = threading.BoundedSemaphore(max_connections)
        super().__init__(address, Handler)

    # One thread per connection, but never more than ``max_connections`` at once: a flood of idle
    # or slow connections gets 503s instead of exhausting threads and file descriptors.
    def process_request(self, request, client_address):
        if not self._conn_slots.acquire(blocking=False):
            body = b'{"error":"server busy: too many open connections; retry shortly"}'
            try:
                request.settimeout(1.0)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                                b"Retry-After: 1\r\nConnection: close\r\nAccess-Control-Allow-Origin: *\r\n"
                                b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._conn_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._conn_slots.release()

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        return f"http://{host}:{port}"

    def handle_error(self, request, client_address):
        """Clients that hang up mid-response are normal; log everything else."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            log.debug("client %s disconnected: %s", client_address, exc)
        else:
            log.exception("error while handling a request from %s", client_address)

    def start_background(self) -> "AgentCivServer":
        """Serve in a daemon thread (handy for tests and scripts)."""
        self._thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.05},
                                        name="agentciv-http", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.manager.shutdown()
        self.shutdown()
        self.server_close()


def create_server(host: str = "127.0.0.1", port: int = 8765, data_dir: str = "data",
                  web_dir: str | Path | None = None, open_ratings: bool = False,
                  spectator_key: str | None = None) -> AgentCivServer:
    """Create (but don't start) a server. ``port=0`` picks a free port.
    ``open_ratings`` rates every ``rated`` game (see GameManager)."""
    return AgentCivServer((host, port), GameManager(data_dir, open_ratings=open_ratings), web_dir,
                         spectator_key=spectator_key)


class Handler(BaseHTTPRequestHandler):
    server: AgentCivServer
    protocol_version = "HTTP/1.1"
    server_version = "AgentCiv/0.1"
    timeout = REQUEST_TIMEOUT  # StreamRequestHandler applies it to the socket

    # ------------------------------------------------------------ plumbing
    def send_error(self, code, message=None, explain=None):
        """Protocol-level errors (bad request line, headers…) as JSON too."""
        try:
            self.close_connection = True
            if getattr(self, "request_version", "HTTP/0.9") in ("HTTP/0.9", ""):
                self.request_version = "HTTP/1.0"  # parsing failed before the version was known: send headers
            self._error(code, message or HTTPStatus(code).phrase)
        except Exception:
            pass

    def log_message(self, fmt, *args):  # route http.server logs to logging
        # http.server includes raw request lines (even malformed ones) in these messages.
        # They may contain credentials, so never log request text or headers.
        log.debug("%s - HTTP request", self.address_string())

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Spectator-Key")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Access-Control-Expose-Headers", "X-Server-Time")

    def _send(self, status: int, body: bytes, ctype: str = "application/json", extra: dict | None = None) -> None:
        # path/headers/command are missing when http.server fails early (bad request line, 414…)
        headers = getattr(self, "headers", None)
        path = getattr(self, "path", None) or ""
        if (headers is not None and len(body) >= GZIP_MIN and "gzip" in headers.get("Accept-Encoding", "")
                and (ctype.startswith(("application/json", "text/")) or "javascript" in ctype)):
            body = self.server.gzip_cache.get(body)
            extra = {**(extra or {}), "Content-Encoding": "gzip", "Vary": "Accept-Encoding"}
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if path.startswith("/api"):
            self.send_header("X-Server-Time", f"{time.time():.3f}")  # lets clients correct clock skew for deadlines
        self._cors()
        self.end_headers()
        if getattr(self, "command", None) != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, json.dumps(obj, separators=(",", ":")).encode())

    def _error(self, http_status: int, message: str, **extra) -> None:
        self._json({"error": message, **extra}, http_status)

    def _body(self):
        length = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            raise ApiError(411, "chunked request bodies are not supported; send Content-Length")
        try:
            n = int(length or 0)
        except ValueError:
            raise ApiError(400, "bad Content-Length") from None
        if n < 0:
            raise ApiError(400, "bad Content-Length")
        if n > MAX_BODY:
            raise ApiError(413, f"body too large (max {MAX_BODY} bytes)")
        raw = self.rfile.read(n) if n else b""
        self._body_consumed = True
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError) as e:
            raise ApiError(400, f"invalid JSON body: {e}") from None
        except RecursionError:
            raise ApiError(400, "invalid JSON body: nested too deeply") from None

    def _token(self, query: dict) -> str | None:
        auth = self.headers.get("Authorization", "")
        if auth[:7].lower() == "bearer ":
            return auth[7:].strip() or None
        vals = query.get("token")
        return vals[0] if vals else None

    def _player(self, game_id: str, query: dict, required: bool) -> str | None:
        """Resolve the bearer token to a player id in ``game_id``."""
        token = self._token(query)
        if token is None:
            if required:
                raise ApiError(401, "missing token: send 'Authorization: Bearer <token>' (from join/quickmatch)")
            return None
        found = self.server.manager.resolve_token(token)
        if found is None:
            raise ApiError(401, "invalid token: use the token returned by POST /api/quickmatch or "
                                "/api/games/{id}/join (tokens do not survive a server restart)")
        if found[0] != game_id:
            raise ApiError(403, f"this token belongs to game {found[0]}, not {game_id}")
        return found[1]

    def _spectator(self, query: dict) -> bool:
        """Validate every supplied operator credential, including empty values."""
        keys = self.headers.get_all("X-Spectator-Key", []) + query.get("spectator_key", [])
        if not keys:
            return False
        configured = self.server.spectator_key
        if not configured:
            raise ApiError(401, "spectator key access is not configured on this server")
        if not all(hmac.compare_digest(key.encode(), configured.encode()) for key in keys):
            raise ApiError(401, "invalid spectator key")
        return True

    # ------------------------------------------------------------ dispatch
    def _discard_body(self) -> None:
        """A GET/HEAD/OPTIONS request with a body: its bytes must not be parsed
        as the next request on this connection (keep-alive desync / request
        smuggling behind a proxy), so the connection is closed after the reply."""
        if self.headers.get("Transfer-Encoding") or (self.headers.get("Content-Length") or "0").strip() not in ("", "0"):
            self.close_connection = True

    def do_OPTIONS(self):
        self._discard_body()
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        self._body_consumed = False
        if method != "POST":
            self._discard_body()
        try:
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            self.full_spectator = self._spectator(parse_qs(url.query, keep_blank_values=True))
            path = url.path
            if path == "/api" or path.startswith("/api/"):
                if method == "POST":
                    body = self._body()
                    self._api_post(path, query, body)
                else:
                    self._api_get(path, query)
            elif method == "GET":
                self._static(path)
            else:
                raise ApiError(405, "method not allowed")
        except ApiError as e:
            if method == "POST" and not self._body_consumed:
                self.close_connection = True  # unread body bytes would corrupt keep-alive
            self._error(e.status, e.message, **e.extra)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True  # client gone, or too slow to send its body
        except Exception as e:  # never let a request kill the server
            log.exception("error handling %s request", method)
            try:
                self._error(500, f"internal error: {type(e).__name__}")
            except Exception:
                self.close_connection = True

    def _route(self, path: str) -> tuple[str | None, str]:
        """Split ``/api/games/{id}/{action}`` into (id, action)."""
        parts = [unquote(p) for p in path.strip("/").split("/")]
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "games":
            if len(parts) == 3:
                return parts[2], ""
            if len(parts) == 4:
                return parts[2], parts[3]
        return None, path

    # ------------------------------------------------------------ GET
    def _api_get(self, path: str, query: dict) -> None:
        mgr = self.server.manager
        p = path.rstrip("/")
        if p == "/api":
            return self._json(api_index(self._base_url()))
        if p == "/api/rules":
            body = rules_markdown() + api_quickref_markdown(self._base_url()).encode()
            return self._send(200, body, "text/markdown; charset=utf-8")
        if p == "/api/rules.json":
            return self._json(rules_json())
        if p == "/api/games":
            limit = self._int_query(query, "limit", None)
            return self._json(mgr.list_games(None if limit is None else min(max(limit, 0), 10_000)))
        if p == "/api/leaderboard":
            mode = (query.get("mode") or ["standard"])[-1]
            if mode not in ("standard", "fog"):
                raise ApiError(400, "mode must be 'standard' or 'fog'")
            return self._json(mgr.leaderboard(mode))
        if p == "/api/bots":
            return self._json(available_bots())
        game_id, action = self._route(p)
        if game_id is None:
            raise ApiError(404, f"no such endpoint: GET {path} (GET /api lists the endpoints)")
        game = mgr.get(game_id)
        if action == "":
            return self._json(game.summary())
        if action == "state":
            pid = self._player(game_id, query, required=False)
            return self._send(200, game.state_bytes(pid, full=self.full_spectator and pid is None))
        if action == "wait":
            since = self._int_query(query, "since_turn", None)
            timeout = self._float_query(query, "timeout", 30.0)
            timeout = min(max(timeout, 0.0), MAX_WAIT)
            if self.command == "HEAD":
                timeout = 0.0  # HEAD must not block
            return self._json(game.wait(since, timeout))
        if action == "inbox":
            pid = self._player(game_id, query, required=True)
            since = self._int_query(query, "since", None)
            timeout = self._float_query(query, "timeout", 30.0)
            timeout = min(max(timeout, 0.0), MAX_WAIT)
            if self.command == "HEAD":
                timeout = 0.0
            return self._json(game.inbox(pid, since, timeout, self._int_query(query, "turn", None)))
        if action == "replay":
            compact = (query.get("compact") or ["0"])[0].lower() in ("1", "true", "yes")
            lo = self._int_query(query, "from", None)
            hi = self._int_query(query, "to", None)
            return self._send(200, game.replay_bytes(compact=compact, lo=lo, hi=hi,
                              full=self.full_spectator and self._token(query) is None))
        if action == "stream":
            return self._stream(game, full=self.full_spectator and self._token(query) is None)
        raise ApiError(404, f"no such endpoint: GET {path} (GET /api lists the endpoints)")

    def _base_url(self) -> str:
        host = self.headers.get("Host") or self.server.url.split("://", 1)[1]
        proto = self.headers.get("X-Forwarded-Proto", "http").split(",")[0].strip() or "http"
        return f"{proto}://{host}"

    @staticmethod
    def _int_query(query: dict, key: str, default):
        vals = query.get(key)
        if not vals or vals[0] == "":
            return default
        try:
            return int(float(vals[0]))
        except (ValueError, OverflowError):  # int(float("1e400")) overflows
            raise ApiError(400, f"{key} must be an integer") from None

    @staticmethod
    def _float_query(query: dict, key: str, default: float) -> float:
        vals = query.get(key)
        if not vals or vals[0] == "":
            return default
        try:
            v = float(vals[0])
        except ValueError:
            raise ApiError(400, f"{key} must be a number") from None
        if v != v:  # NaN
            raise ApiError(400, f"{key} must be a number")
        return v

    def _stream(self, game, full: bool = False) -> None:
        """Server-sent events: ``event: state`` with the spectator view on
        connect, after every turn and on public diplomacy (an executed deal,
        a public message); ``: keep-alive`` comments in between.
        The stream ends after the final (finished) frame."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self._cors()
        self.end_headers()
        self.close_connection = True
        if self.command == "HEAD":
            return  # headers only: no event stream
        mgr = self.server.manager
        key = None
        try:
            self.wfile.write(b"retry: 2000\n\n")
            while not mgr.stopping:
                key, data, finished = game.next_frame(key, SSE_KEEPALIVE, full=full)
                if data is None:
                    self.wfile.write(b": keep-alive\n\n")
                else:
                    self.wfile.write(b"event: state\ndata: " + data + b"\n\n")
                self.wfile.flush()
                if finished and data is not None:
                    self.wfile.write(b"event: finished\ndata: {}\n\n")
                    self.wfile.flush()
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception:  # headers are already sent: log and end the stream (clients reconnect)
            log.exception("error in event stream of game %s", getattr(game, "game_id", "?"))

    # ------------------------------------------------------------ POST
    def _api_post(self, path: str, query: dict, body) -> None:
        mgr = self.server.manager
        p = path.rstrip("/")
        if p == "/api/games":
            session = mgr.create_game(body)
            return self._json({"game_id": session.game_id, "status": session.status,
                               "creator_token": session.creator_token, "rated": session.opts["rated"],
                               "unrated_reason": session.opts.get("unrated_reason"),
                               "fog": session.opts.get("fog", False)})
        if p == "/api/quickmatch":
            session, seat = mgr.quickmatch(body)
            return self._json({"game_id": session.game_id, "player_id": seat.pid, "token": seat.token,
                               "status": session.status})
        game_id, action = self._route(p)
        if game_id is None:
            raise ApiError(404, f"no such endpoint: POST {path} (GET /api lists the endpoints)")
        game = mgr.get(game_id)
        if action == "join":
            if not isinstance(body, dict):
                raise ApiError(400, "body must be {\"name\": ...}")
            seat = game.join(body.get("name"), _player_key(body))
            return self._json({"game_id": game_id, "player_id": seat.pid, "token": seat.token,
                               "status": game.status})
        if action == "start":
            started = game.start(authorized=self._may_start(game, query))
            return self._json({"ok": True, "started": started, "status": game.status})
        if action == "orders":
            pid = self._player(game_id, query, required=True)
            return self._json(game.submit(pid, body))
        if action == "diplomacy":
            pid = self._player(game_id, query, required=True)
            return self._json(game.diplomacy(pid, body))
        raise ApiError(404, f"no such endpoint: POST {path} (GET /api lists the endpoints)")

    def _may_start(self, game, query: dict) -> bool:
        """A seated player's token or the creator token authorises POST /start."""
        token = self._token(query)
        if token is None:
            return False
        if game.creator_token and secrets.compare_digest(token, game.creator_token):
            return True
        found = self.server.manager.resolve_token(token)
        if found is None:
            raise ApiError(401, "invalid token")
        if found[0] != game.game_id:
            raise ApiError(403, f"this token belongs to game {found[0]}, not {game.game_id}")
        return True

    # ------------------------------------------------------------ static
    def _static(self, path: str) -> None:
        root = self.server.web_dir
        rel = unquote(path).lstrip("/") or "index.html"
        try:
            target = (root / rel).resolve()
        except (OSError, ValueError):
            raise ApiError(404, "not found") from None
        if not target.is_relative_to(root):
            raise ApiError(404, "not found")
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            raise ApiError(404, f"not found: {path}")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)


def serve(host: str = "127.0.0.1", port: int = 8765, data_dir: str = "data",
          web_dir: str | Path | None = None, open_ratings: bool = False,
          spectator_key: str | None = None) -> None:
    """Run the server in the foreground until Ctrl-C."""
    srv = create_server(host, port, data_dir, web_dir, open_ratings=open_ratings, spectator_key=spectator_key)
    log.info("AgentCiv server on %s (data: %s, web: %s)", srv.url, data_dir, srv.web_dir)
    print(f"AgentCiv server listening on http://{host}:{srv.server_address[1]}  "
          f"(GUI: {srv.url}/ , API index: {srv.url}/api)", flush=True)
    try:
        srv.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nshutting down…", flush=True)
    finally:
        srv.manager.shutdown()
        srv.server_close()
        time.sleep(0.05)
