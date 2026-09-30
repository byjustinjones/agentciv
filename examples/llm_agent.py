"""An LLM agent that plays AgentCiv through the HTTP API, using Claude with tool use.

    pip install anthropic            # the only extra dependency
    export ANTHROPIC_API_KEY=...     # or: ant auth login
    python -m agentciv.server        # in another terminal
    python examples/llm_agent.py --quickmatch --name Claude
    python examples/llm_agent.py --game g3 --model claude-sonnet-5-5 --effort low

How it works: every game turn starts a *fresh* short conversation (so context
never grows over a 150-turn game). The system prompt holds the full rules
(prompt-cached across turns); the user message holds a compact state summary
(including open deals, contracts and reputation), the ASCII map, what arrived
in the inbox and the agent's own notes from last turn. Claude may inspect the
full JSON state and **barter** — propose deals, counter/accept/reject offers,
send messages and wait a few seconds for replies (``wait_for_replies``) —
then calls ``submit_orders`` (it can resubmit to fix rejected orders). After
the orders are in, the agent keeps watching the inbox until the turn ends:
anything addressed to it (a new offer, a counter, a message) is handed back to
Claude in the same conversation so it can answer in real time. Its ``notes``
are carried to the next turn as memory.

Model: ``--model`` or ``$AGENTCIV_MODEL`` (default: claude-opus-5-5).
Games with LLM players should use a generous ``turn_timeout`` (e.g. 120 s).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))  # run from a checkout
from agentciv.client import AgentCivClient, ApiError, ascii_map, describe_event, summarize_view  # noqa: E402

DEFAULT_MODEL = "claude-opus-5-5"
# Models that accept server-side refusal fallbacks (fallbacks="default").
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-fable-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
DEADLINE_MARGIN = 3.0  # seconds kept free before the turn deadline

SYSTEM_PROMPT = """You are {name}, a player in AgentCiv, competing against other AI agents.
Your goal is to win (finish 1st). The victory conditions are conquest, wonder, influence, relics, economic and
score; the rules below describe what is allowed.

Each turn you receive a state summary, an ASCII map, new diplomacy from your inbox and your notes from last
turn. Use get_full_state if you need exact details (tile owners, armies, improvements, deals).

Diplomacy is live: deals settle the moment they are accepted. Tools: propose_deal and respond_to_deal
(accept | reject | counter | withdraw), say for messages, and wait_for_replies to wait a few seconds for answers.
The turn resolves as soon as every player has submitted orders; diplomacy after that applies to the next turn.

Finally call submit_orders with ALL of this turn's orders (you may call it again to fix rejected orders or
after a deal changed your resources; the latest call replaces earlier ones), and put your plan for the next
turns in `notes`. Orders execute in the order given, paying costs when executed.

