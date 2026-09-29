"""Browser regression tests for the spectator GUI (web/app.js), run with
Playwright + headless Chromium against a real in-process server. Skipped when
Playwright or its browser is not installed."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from agentciv.client import AgentCivClient
from agentciv.server import create_server

sync_api = pytest.importorskip("playwright.sync_api")


def _launch(p):
    """Launch Chromium; fall back to any installed build when the one this
    Playwright version expects is missing."""
    try:
        return p.chromium.launch()
    except Exception as first:
        root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
        for exe in sorted(root.glob("chromium-*/chrome-linux*/chrome"), reverse=True):
            try:
                return p.chromium.launch(executable_path=str(exe))
            except Exception:
                continue
        raise first


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = _launch(p)
        except Exception as e:  # pragma: no cover - no browser in this environment
            pytest.skip(f"chromium unavailable: {e}")
        yield b
        b.close()


@pytest.fixture()
def server(tmp_path):
    srv = create_server("127.0.0.1", 0, data_dir=str(tmp_path / "data")).start_background()
    yield srv
    srv.stop()


@pytest.fixture()
def page(browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    yield pg
    ctx.close()


def running_game(srv, prefix: str) -> str:
    c = AgentCivClient(srv.url)
    gid = c.create_game(max_players=2, turn_timeout=600, name=f"Game {prefix}")
    AgentCivClient(srv.url).join(gid, f"{prefix}-one")
    AgentCivClient(srv.url).join(gid, f"{prefix}-two")
    return gid


def conn_text(page) -> str:
    return page.eval_on_selector("#conn span", "e => e.textContent")


def test_network_blip_does_not_switch_to_mock_mode(server, page):
    page.goto(server.url + "/#/")
    page.wait_for_function("document.querySelector('#conn span').textContent === 'connected'")
    page.route("**/api/games", lambda route: route.abort())
    page.route("**/mock/*.json", lambda route: route.abort())
    page.evaluate("AgentCivGUI.Lobby.refreshGames()")
    assert page.evaluate("AgentCivGUI.App.mock") is False
    assert conn_text(page) == "offline"
    # a failed mock fetch is not cached forever
    assert page.evaluate("AgentCivGUI.Mock.file('games').then(() => 'ok', () => 'failed')") == "failed"
    page.unroute("**/api/games")
    page.unroute("**/mock/*.json")
    assert page.evaluate("AgentCivGUI.Mock.file('games').then(() => 'ok', () => 'failed')") == "ok"
    gid = AgentCivClient(server.url).create_game(max_players=2, turn_timeout=5, name="After the blip")
    page.evaluate("AgentCivGUI.Lobby.refreshGames()")
    assert conn_text(page) == "connected"
    assert gid in page.inner_text("#games-body")


def test_lobby_polling_stops_when_leaving_before_first_refresh(server, page, monkeypatch):
    gid = running_game(server, "AAA")
    orig = server.manager.list_games
    calls = []

    def slow(*a, **kw):
        calls.append(time.time())
        time.sleep(0.8)
        return orig(*a, **kw)

    monkeypatch.setattr(server.manager, "list_games", slow)
    page.goto(server.url + "/#/")
    # boot probe (slow) -> lobby start -> refreshGames in flight: navigate away right then
    page.wait_for_function("window.AgentCivGUI && !document.querySelector('#lobby').hidden", timeout=5000)
    page.wait_for_timeout(900)
    page.evaluate(f"location.hash = '#/game/{gid}'")
    page.wait_for_timeout(2000)
    assert page.evaluate("AgentCivGUI.Lobby.timer") is None
    n = len(calls)
    page.wait_for_timeout(3500)
    assert len(calls) == n  # no lobby polling while the game view is open
    assert conn_text(page) != "connected"


def test_switching_games_clears_the_previous_sidebar(server, page):
    a = running_game(server, "AAA")
    b = running_game(server, "BBB")
    page.goto(server.url + f"/#/game/{a}")
    page.wait_for_function("document.querySelector('#players-table').textContent.includes('AAA-one')")
    assert "AAA-one" in page.inner_text("#feed-filter")
    page.evaluate(f"location.hash = '#/game/{b}'")
    page.wait_for_function("document.querySelector('#players-table').textContent.includes('BBB-one')")
    options = page.inner_text("#feed-filter")
    assert "BBB-one" in options and "AAA-one" not in options
    page.evaluate("location.hash = '#/game/nosuchgame'")
    page.wait_for_function("document.querySelector('#map-empty').textContent.includes('Could not load')")
    assert page.inner_text("#players-table").strip() == ""
    assert page.inner_text("#status-panel").strip() == ""
    assert "BBB" not in page.inner_text("#feed-filter")


def test_games_refresh_button_refreshes(server, page):
    page.goto(server.url + "/#/")
    page.wait_for_function("document.querySelector('#conn span').textContent === 'connected'")
    gid = AgentCivClient(server.url).create_game(max_players=2, turn_timeout=5, name="Fresh one")
    page.click("#games-refresh")
    page.wait_for_function(f"document.querySelector('#games-body').textContent.includes('{gid}')", timeout=2500)
