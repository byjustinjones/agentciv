"""AgentCiv HTTP game server (stdlib only).

Run it::

    python -m agentciv.server --host 0.0.0.0 --port 8765 --data-dir data

Embed it (e.g. in tests)::

    from agentciv.server import create_server
    srv = create_server("127.0.0.1", 0, data_dir="/tmp/data").start_background()
    print(srv.url)
    ...
    srv.stop()

See docs/DESIGN.md §12 for the HTTP API and docs/CONNECTING.md for a guide.
"""
from .app import AgentCivServer, create_server, serve
from .manager import ApiError, GameManager, GameSession

__all__ = ["AgentCivServer", "ApiError", "GameManager", "GameSession", "create_server", "serve"]
