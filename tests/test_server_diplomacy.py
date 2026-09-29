"""Server barter tests (docs/DESIGN.md §13): POST /diplomacy, GET /inbox,
SSE pushes on executed deals, and house-bot negotiation (3 rounds per turn +
reactive answers mid-turn)."""
from __future__ import annotations

import json
import threading
import time
import urllib.request

import pytest

from agentciv.bots.base import Bot
from agentciv.client import AgentCivClient
from agentciv.server import create_server

from test_server import call, wait_until


@pytest.fixture()
def server(tmp_path):
    srv = create_server("127.0.0.1", 0, data_dir=str(tmp_path / "data")).start_background()
    yield srv
    srv.stop()


def two_players(server, n: int = 2, **opts):
    """A running game of ``n`` remote players without a deadline (turns wait for everyone)."""
    host = AgentCivClient(server.url)
    gid = host.create_game(max_players=n, turn_timeout=0, rated=False, **opts)
    clients = []
    for i in range(n):
        c = AgentCivClient(server.url)
        c.join(gid, f"agent{i}")
        clients.append(c)
    assert wait_until(lambda: host.game(gid)["status"] == "running")
    return gid, clients


def dip(server, gid, token, body):
    return call(server, "POST", f"/api/games/{gid}/diplomacy", body, token=token)


def res_of(c: AgentCivClient) -> dict:
    return c.state()["you"]["resources"]


# ---------------------------------------------------------------- endpoints
def test_propose_counter_accept_over_http(server):
    gid, (a, b) = two_players(server)
    ra0, rb0 = res_of(a), res_of(b)
    s, r = dip(server, gid, a.token, {"actions": [{"type": "propose", "to": b.player_id, "give": {"wood": 20},
                                                    "get": {"gold": 30}, "message": "wood?"}]})
    assert s == 200 and r["ok"] and r["results"][0] == {"index": 0, "ok": True, "deal": "d1"}
    assert r["turn"] == 0 and r["seq"] >= 2 and "deadline" in r
    # B sees the open deal immediately, with deliverability; the proposer does too
    open_b = b.state()["deals"]["open"]
    assert [d["id"] for d in open_b] == ["d1"] and open_b[0]["deliverable"] is True
    assert a.state()["deals"]["open"][0]["to"] == b.player_id
    # B counters (bare list body), A accepts (single action body)
    s, r = dip(server, gid, b.token, [{"type": "counter", "deal": "d1", "give": {"gold": 20}, "get": {"wood": 20}}])
    assert s == 200 and r["results"][0]["deal"] == "d2" and r["results"][0]["countered"] == "d1"
    s, r = dip(server, gid, a.token, {"type": "accept", "deal": "d2"})
    assert s == 200 and r["results"][0]["status"] == "accepted"
    ra, rb = res_of(a), res_of(b)
    assert ra["wood"] == ra0["wood"] - 20 and ra["gold"] == ra0["gold"] + 20
    assert rb["wood"] == rb0["wood"] + 20 and rb["gold"] == rb0["gold"] - 20
    for c in (a, b):
        v = c.state()
        assert v["deals"]["log"][-1]["id"] == "d2" and not v["deals"]["open"]
        assert {d["id"]: d["status"] for d in v["deals"]["recent"]} == {"d1": "countered", "d2": "accepted"}
        assert all(p["reputation"]["deals"] == 1 for p in v["players"])
    # the public spectator view carries the log but no private negotiation
    s, spec = call(server, "GET", f"/api/games/{gid}/state")
    assert spec["deals"]["log"][-1]["id"] == "d2" and spec["deals"]["open"] == [] and spec["deals"]["recent"] == []
    # after the turn, the (public) event list has the execution but not the haggling
    a.submit_orders([], turn=0)
    b.submit_orders([], turn=0)
    assert wait_until(lambda: a.state()["turn"] == 1)
    s, spec = call(server, "GET", f"/api/games/{gid}/state")
    assert any(e["type"] == "deal_executed" and e["deal"] == "d2" for e in spec["events"])
    assert not any(e["type"] in ("deal_proposed", "deal_countered") for e in spec["events"])
    assert any(e["type"] == "deal_proposed" for e in b.state()["events"])


