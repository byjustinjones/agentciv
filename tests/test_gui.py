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


def _owned_tiles(view: dict, pid: str) -> list:
    cities = {(c["x"], c["y"]) for c in view["cities"]}
    relics = {(r["x"], r["y"]) for r in view["map"]["relics"]}
    return [[x, y] for y, row in enumerate(view["map"]["owner"]) for x, o in enumerate(row)
            if o == pid and (x, y) not in cities and (x, y) not in relics]


def test_trade_panel_and_map_show_live_deals(server, page):
    """Barter (DESIGN §13) in the spectator GUI: a deal executed mid-turn shows up in
    the Trade tab (deal log, contract, reputation), as a line on the map and on the
    traded tile, while the private haggling stays hidden until the game is over."""
    c = AgentCivClient(server.url)
    gid = c.create_game(max_players=2, turn_timeout=600, name="Barter GUI")
    a, b = AgentCivClient(server.url), AgentCivClient(server.url)
    pa = a.join(gid, "Seller")["player_id"]
    pb = b.join(gid, "Buyer")["player_id"]
    # traded tiles must touch the receiver's land (§13.1): give the buyer a tile next to one of the seller's
    g, mine = server.manager.sessions[gid].game, _owned_tiles(a.state(), pa)
    with server.manager.sessions[gid].cond:
        tile, (nx, ny) = next(
            (t, n) for t in mine
            for n in ((t[0] - 1, t[1]), (t[0] + 1, t[1]), (t[0], t[1] - 1), (t[0], t[1] + 1))
            if 0 <= n[0] < g.width and 0 <= n[1] < g.height and g.owner[g.idx(*n)] is None)
        g.owner[g.idx(nx, ny)] = pb
    r = a.diplomacy([{"type": "propose", "to": pb, "give": {"tiles": [tile], "wood": 10},
                      "get": {"food": 30, "per_turn": {"gold": 1}, "turns": 5}, "peace": 20,
                      "message": "secret haggling text"}])
    deal = r["results"][0]["deal"]
    assert b.diplomacy([{"type": "accept", "deal": deal}])["results"][0]["ok"]

    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(server.url + f"/#/game/{gid}")
    page.wait_for_function("document.querySelector('#players-table').textContent.includes('Seller')")
    page.click("#tab-btn-trade")
    assert page.is_visible("#tab-trade") and not page.is_visible("#tab-events")
    page.wait_for_function("document.querySelector('#trade').textContent.includes('Deal log (1)')", timeout=5000)
    trade = page.text_content("#trade")
    assert "Seller gives" in trade and "10 wood" in trade and "1 tile" in trade
    assert "30 food" in trade and "1 gold/turn × 5" in trade and "peace for 20 turns" in trade
    assert "Active contracts (1)" in trade and "Buyer" in trade
    assert "secret haggling text" not in trade           # private while the game runs
    assert "revealed" in trade or "private while the game runs" in trade
    assert page.inner_text("#trade-n") == "1"
    # reputation table: both parties have one deal
    rows = page.eval_on_selector_all(".rep-table tbody tr",
                                     "rs => rs.map(r => Array.from(r.cells, (c) => c.textContent.trim()))")
    assert sorted(r[0] for r in rows) == ["Buyer", "Seller"] and all(r[1] == "1" for r in rows)
    # map: one deal line and the traded tile's tooltip
    assert page.evaluate("AgentCivGUI.GameView.dealLinks(AgentCivGUI.GameView.view).length") == 1
    assert page.evaluate("AgentCivGUI.GameView.dealKinds(AgentCivGUI.GameView.dealLinks(AgentCivGUI.GameView.view)[0])") \
        == ["goods", "land", "contract", "peace"]
    tip = page.evaluate(f"AgentCivGUI.GameView.tileInfo({tile[0]}, {tile[1]})")
    assert "Traded" in tip and deal in tip
    assert page.evaluate("AgentCivGUI.GameView.animRaf") != 0   # lines are animated
    # the player filter applies to the trade panel
    page.select_option("#feed-filter", pa)
    assert "Deal log (1)" in page.text_content("#trade")
    assert errors == []


