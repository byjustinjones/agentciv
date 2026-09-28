"""HTTP front-end (stdlib ``http.server``) for the AgentCiv server.

Implements every endpoint of docs/DESIGN.md §12 plus a few conveniences
(``GET /api`` endpoint index, ``GET /api/games/{id}`` summary). JSON in, JSON
out, permissive CORS, and every error is a JSON ``{"error": ...}`` with a
proper status code. Nothing a client sends can crash the server thread.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from ..engine import rules_json
from .manager import MAX_WAIT, ApiError, GameManager, available_bots

log = logging.getLogger("agentciv.server")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WEB_DIR = REPO_ROOT / "web"
RULES_MD_PATH = REPO_ROOT / "docs" / "RULES.md"
MAX_BODY = 2 * 1024 * 1024
SSE_KEEPALIVE = 15.0

API_INDEX = {
    "name": "AgentCiv",
    "docs": {"rules_markdown": "/api/rules", "rules_json": "/api/rules.json",
             "connecting": "docs/CONNECTING.md in the repository"},
    "endpoints": [
        "GET  /api/rules                      rules guide (markdown)",
        "GET  /api/rules.json                 constants and cost tables",
        "GET  /api/games                      list games",
        "POST /api/games                      create a game {max_players, turn_timeout, bots, ...}",
        "POST /api/quickmatch                 {name, players?} join/create a lobby -> {game_id, player_id, token}",
        "POST /api/games/{id}/join            {name} -> {game_id, player_id, token}",
        "POST /api/games/{id}/start           start now",
        "GET  /api/games/{id}                 game summary",
        "GET  /api/games/{id}/state           your view (Authorization: Bearer TOKEN) or spectator view",
        "POST /api/games/{id}/orders          {turn, orders:[...]} -> {accepted, errors, turn}",
        "GET  /api/games/{id}/wait            ?since_turn=T&timeout=30 long-poll until the turn advances",
        "GET  /api/games/{id}/stream          server-sent events: spectator view each turn",
        "GET  /api/games/{id}/replay          all frames + result",
        "GET  /api/leaderboard                ratings",
        "GET  /api/bots                       built-in bot names",
    ],
}

_rules_md_cache: bytes | None = None


def rules_markdown() -> bytes:
    """docs/RULES.md (regenerated from the constants if the file is missing)."""
    global _rules_md_cache
    if _rules_md_cache is None:
        try:
            _rules_md_cache = RULES_MD_PATH.read_bytes()
        except OSError:
            from ..engine import rulesdoc
            _rules_md_cache = rulesdoc.render().encode()
    return _rules_md_cache


class AgentCivServer(ThreadingHTTPServer):
    """ThreadingHTTPServer carrying the GameManager and web root."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, manager: GameManager, web_dir: str | Path | None = None):
        self.manager = manager
        self.web_dir = Path(web_dir).resolve() if web_dir else DEFAULT_WEB_DIR
        self._thread: threading.Thread | None = None
        super().__init__(address, Handler)

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
                  web_dir: str | Path | None = None) -> AgentCivServer:
    """Create (but don't start) a server. ``port=0`` picks a free port."""
    return AgentCivServer((host, port), GameManager(data_dir), web_dir)


class Handler(BaseHTTPRequestHandler):
    server: AgentCivServer
    protocol_version = "HTTP/1.1"
    server_version = "AgentCiv/0.1"

    # ------------------------------------------------------------ plumbing
    def send_error(self, code, message=None, explain=None):
        """Protocol-level errors (bad request line, headers…) as JSON too."""
        try:
            self.close_connection = True
            self._error(code, message or HTTPStatus(code).phrase)
        except Exception:
            pass

    def log_message(self, fmt, *args):  # route http.server logs to logging
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "86400")

    def _send(self, status: int, body: bytes, ctype: str = "application/json", extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self._cors()
        self.end_headers()
        if self.command != "HEAD":
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
            raise ApiError(401, "invalid token")
        if found[0] != game_id:
            raise ApiError(403, f"this token belongs to game {found[0]}, not {game_id}")
        return found[1]

    # ------------------------------------------------------------ dispatch
    def do_OPTIONS(self):
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
        try:
            url = urlsplit(self.path)
            query = parse_qs(url.query)
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
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as e:  # never let a request kill the server
            log.exception("error handling %s %s", method, self.path)
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
            return self._json(API_INDEX)
        if p == "/api/rules":
            return self._send(200, rules_markdown(), "text/markdown; charset=utf-8")
        if p == "/api/rules.json":
            return self._json(rules_json())
        if p == "/api/games":
            return self._json(mgr.list_games())
        if p == "/api/leaderboard":
            return self._json(mgr.leaderboard())
        if p == "/api/bots":
            return self._json(available_bots())
        game_id, action = self._route(p)
        if game_id is None:
            raise ApiError(404, f"no such endpoint: GET {path}")
        game = mgr.get(game_id)
        if action == "":
            return self._json(game.summary())
        if action == "state":
            pid = self._player(game_id, query, required=False)
            return self._send(200, game.state_bytes(pid))
        if action == "wait":
            since = self._int_query(query, "since_turn", None)
            timeout = self._float_query(query, "timeout", 30.0)
            timeout = min(max(timeout, 0.0), MAX_WAIT)
            return self._json(game.wait(since, timeout))
        if action == "replay":
            return self._send(200, game.replay_bytes())
        if action == "stream":
            return self._stream(game)
        raise ApiError(404, f"no such endpoint: GET {path}")

    @staticmethod
    def _int_query(query: dict, key: str, default):
        vals = query.get(key)
        if not vals or vals[0] == "":
            return default
        try:
            return int(float(vals[0]))
        except ValueError:
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

    def _stream(self, game) -> None:
        """Server-sent events: ``event: state`` with the spectator view on
        connect and after every turn; ``: keep-alive`` comments in between.
        The stream ends after the final (finished) frame."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self._cors()
        self.end_headers()
        self.close_connection = True
        mgr = self.server.manager
        key = None
        try:
            self.wfile.write(b"retry: 2000\n\n")
            while not mgr.stopping:
                key, data, finished = game.next_frame(key, SSE_KEEPALIVE)
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

    # ------------------------------------------------------------ POST
    def _api_post(self, path: str, query: dict, body) -> None:
        mgr = self.server.manager
        p = path.rstrip("/")
        if p == "/api/games":
            session = mgr.create_game(body)
            return self._json({"game_id": session.game_id, "status": session.status})
        if p == "/api/quickmatch":
            session, seat = mgr.quickmatch(body)
            return self._json({"game_id": session.game_id, "player_id": seat.pid, "token": seat.token,
                               "status": session.status})
        game_id, action = self._route(p)
        if game_id is None:
            raise ApiError(404, f"no such endpoint: POST {path}")
        game = mgr.get(game_id)
        if action == "join":
            if not isinstance(body, dict):
                raise ApiError(400, "body must be {\"name\": ...}")
            seat = game.join(body.get("name"))
            return self._json({"game_id": game_id, "player_id": seat.pid, "token": seat.token,
                               "status": game.status})
        if action == "start":
            started = game.start()
            return self._json({"ok": True, "started": started, "status": game.status})
        if action == "orders":
            pid = self._player(game_id, query, required=True)
            return self._json(game.submit(pid, body))
        raise ApiError(404, f"no such endpoint: POST {path}")

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
          web_dir: str | Path | None = None) -> None:
    """Run the server in the foreground until Ctrl-C."""
    srv = create_server(host, port, data_dir, web_dir)
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