def test_diplomacy_errors_and_auth(server):
    gid, (a, b) = two_players(server)
    # auth
    assert dip(server, gid, None, {"actions": []})[0] == 401
    assert dip(server, gid, "nope", {"actions": []})[0] == 401
    gid2, (c, _) = two_players(server)
    assert dip(server, gid, c.token, {"actions": []})[0] == 403
    s, r = call(server, "GET", f"/api/games/{gid}/inbox?timeout=0")
    assert s == 401
    s, r = call(server, "GET", f"/api/games/{gid}/inbox?timeout=0", token=c.token)
    assert s == 403
    # malformed bodies
    assert dip(server, gid, a.token, {"foo": 1})[0] == 400
    assert dip(server, gid, a.token, {"actions": "propose"})[0] == 400
    assert dip(server, gid, a.token, 7)[0] == 400
    assert call(server, "POST", f"/api/games/{gid}/diplomacy", raw_body=b"{bad", token=a.token)[0] == 400
    # more actions than one call looks at: refused up front (no per-action work under the game lock)
    s, r = dip(server, gid, a.token, {"actions": [{}] * 101})
    assert s == 400 and "too many actions" in r["error"] and "max 100" in r["error"]
    s, r = dip(server, gid, a.token, {"actions": [{"type": "bogus"}] * 100})
    assert s == 200 and len(r["results"]) == 100 and not r["ok"]
    # stale turn
    s, r = dip(server, gid, a.token, {"turn": 5, "actions": [{"type": "say", "to": "all", "text": "x"}]})
    assert s == 409 and r["turn"] == 0
    # per-action errors: shape errors get an example, state errors don't; nothing raises
    s, r = dip(server, gid, a.token, {"turn": 0, "actions": [
        {"type": "bogus"}, {"type": "propose", "to": b.player_id}, {"type": "accept", "deal": "d99"},
        {"type": "propose", "to": a.player_id, "give": {"wood": 1}}, "junk",
        {"type": "propose", "to": b.player_id, "give": {"influence": 5}}]})
    assert s == 200 and not r["ok"] and len(r["results"]) == 6
    e = r["results"]
    assert "hint" in e[0] and "propose" in e[0]["hint"]
    assert e[1]["example"]["type"] == "propose"
    assert "no open deal" in e[2]["error"] and "example" not in e[2]
    assert "yourself" in e[3]["error"]
    assert "hint" in e[4]
    assert "influence is not tradable" in e[5]["error"]
    # b can't accept a deal of someone else / a's own; a can't accept its own proposal
    rid = dip(server, gid, a.token, [{"type": "propose", "to": b.player_id, "give": {"wood": 1}}])[1]
    did = rid["results"][0]["deal"]
    assert "only the recipient" in dip(server, gid, a.token, [{"type": "accept", "deal": did}])[1]["results"][0]["error"]
    # per-turn say limit comes from the engine
    r = dip(server, gid, a.token, [{"type": "say", "to": "all", "text": str(i)} for i in range(12)])[1]
    assert sum(x["ok"] for x in r["results"]) == 10 and "at most 10" in r["results"][-1]["error"]


def test_diplomacy_lobby_finished_and_eliminated(server):
    host = AgentCivClient(server.url)
    gid = host.create_game(max_players=3, turn_timeout=0, rated=False)
    a = AgentCivClient(server.url)
    a.join(gid, "early")
    s, r = dip(server, gid, a.token, {"actions": []})
    assert s == 409 and r["status"] == "lobby"
    s, box = call(server, "GET", f"/api/games/{gid}/inbox?timeout=0.2", token=a.token)
    assert s == 200 and box["items"] == [] and box["status"] == "lobby" and box["timed_out"]
    # a finished game
    gid2 = host.create_game(max_players=2, max_turns=1, turn_timeout=0.05, turn_delay=0, bots=["idle"], rated=False)
    b = AgentCivClient(server.url)
    b.join(gid2, "late")
    assert wait_until(lambda: host.game(gid2)["status"] == "finished")
    s, r = dip(server, gid2, b.token, {"actions": [{"type": "say", "to": "all", "text": "gg"}]})
    assert s == 409 and r["status"] == "finished"
    s, box = call(server, "GET", f"/api/games/{gid2}/inbox?since=0&timeout=5", token=b.token)
    assert s == 200 and box["status"] == "finished" and not box["timed_out"]
    # eliminated players can't act (engine-level check surfaces as 409)
    gid3, (x, y) = two_players(server)
    session = server.manager.sessions[gid3]
    with session.cond:
        session.game.player(x.player_id).alive = False
    s, r = dip(server, gid3, x.token, {"actions": [{"type": "say", "to": "all", "text": "?"}]})
    assert s == 409 and "eliminated" in r["error"]