def test_every_deal_event_has_readable_text(server, page):
    gid = running_game(server, "EVT")
    page.goto(server.url + f"/#/game/{gid}")
    page.wait_for_function("document.querySelector('#players-table').textContent.includes('EVT-one')")
    texts = page.evaluate("""() => {
      const G = AgentCivGUI.GameView;
      const d = {id: 'd7', thread: 'd7', from: 'p1', to: 'p2', give: {wood: 60, tiles: [[3, 4]]},
                 get: {gold: 45, per_turn: {gold: 5}, turns: 10}, peace: 20, message: 'hi'};
      const evs = [
        {type: 'deal_proposed', turn: 1, by: 'p1', from: 'p1', to: 'p2', deal: d},
        {type: 'deal_countered', turn: 1, by: 'p2', from: 'p1', to: 'p2', deal: 'd7', new: {...d, id: 'd8', from: 'p2', to: 'p1'}},
        {type: 'deal_executed', turn: 1, by: 'p2', from: 'p1', to: 'p2', deal: 'd8', thread: 'd7', give: d.give, get: d.get, peace: 20, contracts: ['c1']},
        {type: 'deal_rejected', turn: 1, by: 'p2', from: 'p1', to: 'p2', deal: 'd7', message: 'too pricey'},
        {type: 'deal_withdrawn', turn: 1, by: 'p1', from: 'p1', to: 'p2', deal: 'd7', reason: 'withdrawn by p1'},
        {type: 'deal_withdrawn', turn: 1, by: null, from: 'p1', to: 'p2', deal: 'd7', reason: 'p1 was eliminated'},
        {type: 'deal_expired', turn: 1, from: 'p1', to: 'p2', deal: 'd7'},
        {type: 'deal_failed', turn: 1, by: 'p2', from: 'p1', to: 'p2', deal: 'd7', reason: 'p1 lacks 10 wood'},
        {type: 'contract_paid', turn: 1, contract: 'c1', payer: 'p2', payee: 'p1', paid: {gold: 5}, turns_left: 9},
        {type: 'contract_completed', turn: 1, contract: 'c1', payer: 'p2', payee: 'p1', deal: 'd8'},
        {type: 'contract_default', turn: 1, contract: 'c1', payer: 'p2', payee: 'p1', per_turn: {gold: 5}, turns_left: 3, penalty: 25, deal: 'd8'},
        {type: 'say', turn: 1, by: 'p1', from: 'p1', to: 'all', text: 'hello all'},
        {type: 'treaty_signed', turn: 1, a: 'p1', b: 'p2', until_turn: 21, deal: 'd8'},
      ];
      return evs.map((e) => { const el = document.createElement('div'); el.innerHTML = G.describe(e).html; return e.type + ': ' + el.textContent; });
    }""")
    joined = "\n".join(texts)
    assert "=" not in joined, joined            # no raw key=value fallback
    for needle in ("proposed a deal", "countered", "Deal", "rejected", "withdrew", "was withdrawn", "expired",
                   "failed", "paid", "honoured", "defaulted", "hello all", "part of deal d8", "60 wood",
                   "5 gold/turn × 10", "peace 20 turns", "(3,4)"):
        assert needle in joined, (needle, joined)


def test_treaty_broken_text_old_new_and_fog_redacted(server, page):
    """A new-rules break seen by a fog spectator has no `removed`/`paid`
    (redacted) but keeps `free`: its bank share and bond must read as removed,
    never as gold to the partner (retune review)."""
    gid = running_game(server, "TBR")
    page.goto(server.url + f"/#/game/{gid}")
    page.wait_for_function("document.querySelector('#players-table').textContent.includes('TBR-one')")
    texts = page.evaluate("""() => {
      const G = AgentCivGUI.GameView;
      const evs = [
        {type: 'treaty_broken', turn: 5, by: 'p1', with: 'p2', cost: 50, legacy_lost: 0, bank_share: 40, bond: 0,
         free: false, betrayals: 1},
        {type: 'treaty_broken', turn: 5, by: 'p1', with: 'p2', cost: 50, legacy_lost: 0, bank_share: 40, bond: 0,
         refund: 0, paid: 0, removed: 40, bank_fee: 0, debt: 0, free: false, cancelled: [], betrayals: 1},
        {type: 'treaty_broken', turn: 5, by: 'p1', with: 'p2', cost: 50, legacy_lost: 0, bank_share: 40, bond: 0,
         betrayals: 1},
      ];
      return evs.map((e) => { const el = document.createElement('div'); el.innerHTML = G.describe(e).html; return el.textContent; });
    }""")
    redacted, full, old = texts
    assert "up to 40 gold removed" in redacted and "gold to" not in redacted, redacted
    assert "40 gold removed" in full and "gold to" not in full, full
    assert "≥40 gold to" in old, old


def test_finished_game_reveals_negotiation_threads(server, page):
    c = AgentCivClient(server.url)
    gid = c.create_game(max_players=2, turn_timeout=600, max_turns=3, name="Threads")
    a, b = AgentCivClient(server.url), AgentCivClient(server.url)
    pa = a.join(gid, "Haggler")["player_id"]
    pb = b.join(gid, "Holdout")["player_id"]
    r = a.diplomacy([{"type": "propose", "to": pb, "give": {"wood": 20}, "get": {"food": 40}, "message": "opening bid"}])
    d1 = r["results"][0]["deal"]
    d2 = b.diplomacy([{"type": "counter", "deal": d1, "give": {"food": 25}, "get": {"wood": 20},
                       "message": "25 at most"}])["results"][0]["deal"]
    assert a.diplomacy([{"type": "accept", "deal": d2}])["results"][0]["ok"]
    r = b.diplomacy([{"type": "propose", "to": pa, "give": {"stone": 1}, "get": {"gold": 30}}])
    a.diplomacy([{"type": "reject", "deal": r["results"][0]["deal"], "message": "no way"}])
    for _ in range(3):
        turn = a.state()["turn"]
        a.submit_orders([], turn=turn)
        b.submit_orders([], turn=turn)
        a.wait(since_turn=turn, timeout=10)
    assert a.state(spectator=True)["status"] == "finished"

    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(server.url + f"/?tab=trade#/game/{gid}")
    page.wait_for_function("document.querySelectorAll('#trade .thread').length === 2", timeout=8000)
    threads = page.eval_on_selector_all("#trade .thread", "ts => ts.map(t => t.textContent)")
    accepted = next(t for t in threads if "opening bid" in t)
    assert "25 at most" in accepted and "Accepted" in accepted and "2 offers" in accepted
    rejected = next(t for t in threads if "no way" in t)
    assert "Rejected" in rejected
    assert errors == []