THE RULES:
{rules}"""

BUNDLE = {
    "type": "object",
    "description": 'resources {"food","wood","stone","gold"} (integers), "tiles": [[x,y],...] (owned non-city '
                   'tiles), and/or a contract "per_turn": {"gold": 5} with "turns": 1-30. {} = nothing.',
    "properties": {
        "food": {"type": "integer"}, "wood": {"type": "integer"}, "stone": {"type": "integer"},
        "gold": {"type": "integer"},
        "tiles": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}},
        "per_turn": {"type": "object", "additionalProperties": {"type": "integer"}},
        "turns": {"type": "integer"},
    },
}
DEAL_TERMS = {
    "give": {**BUNDLE, "description": "what YOU hand over. " + BUNDLE["description"]},
    "get": {**BUNDLE, "description": "what YOU receive. " + BUNDLE["description"]},
    "peace": {"type": "integer", "description": "optional: 10-50 turns of binding peace on acceptance"},
    "message": {"type": "string", "description": "optional short note (<= 300 chars)"},
}

TOOLS = [
    {
        "name": "get_full_state",
        "description": "Return parts of the full, current JSON game view. Sections: you, players, map, cities, "
                       "armies, market, treaties, treaty_proposals, deals, contracts, messages, events, victory, "
                       "costs.",
        "input_schema": {
            "type": "object",
            "properties": {"sections": {"type": "array", "items": {"type": "string"},
                                        "description": "which top-level sections to return"}},
            "required": ["sections"],
        },
    },
    {
        "name": "propose_deal",
        "description": "Propose a deal to another player now. It stays open (2 turns) until they accept, reject or "
                       "counter it. Example: to p2, give {\"wood\":60}, get {\"gold\":45}.",
        "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, **DEAL_TERMS},
                         "required": ["to"]},
    },
    {
        "name": "respond_to_deal",
        "description": "Answer a deal: accept | reject | counter (deals proposed TO you; for counter give/get are "
                       "YOUR new terms from YOUR point of view) or withdraw (your own proposal).",
        "input_schema": {"type": "object",
                         "properties": {"deal": {"type": "string"},
                                        "response": {"type": "string",
                                                     "enum": ["accept", "reject", "counter", "withdraw"]},
                                        **DEAL_TERMS},
                         "required": ["deal", "response"]},
    },
    {
        "name": "say",
        "description": "Send a message now to a player id (private) or \"all\" (public).",
        "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "text": {"type": "string"}},
                         "required": ["to", "text"]},
    },
    {
        "name": "wait_for_replies",
        "description": "Wait up to `seconds` (max 20) for diplomacy addressed to you (answers to your offers, new "
                       "offers, messages) and return it. Use after proposing, before submitting orders.",
        "input_schema": {"type": "object", "properties": {"seconds": {"type": "number"}}, "required": ["seconds"]},
    },
    {
        "name": "submit_orders",
        "description": "Submit this turn's orders (replaces any earlier submission this turn). Returns which orders "
                       "were rejected and why. Order formats are in the rules, e.g. "
                       '{"type":"claim","at":[x,y]}, {"type":"build","at":[x,y],"building":"farm"}, '
                       '{"type":"recruit","city":[x,y],"unit":"infantry","count":2}, '
                       '{"type":"move","from":[x,y],"path":[[x2,y2]],"units":{"infantry":2}}.',
        "input_schema": {
            "type": "object",
            "properties": {
                "orders": {"type": "array", "items": {"type": "object"}},
                "notes": {"type": "string", "description": "your plan / memory for the next turn"},
            },
            "required": ["orders"],
        },
    },
]


def fail(msg: str) -> None:
    print(msg, file=sys.stderr)
    sys.exit(2)


def make_client():
    try:
        import anthropic
    except ImportError:
        fail("This example needs the Anthropic Python SDK:  pip install anthropic   (or: pip install -e '.[llm]')")
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("note: ANTHROPIC_API_KEY is not set; trying other credentials (e.g. an `ant auth login` profile).",
              file=sys.stderr)
    try:
        return anthropic, anthropic.Anthropic()
    except Exception as e:  # missing credentials etc.
        fail(f"Could not create the Anthropic client ({e}).\n"
             "Set ANTHROPIC_API_KEY (https://console.anthropic.com/) or run `ant auth login`.")


class LLMAgent:
    def __init__(self, args):
        self.args = args
        self.anthropic, self.llm = make_client()
        self.game = AgentCivClient(args.url)
        self.notes = "(first turn: no notes yet)"
        self.system = None

    # ------------------------------------------------------------ Claude call
    def create(self, messages: list):
        kwargs = dict(
            model=self.args.model,
            max_tokens=16000,
            system=self.system,
            tools=TOOLS,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": self.args.effort},
        )
        if self.args.model in FALLBACK_MODELS and not self.args.no_fallback:
            # Server-side refusal fallback: a declined request is re-run on a fallback model.
            return self.llm.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
        return self.llm.messages.create(**kwargs)

    # ------------------------------------------------------------ tools
    def diplomacy(self, action: dict, turn: int) -> tuple[str, bool]:
        try:
            res = self.game.diplomacy([action], turn=turn)
        except ApiError as e:
            return f"failed: {e.message}", True
        r = (res.get("results") or [{}])[0]
        if not r.get("ok"):
            return f"rejected: {r.get('error')}", True
        extra = f" (replaces {r['countered']})" if r.get("countered") else ""
        return "ok" + (f", deal {r['deal']}{extra}" if r.get("deal") else "") + \
            (f", {r['status']}" if r.get("status") else ""), False

    @staticmethod
    def terms(action: dict, args: dict) -> dict:
        action["give"] = args.get("give") or {}
        action["get"] = args.get("get") or {}
        for k in ("peace", "message"):
            if args.get(k):
                action[k] = args[k]
        return action

    def run_tool(self, name: str, args: dict, view: dict) -> tuple[str, bool]:
        turn = view["turn"]
        if name == "get_full_state":
            try:
                fresh = self.game.state()  # deals may have changed since the turn started
            except (ApiError, OSError):
                fresh = view
            sections = args.get("sections") or []
            return json.dumps({k: fresh.get(k) for k in sections if k in fresh}, separators=(",", ":")), False
        if name == "propose_deal":
            return self.diplomacy(self.terms({"type": "propose", "to": args.get("to")}, args), turn)
        if name == "respond_to_deal":
            resp = args.get("response")
            if resp == "counter":
                return self.diplomacy(self.terms({"type": "counter", "deal": args.get("deal")}, args), turn)
            action = {"type": resp, "deal": args.get("deal")}
            if resp == "reject" and args.get("message"):
                action["message"] = args["message"]
            return self.diplomacy(action, turn)
        if name == "say":
            return self.diplomacy({"type": "say", "to": args.get("to"), "text": args.get("text")}, turn)
        if name == "wait_for_replies":
            try:
                seconds = float(args.get("seconds") or 10)
            except (TypeError, ValueError):
                seconds = 10.0
            seconds = max(0.0, min(seconds, 20.0, self.time_left(view) - DEADLINE_MARGIN))
            box = self.game.inbox(timeout=seconds, turn=turn)
            if box.get("turn") != turn or box.get("status") != "running":
                return "The turn is already over.", True
            lines = self.describe(box)
            return "\n".join(lines) if lines else f"Nothing new after {seconds:.0f}s.", False
        if name == "submit_orders":
            orders = args.get("orders")
            if not isinstance(orders, list):
                return "orders must be an array", True
            if isinstance(args.get("notes"), str):
                self.notes = args["notes"][:4000]
            try:
                res = self.game.submit_orders(orders, turn=turn)
            except ApiError as e:
                return f"submit failed: {e.message}", True
            lines = [f"{res['accepted']} accepted, {len(res['errors'])} rejected."]
            lines += [f"order #{e['index']}: {e['error']}" for e in res["errors"]]
            return "\n".join(lines), False
        return f"unknown tool {name}", True

    def describe(self, box: dict) -> list[str]:
        pid = self.game.player_id
        return [describe_event(e, pid) for e in box.get("items") or []]

    @staticmethod
    def time_left(view: dict) -> float:
        return view["deadline"] - time.time() if view.get("deadline") else float("inf")

    # ------------------------------------------------------------ one turn
    def converse(self, messages: list, view: dict, steps: int) -> tuple[int, bool]:
        """Let Claude call tools until it stops. Returns (steps used, orders submitted)."""
        submitted = False
        used = 0
        while used < steps:
            if self.time_left(view) < DEADLINE_MARGIN:
                print("  (turn deadline passed)", file=sys.stderr)
                break
            used += 1
            try:
                response = self.create(messages)
            except self.anthropic.RateLimitError:
                time.sleep(5)
                continue
            except self.anthropic.APIStatusError as e:
                print(f"  API error {e.status_code}: {e.message}", file=sys.stderr)
                break
            except self.anthropic.APIConnectionError as e:
                print(f"  network error: {e}", file=sys.stderr)
                break
            if response.stop_reason == "refusal":
                print("  (the model declined this request; skipping the turn)", file=sys.stderr)
                break
            messages.append({"role": "assistant", "content": response.content})
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                break
            results = []
            clean = False
            for block in tool_uses:
                text, is_error = self.run_tool(block.name, block.input if isinstance(block.input, dict) else {},
                                               view)
                if block.name == "submit_orders" and not is_error:
                    submitted = True
                    clean = " 0 rejected" in text.splitlines()[0]
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": text,
                                "is_error": is_error})
                if block.name in ("submit_orders", "propose_deal", "respond_to_deal"):
                    print(f"  {block.name}: {text.splitlines()[0]}", file=sys.stderr)
            messages.append({"role": "user", "content": results})
            if clean and all(b.name == "submit_orders" for b in tool_uses):
                break  # every order accepted: no need for another round trip
        return used, submitted

    def play_turn(self, view: dict) -> None:
        pid = view["you"]["id"]
        turn = view["turn"]
        # what arrived since we last looked (answers to last turn's offers, messages...)
        try:
            inbox = self.describe(self.game.inbox(timeout=0))
        except (ApiError, OSError):
            inbox = []
        news = "\n".join(inbox[-30:]) or "(nothing new)"
        prompt = (f"Turn {turn} of {view['max_turns']}.\n\nYOUR NOTES FROM LAST TURN:\n{self.notes}\n\n"
                  f"NEW DIPLOMACY (inbox):\n{news}\n\nSTATE:\n{summarize_view(view, pid)}\n\n"
                  f"MAP:\n{ascii_map(view, pid)}\n\n"
                  "Negotiate if useful, then call submit_orders.")
        messages = [{"role": "user", "content": prompt}]
        steps = self.args.max_steps
        used, submitted = self.converse(messages, view, steps)
        steps -= used
        if not submitted:
            try:
                self.game.submit_orders([], turn=turn)  # don't hold the game up
            except ApiError:
                pass
        # Orders are in; until the turn ends, answer whatever is addressed to us in real time.
        while steps > 0 and self.time_left(view) > DEADLINE_MARGIN:
            try:
                box = self.game.inbox(timeout=min(30.0, self.time_left(view) - DEADLINE_MARGIN), turn=turn)
            except (ApiError, OSError):
                break
            if box.get("turn") != turn or box.get("status") != "running":
                break
            mine = [e for e in box.get("items") or [] if pid in (e.get("to"), e.get("from"))
                    and e.get("type") in ("deal_proposed", "deal_countered", "deal_executed", "deal_rejected",
                                          "deal_failed", "say")]
            if not mine:
                continue
            lines = [describe_event(e, pid) for e in mine]
            messages.append({"role": "user", "content":
                             "Your orders are submitted, but the turn is still open. New diplomacy:\n"
                             + "\n".join(lines) + "\nRespond if useful (respond_to_deal / propose_deal / say). "
                             "If a deal changed your resources, resubmit your full orders; otherwise just stop."})
            print(f"  inbox: {lines[0]}" + (f" (+{len(lines) - 1})" if len(lines) > 1 else ""), file=sys.stderr)
            used, _ = self.converse(messages, view, steps)
            steps -= max(1, used)

    # ------------------------------------------------------------ game loop
    def run(self) -> None:
        a = self.args
        if a.game:
            joined = self.game.join(a.game, a.name)
        else:
            joined = self.game.quickmatch(a.name, players=a.players, turn_timeout=a.turn_timeout)
        print(f"Joined {joined['game_id']} as {joined['player_id']} with model {a.model}", file=sys.stderr)
        self.system = [{"type": "text", "text": SYSTEM_PROMPT.format(name=a.name, rules=self.game.rules()),
                        "cache_control": {"type": "ephemeral"}}]  # identical every turn -> cached
        last = -1
        while True:
            w = self.game.wait(since_turn=last, timeout=30)
            if w["status"] == "lobby":
                continue
            view = self.game.state()
            if view["status"] == "finished":
                break
            if view["turn"] <= last:
                continue
            last = view["turn"]
            if not view["you"]["alive"]:
                continue
            print(f"turn {last}: thinking…", file=sys.stderr)
            self.play_turn(view)
        result = view["victory"]["result"] or {}
        places = result.get("placements", [])
        me = self.game.player_id
        print(f"Game over: winner {result.get('winner')} by {result.get('condition')}; "
              f"you placed {places.index(me) + 1 if me in places else '?'} of {len(places)}.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Claude plays AgentCiv")
    ap.add_argument("--url", default=os.environ.get("AGENTCIV_URL", "http://localhost:8765"))
    ap.add_argument("--model", default=os.environ.get("AGENTCIV_MODEL", DEFAULT_MODEL))
    ap.add_argument("--effort", default=os.environ.get("AGENTCIV_EFFORT", "medium"),
                    choices=["low", "medium", "high", "xhigh", "max"], help="thinking effort (default medium)")
    ap.add_argument("--name", default="Claude")
    ap.add_argument("--game", help="game id to join (default: quickmatch)")
    ap.add_argument("--quickmatch", action="store_true", help="join a quickmatch lobby (the default)")
    ap.add_argument("--players", type=int, default=6, help="quickmatch lobby size")
    ap.add_argument("--turn-timeout", type=float, default=120.0, help="quickmatch turn timeout in seconds")
    ap.add_argument("--max-steps", type=int, default=10, help="max model calls per game turn (incl. negotiation)")
    ap.add_argument("--no-fallback", action="store_true", help="disable server-side refusal fallbacks")
    LLMAgent(ap.parse_args()).run()


if __name__ == "__main__":
    main()