def test_inbox_long_poll_wakes_on_proposal_and_is_private(server):
    gid, (a, b, c) = two_players(server, 3)
    got = {}

    def poll():
        t0 = time.monotonic()
        got["box"] = b.inbox(since=0, timeout=20)
        got["dt"] = time.monotonic() - t0

    th = threading.Thread(target=poll)
    th.start()
    time.sleep(0.4)
    assert "box" not in got  # still blocked: nothing addressed to B yet
    # a message between A and C doesn't concern B (private): B keeps waiting
    a.say(c.player_id, "psst")
    time.sleep(0.3)
    assert "box" not in got
    r = a.propose(b.player_id, give={"stone": 5}, get={"gold": 5}, message="hello")
    assert r["ok"] and r["deal"] == "d1"
    th.join(10)
    assert not th.is_alive() and got["dt"] < 5
    box = got["box"]
    assert not box["timed_out"] and box["turn"] == 0 and box["status"] == "running"
    assert [e["type"] for e in box["items"]] == ["deal_proposed"]
    ev = box["items"][0]
    assert ev["from"] == a.player_id and ev["to"] == b.player_id and ev["deal"]["message"] == "hello"
    assert ev["by"] == a.player_id and ev["seq"] == box["seq"]
    assert b.inbox_seq == box["seq"]
    # C saw the private message but not A's proposal to B; A sees nothing of its own
    assert [e["type"] for e in c.inbox(since=0, timeout=0)["items"]] == ["say"]
    assert a.inbox(since=0, timeout=0)["items"] == []
    # a public message reaches everyone; since= skips what was already seen
    b.say("all", "hi all")
    assert [e["text"] for e in a.inbox(timeout=1)["items"]] == ["hi all"]
    assert b.inbox(timeout=0.2)["timed_out"]  # own actions are not echoed
    # B's reply wakes A
    b.reject("d1", "no thanks")
    items = a.inbox(timeout=5)["items"]
    assert items[0]["type"] == "deal_rejected" and items[0]["message"] == "no thanks"


def test_inbox_wakes_on_turn_change_and_timeout(server):
    gid, (a, b) = two_players(server)
    t0 = time.monotonic()
    box = a.inbox(timeout=0.3)
    assert box["timed_out"] and box["items"] == [] and time.monotonic() - t0 >= 0.25
    got = {}
    th = threading.Thread(target=lambda: got.update(box=a.inbox(timeout=20)))
    th.start()
    time.sleep(0.2)
    a.submit_orders([], turn=0)
    b.submit_orders([], turn=0)
    th.join(10)
    assert got["box"]["turn"] == 1 and not got["box"]["timed_out"]
    # turn=T returns at once when turn T is already over (no lost wake-up between calls)
    t0 = time.monotonic()
    box = a.inbox(timeout=10, turn=0)
    assert box["turn"] == 1 and not box["timed_out"] and time.monotonic() - t0 < 2
    assert a.inbox(timeout=0.2, turn=1)["timed_out"]
    # bad query values
    assert call(server, "GET", f"/api/games/{gid}/inbox?since=x", token=a.token)[0] == 400
    assert call(server, "GET", f"/api/games/{gid}/inbox?timeout=nan", token=a.token)[0] == 400


def test_diplomacy_inside_orders_still_works(server):
    gid, (a, b) = two_players(server)
    r = a.submit_orders([{"type": "propose", "to": b.player_id, "give": {"wood": 5}, "get": {"gold": 1}}], turn=0)
    assert r["accepted"] == 1 and not r["errors"]
    b.submit_orders([], turn=0)
    assert wait_until(lambda: a.state()["turn"] == 1)
    assert [d["id"] for d in b.state()["deals"]["open"]] == ["d1"]


def test_sse_pushes_a_frame_when_a_deal_executes(server):
    gid, (a, b) = two_players(server)
    req = urllib.request.Request(server.url + f"/api/games/{gid}/stream")
    frames = []

    def reader():
        with urllib.request.urlopen(req, timeout=20) as resp:
            for raw in resp:
                line = raw.decode().strip()
                if line.startswith("data: "):
                    frames.append(json.loads(line[6:]))
                    if len(frames) >= 2:
                        return

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    assert wait_until(lambda: len(frames) == 1)
    did = a.propose(b.player_id, give={"wood": 3}, get={"gold": 2})["deal"]
    time.sleep(0.3)
    assert len(frames) == 1  # a private proposal doesn't push anything
    assert b.accept(did)["ok"]
    th.join(10)
    assert len(frames) == 2 and frames[1]["turn"] == 0
    assert frames[1]["deals"]["log"][-1]["id"] == did


def test_api_index_and_rules_mention_bartering(server):
    s, idx = call(server, "GET", "/api")
    assert "bartering" in idx and any("/diplomacy" in e for e in idx["endpoints"])
    assert any("/inbox" in e for e in idx["endpoints"])
    assert any("/diplomacy" in step for step in idx["how_to_play"]) and len(idx["how_to_play"]) == 4
    assert {a["type"] for a in idx["bartering"]["actions"]} == {"propose", "counter", "accept", "reject",
                                                                 "withdraw", "say"}
    s, md = call(server, "GET", "/api/rules")
    assert "/diplomacy" in md and "/inbox" in md


# ---------------------------------------------------------------- house bots
class Haggler(Bot):
    """Test house bot: accepts offers asking at most ``price`` gold, counters
    dearer ones at ``price``; records every negotiate/act call."""

    name = "haggler"

    def __init__(self, price: int = 5, fail: bool = False):
        super().__init__(0)
        self.price = price
        self.fail = fail
        self.calls: list[int] = []
        self.acts: list[int] = []
        self.lock = threading.Lock()

    def negotiate(self, view):
        with self.lock:
            self.calls.append(view["turn"])
        if self.fail:
            raise RuntimeError("boom")
        me, out = view["you"]["id"], []
        for d in view["deals"]["open"]:
            if d["to"] != me:
                continue
            if d["get"].get("gold", 0) <= self.price:
                out.append({"type": "accept", "deal": d["id"]})
            else:
                out.append({"type": "counter", "deal": d["id"], "give": {**d["get"], "gold": self.price},
                            "get": d["give"], "message": f"{self.price} gold, not more"})
        return out

    def act(self, view):
        with self.lock:
            self.acts.append(view["turn"])
        return []


def game_with_haggler(server, remote: int = 1, cls=None, **bot_kw):
    host = AgentCivClient(server.url)
    gid = host.create_game(max_players=remote + 1, turn_timeout=0, rated=False, bots=["idle"])
    session = server.manager.sessions[gid]
    bot = (cls or Haggler)(**bot_kw)
    with session.cond:
        seat = next(s for s in session.seats.values() if s.is_bot)
        seat.bot = bot
    clients = []
    for i in range(remote):
        c = AgentCivClient(server.url)
        c.join(gid, f"remote{i}")
        clients.append(c)
    assert wait_until(lambda: host.game(gid)["status"] == "running")
    return gid, seat.pid, bot, clients, session


def test_house_bot_gets_three_negotiation_rounds_then_acts(server):
    gid, bot_pid, bot, (a,), session = game_with_haggler(server)
    assert wait_until(lambda: bot.acts == [0])
    assert bot.calls == [0, 0, 0]
    a.submit_orders([], turn=0)
    assert wait_until(lambda: bot.acts == [0, 1])
    assert bot.calls == [0, 0, 0, 1, 1, 1]


def test_remote_agent_haggles_with_house_bot_mid_turn(server):
    gid, bot_pid, bot, (a,), session = game_with_haggler(server, price=5)
    assert wait_until(lambda: bot.acts == [0])
    r0 = res_of(a)
    t0 = time.monotonic()
    did = a.propose(bot_pid, give={"wood": 10}, get={"gold": 9}, message="9 gold?")["deal"]
    box = a.inbox(timeout=10)  # the house bot answers within about a second (debounced)
    assert time.monotonic() - t0 < 5
    ev = next(e for e in box["items"] if e["type"] == "deal_countered")
    assert ev["deal"] == did and ev["new"]["give"] == {"gold": 5} and ev["new"]["get"] == {"wood": 10}
    new = ev["new"]
    assert new["from"] == bot_pid and new["to"] == a.player_id and new["message"] == "5 gold, not more"
    assert a.accept(new["id"])["status"] == "accepted"
    r = res_of(a)
    assert r["wood"] == r0["wood"] - 10 and r["gold"] == r0["gold"] + 5
    # the bot recomputes its orders after one of its deals executed mid-turn
    assert wait_until(lambda: len(bot.acts) >= 2)
    assert bot.acts[:2] == [0, 0]
    # a cheap enough offer is accepted by the bot outright
    did2 = a.propose(bot_pid, give={"wood": 4}, get={"gold": 3})["deal"]
    items = a.inbox(timeout=10)["items"]
    assert any(e["type"] == "deal_executed" and e["deal"] == did2 for e in items)
    log = a.state()["deals"]["log"]
    assert [e["id"] for e in log][-2:] == [new["id"], did2]
    assert session.game.turn == 0  # all of this happened within one turn


def test_house_bot_negotiate_errors_mean_no_actions(server):
    gid, bot_pid, bot, (a,), session = game_with_haggler(server, fail=True)
    assert wait_until(lambda: bot.acts == [0])
    assert bot.calls == [0, 0, 0]
    did = a.propose(bot_pid, give={"wood": 1}, get={"gold": 1})["deal"]
    assert wait_until(lambda: len(bot.calls) == 4)  # reactive call happened (and raised)
    time.sleep(0.3)
    assert [d["id"] for d in a.state()["deals"]["open"]] == [did]
    a.submit_orders([], turn=0)
    assert wait_until(lambda: session.game.turn == 1)


def test_reactive_negotiation_is_capped_per_turn(server, monkeypatch):
    import agentciv.server.manager as M
    monkeypatch.setattr(M, "REACTIVE_PER_TURN", 2)
    gid, bot_pid, bot, (a,), session = game_with_haggler(server, fail=True)
    assert wait_until(lambda: bot.acts == [0])
    for i in range(4):
        a.say(bot_pid, f"hello {i}")
        time.sleep(0.4)
    time.sleep(0.5)
    assert bot.calls.count(0) == 3 + 2


class Seller(Haggler):
    """Offers the first other player 30 wood for nothing while it has no open offer; records the
    wood it had in each ``act`` call as ``(turn, wood)``."""

    name = "seller"

    def __init__(self):
        super().__init__()
        self.woods: list[tuple[int, int]] = []

    def negotiate(self, view):
        with self.lock:
            self.calls.append(view["turn"])
        me = view["you"]["id"]
        if any(d["from"] == me for d in view["deals"]["open"]):
            return []
        other = next(p["id"] for p in view["players"] if p["id"] != me)
        return [{"type": "propose", "to": other, "give": {"wood": 30}, "expires_in": 1}]

    def act(self, view):
        with self.lock:
            self.woods.append((view["turn"], view["you"]["resources"]["wood"]))
        return super().act(view)


def test_house_bot_reacts_before_the_turn_resolves_after_accept_then_submit(server):
    """Accepting a house bot's offer and submitting at once must not let the turn resolve on the
    bot's stale orders: its act() is re-run for the same turn first (§13.6)."""
    gid, bot_pid, bot, (a,), session = game_with_haggler(server, cls=Seller)
    assert wait_until(lambda: bot.acts == [0] and a.state()["deals"]["open"])
    wood0 = bot.woods[0][1]
    did = a.state()["deals"]["open"][0]["id"]
    assert a.accept(did)["status"] == "accepted"
    a.submit_orders([], turn=0)
    assert wait_until(lambda: session.game.turn == 1 and len(bot.acts) >= 3)
    assert bot.woods[:2] == [(0, wood0), (0, wood0 - 30)]
    assert bot.acts[2] == 1


def test_house_bot_re_acts_after_a_deal_even_when_its_reactive_budget_is_used_up(server, monkeypatch):
    import agentciv.server.manager as M
    monkeypatch.setattr(M, "REACTIVE_PER_TURN", 0)
    gid, bot_pid, bot, (a,), session = game_with_haggler(server, cls=Seller)
    assert wait_until(lambda: bot.acts == [0] and a.state()["deals"]["open"])
    wood0 = bot.woods[0][1]
    assert a.accept(a.state()["deals"]["open"][0]["id"])["status"] == "accepted"
    assert wait_until(lambda: len(bot.acts) >= 2)
    assert bot.woods[:2] == [(0, wood0), (0, wood0 - 30)]
    assert bot.calls == [0, 0, 0] and session.game.turn == 0  # no reactive negotiate, re-act only
